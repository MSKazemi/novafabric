package spool

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// evictOldest finds and removes the oldest fully-acknowledged committed
// segment (.jsonl) in dir, freeing disk space when the spool is under
// backpressure.
//
// Only segments whose sequence number is ≤ cp.LastAckSeq() are considered
// safe to evict — evicting an un-acknowledged segment would silently drop
// un-consumed events.  If no acknowledged segment is available the function
// returns an error so that the caller can propagate ErrBackpressure.
//
// It increments the dropped-segments counter via metrics on each successful
// deletion.
func evictOldest(dir string, cp *Checkpoint, m SpoolMetrics) error {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return fmt.Errorf("spool eviction: read dir %s: %w", dir, err)
	}

	// Collect only fully-acknowledged segments, sorted ascending (oldest first).
	// When no segments have ever been acknowledged (HasAcked=false), the
	// candidate list is empty and we return an error immediately.
	var candidates []string
	if cp.HasAcked() {
		for _, e := range entries {
			if !strings.HasSuffix(e.Name(), ".jsonl") {
				continue
			}
			path := filepath.Join(dir, e.Name())
			if seqFromPath(path) <= cp.LastAckSeq() {
				candidates = append(candidates, path)
			}
		}
	}
	sort.Strings(candidates)

	if len(candidates) == 0 {
		return fmt.Errorf("spool eviction: no acknowledged segments available to evict in %s", dir)
	}

	if err := os.Remove(candidates[0]); err != nil {
		return fmt.Errorf("spool eviction: remove %s: %w", candidates[0], err)
	}
	m.IncDroppedSegments()
	return nil
}

// totalSpoolBytes returns the sum of sizes of all committed .jsonl files in dir.
func totalSpoolBytes(dir string) (int64, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return 0, fmt.Errorf("spool: totalSpoolBytes read dir %s: %w", dir, err)
	}

	var total int64
	for _, e := range entries {
		if !strings.HasSuffix(e.Name(), ".jsonl") {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue // file may have been evicted concurrently
		}
		total += info.Size()
	}
	return total, nil
}

// seqFromPath extracts the sequence number from a segment file path.
// The filename format is <20-digit-zero-padded-seq>.jsonl.
func seqFromPath(path string) uint64 {
	base := filepath.Base(path)
	base = strings.TrimSuffix(base, ".jsonl")
	var seq uint64
	_, _ = fmt.Sscanf(base, "%d", &seq)
	return seq
}
