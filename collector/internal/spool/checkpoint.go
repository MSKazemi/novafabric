package spool

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// checkpointFile is the name of the checkpoint JSON file within the spool dir.
const checkpointFile = "checkpoint.json"

// neverAckedSentinel is stored in LastAckSeq when no events have ever been
// acknowledged.  It is distinct from 0 so that segment 0 is not mistakenly
// treated as already-consumed on a brand-new spool.
const neverAckedSentinel = ^uint64(0) // math.MaxUint64

// checkpointData is the on-disk JSON representation of a Checkpoint.
type checkpointData struct {
	LastAckSeq    uint64 `json:"last_ack_seq"`
	LastAckOffset int64  `json:"last_ack_offset"`
	// Initialized is false for a never-written checkpoint file; once any
	// Advance call succeeds, it is set to true and persisted.
	Initialized bool `json:"initialized"`
}

// Checkpoint tracks which events have been acknowledged by downstream
// consumers. It is stored as a JSON file and updated atomically via rename.
type Checkpoint struct {
	dir  string
	data checkpointData
}

// LoadCheckpoint reads the checkpoint file from dir. If no file exists, a
// fresh checkpoint (no events acknowledged) is returned.
func LoadCheckpoint(dir string) (*Checkpoint, error) {
	cp := &Checkpoint{
		dir: dir,
		// Sentinel: no events ever acknowledged.  Using neverAckedSentinel
		// means no segment seq number can equal it (seqNum is a monotonically
		// increasing uint64 starting from 0, but we never write 2^64-1 segments
		// in practice).
		data: checkpointData{LastAckSeq: neverAckedSentinel},
	}
	path := filepath.Join(dir, checkpointFile)

	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return cp, nil
		}
		return nil, fmt.Errorf("spool: read checkpoint %s: %w", path, err)
	}

	if jsonErr := json.Unmarshal(raw, &cp.data); jsonErr != nil {
		return nil, fmt.Errorf("spool: parse checkpoint %s: %w", path, jsonErr)
	}
	if !cp.data.Initialized {
		// Checkpoint file exists but was written by an older version without the
		// Initialized field.  Trust the stored LastAckSeq.
		cp.data.Initialized = true
	}
	return cp, nil
}

// Advance atomically updates the checkpoint to (seq, offset).
// It writes to a .tmp file then renames into place — the only atomic
// primitive used in the spool.
func (cp *Checkpoint) Advance(seq uint64, offset int64) error {
	cp.data.LastAckSeq = seq
	cp.data.LastAckOffset = offset
	cp.data.Initialized = true

	raw, err := json.Marshal(cp.data)
	if err != nil {
		return fmt.Errorf("spool: marshal checkpoint: %w", err)
	}

	dstPath := filepath.Join(cp.dir, checkpointFile)
	tmpPath := dstPath + ".tmp"

	f, err := os.OpenFile(tmpPath, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o600)
	if err != nil {
		return fmt.Errorf("spool: open checkpoint tmp: %w", err)
	}
	if _, err := f.Write(raw); err != nil {
		_ = f.Close()
		return fmt.Errorf("spool: write checkpoint tmp: %w", err)
	}
	// fdatasyncFile flushes data pages to stable storage before the rename so
	// the checkpoint survives a power failure between the write and the rename.
	// This closes the crash-safety gap where the kernel may defer writeback
	// past the rename commit point.  The implementation is platform-specific
	// (syscall.Fdatasync on Linux; f.Sync elsewhere — see checkpoint_sync_*.go).
	if err := fdatasyncFile(f); err != nil {
		_ = f.Close()
		return fmt.Errorf("spool: fdatasync checkpoint tmp: %w", err)
	}
	if err := f.Close(); err != nil {
		return fmt.Errorf("spool: close checkpoint tmp: %w", err)
	}
	// Atomic rename — the ONLY commit primitive in the spool.
	if err := os.Rename(tmpPath, dstPath); err != nil {
		return fmt.Errorf("spool: rename checkpoint: %w", err)
	}
	return nil
}

// LastAckSeq returns the sequence number of the last acknowledged segment, or
// neverAckedSentinel if no segment has ever been acknowledged.
func (cp *Checkpoint) LastAckSeq() uint64 { return cp.data.LastAckSeq }

// HasAcked returns true if at least one Advance call has been made.
func (cp *Checkpoint) HasAcked() bool { return cp.data.Initialized }

// LastAckOffset returns the byte offset within the last acknowledged segment
// up to which events have been consumed.
func (cp *Checkpoint) LastAckOffset() int64 { return cp.data.LastAckOffset }

// recoverSegments scans dir and returns the paths of all fully committed
// .jsonl segment files, sorted by sequence number.  Any .jsonl.tmp files
// are discarded (logged at WARN level for each one) — they represent
// incomplete writes from a prior process that crashed before committing.
func recoverSegments(dir string) ([]string, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, fmt.Errorf("spool: read spool dir %s: %w", dir, err)
	}

	var segments []string
	for _, e := range entries {
		name := e.Name()
		if strings.HasSuffix(name, ".jsonl.tmp") {
			// Incomplete segment from a crashed writer — discard it.
			fullPath := filepath.Join(dir, name)
			slog.Warn("spool: discarding incomplete segment from crashed writer",
				"path", fullPath)
			if rmErr := os.Remove(fullPath); rmErr != nil {
				slog.Warn("spool: failed to remove incomplete segment",
					"path", fullPath, "error", rmErr)
			}
			continue
		}
		if strings.HasSuffix(name, ".jsonl") {
			segments = append(segments, filepath.Join(dir, name))
		}
	}

	// Sort by filename, which sorts by sequence number (zero-padded 20-digit format).
	sort.Strings(segments)
	return segments, nil
}
