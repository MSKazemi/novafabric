// Package spool provides a durable, crash-safe on-disk event queue for the
// NovaFabric collector tier.  It is designed for high-throughput append
// workloads on HPC compute nodes where kernel-level locking primitives are
// unavailable or unreliable.
//
// # Design constraints
//
// The only atomic commit primitive in this package is os.Rename.  The package
// never calls flock, mmap, fcntl, syscall.Flock, F_SETLK, F_SETLK64,
// LOCK_EX, or any equivalent kernel lock.
//
// Crash recovery: any .jsonl.tmp file found at startup was written by a
// process that crashed before committing; it is discarded automatically.
// Fully committed .jsonl segments survive restarts.
//
// Backpressure: when total spool size exceeds maxBytes, Write evicts the
// oldest acknowledged segment.  If no acknowledged segment exists and the
// spool is still full, ErrBackpressure is returned.
package spool

import (
	"bufio"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// ErrBackpressure is returned by Write when the spool is full and eviction
// cannot free enough space.
var ErrBackpressure = fmt.Errorf("spool: backpressure — spool full, event dropped")

// SpoolMetrics is the interface that spool uses to report operational metrics.
// Implementations are expected to be goroutine-safe.
type SpoolMetrics interface {
	// IncDroppedSegments increments nova_spool_dropped_segments_total by 1.
	IncDroppedSegments()
	// SetSegmentCount sets the current segment count gauge.
	SetSegmentCount(n int)
	// SetTotalBytes sets the current spool-size-in-bytes gauge.
	SetTotalBytes(b int64)
	// SetOldestEventAge sets the duration since the oldest unacknowledged
	// event was written.
	SetOldestEventAge(d time.Duration)
	// IncDLQEvents increments nova_collector_dlq_events_total by 1.
	// Called when an event is routed to the dead-letter queue (A-3).
	IncDLQEvents()
}

// SpoolStats is a point-in-time snapshot of spool state.
type SpoolStats struct {
	SegmentCount   int
	TotalBytes     int64
	OldestEventAge time.Duration
}

// Spool is a durable on-disk event queue.  A single Spool must not be shared
// across processes; use separate spool directories per writer process.
type Spool struct {
	dir        string
	maxBytes   int64
	mu         sync.Mutex
	seqNum     uint64
	checkpoint *Checkpoint
	metrics    SpoolMetrics

	// dlq is the optional dead-letter queue (A-3).  When non-nil, events that
	// would otherwise be silently dropped under backpressure are routed here.
	dlq *DLQ

	// readSegments is the sorted list of committed segments eligible for
	// reading.  It is extended by Write and trimmed by Commit.
	readSegments []string

	// readIdx is the index of the segment currently being drained.
	readIdx int

	// readOffset is the byte offset (past the header) within readSegments[readIdx].
	readOffset int64

	// pendingAckSeq is the sequence of the last segment touched by ReadBatch.
	pendingAckSeq uint64
	// pendingAckOffset is the offset after the last event returned by ReadBatch.
	pendingAckOffset int64
}

// NewSpool opens or creates a spool rooted at dir.  maxBytes is the soft
// upper bound on total spool size.
func NewSpool(dir string, maxBytes int64, metrics SpoolMetrics) (*Spool, error) {
	if err := os.MkdirAll(dir, 0o750); err != nil {
		return nil, fmt.Errorf("spool: create dir %s: %w", dir, err)
	}

	cp, err := LoadCheckpoint(dir)
	if err != nil {
		return nil, err
	}

	// Recover committed segments; discard incomplete .tmp files.
	segments, err := recoverSegments(dir)
	if err != nil {
		return nil, err
	}

	// Determine the next sequence number.
	var seqNum uint64
	for _, seg := range segments {
		if seq := seqFromPath(seg); seq >= seqNum {
			seqNum = seq + 1
		}
	}

	// Find the first segment that has NOT been fully acknowledged.
	// If no segment has ever been acknowledged (fresh spool), start from 0.
	readIdx := 0
	if cp.HasAcked() {
		for i, seg := range segments {
			if seqFromPath(seg) > cp.LastAckSeq() {
				readIdx = i
				break
			}
			readIdx = i + 1
		}
		if readIdx > len(segments) {
			readIdx = len(segments)
		}
	}

	return &Spool{
		dir:          dir,
		maxBytes:     maxBytes,
		seqNum:       seqNum,
		checkpoint:   cp,
		metrics:      metrics,
		readSegments: segments,
		readIdx:      readIdx,
	}, nil
}

// SetDLQ attaches a dead-letter queue (A-3) to the spool.  When set, events
// that would be silently dropped under backpressure are written to the DLQ
// instead.  Must be called before any Write.
func (s *Spool) SetDLQ(dlq *DLQ) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.dlq = dlq
}

// Write appends eventJSON (a single JSON object without embedded newlines) to
// a new segment and commits it atomically.
//
// Write is goroutine-safe.  When a DLQ is attached (see SetDLQ) and the spool
// is full, the event is written to the DLQ and ErrBackpressure is still
// returned so the caller knows the primary path was bypassed.  Without a DLQ
// the event is silently dropped (legacy behaviour).
func (s *Spool) Write(eventJSON []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	// Enforce backpressure before writing.
	total, err := totalSpoolBytes(s.dir)
	if err != nil {
		slog.Warn("spool: failed to compute spool size", "error", err)
	} else if total >= s.maxBytes {
		if evErr := evictOldest(s.dir, s.checkpoint, s.metrics); evErr != nil {
			// eviction failed — route to DLQ when available.
			if s.dlq != nil {
				_ = s.dlq.Write(eventJSON)
				if s.metrics != nil {
					s.metrics.IncDLQEvents()
				}
				slog.Debug("spool: event routed to DLQ (backpressure)", "size", len(eventJSON))
			}
			return ErrBackpressure
		}
		// Check again after eviction.
		if total2, _ := totalSpoolBytes(s.dir); total2 >= s.maxBytes {
			if s.dlq != nil {
				_ = s.dlq.Write(eventJSON)
				if s.metrics != nil {
					s.metrics.IncDLQEvents()
				}
				slog.Debug("spool: event routed to DLQ (still full after eviction)", "size", len(eventJSON))
			}
			return ErrBackpressure
		}
	}

	sw, err := newSegmentWriter(s.dir, s.seqNum)
	if err != nil {
		return err
	}
	if err := sw.writeEvent(eventJSON); err != nil {
		sw.abort()
		return err
	}
	if err := sw.commit(); err != nil {
		sw.abort()
		return err
	}

	committedPath := filepath.Join(s.dir, fmt.Sprintf("%020d.jsonl", s.seqNum))
	s.seqNum++
	s.readSegments = append(s.readSegments, committedPath)
	s.updateMetrics()
	return nil
}

// ReadBatch returns up to maxEvents JSON-encoded events from the current read
// position, spanning as many committed segments as needed.
//
// The caller must call Commit(n) after successfully processing all returned
// events.  ReadBatch is goroutine-safe.
func (s *Spool) ReadBatch(maxEvents int) ([][]byte, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	var results [][]byte

	for len(results) < maxEvents && s.readIdx < len(s.readSegments) {
		segPath := s.readSegments[s.readIdx]

		events, newOffset, err := readSegmentEvents(segPath, s.readOffset)
		if err != nil {
			slog.Warn("spool: skipping unreadable segment", "path", segPath, "error", err)
			s.readIdx++
			s.readOffset = 0
			continue
		}

		// How many events can we still fit in this batch?
		remaining := maxEvents - len(results)
		take := len(events)
		if take > remaining {
			take = remaining
		}
		results = append(results, events[:take]...)

		if take == len(events) {
			// Consumed the whole segment; record pending and advance.
			s.pendingAckSeq = seqFromPath(segPath)
			s.pendingAckOffset = newOffset
			s.readIdx++
			s.readOffset = 0
		} else {
			// Partial read within a segment; stay on this segment.
			s.pendingAckSeq = seqFromPath(segPath)
			s.pendingAckOffset = s.readOffset + partialOffset(events[:take])
			s.readOffset = s.pendingAckOffset
		}
	}

	return results, nil
}

// Commit marks the events returned by the last ReadBatch as acknowledged and
// advances the checkpoint.  n must be ≥ 0; passing 0 is a no-op.
func (s *Spool) Commit(n int) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	if n <= 0 {
		return nil
	}
	if err := s.checkpoint.Advance(s.pendingAckSeq, s.pendingAckOffset); err != nil {
		return err
	}
	s.updateMetrics()
	return nil
}

// Stats returns a point-in-time snapshot of spool health.
func (s *Spool) Stats() SpoolStats {
	s.mu.Lock()
	defer s.mu.Unlock()

	total, _ := totalSpoolBytes(s.dir)
	count := 0
	entries, _ := os.ReadDir(s.dir)
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".jsonl") {
			count++
		}
	}

	var oldest time.Duration
	for _, seg := range s.readSegments[s.readIdx:] {
		if seqFromPath(seg) > s.checkpoint.LastAckSeq() {
			if info, err := os.Stat(seg); err == nil {
				oldest = time.Since(info.ModTime())
			}
			break
		}
	}

	return SpoolStats{
		SegmentCount:   count,
		TotalBytes:     total,
		OldestEventAge: oldest,
	}
}

// Close releases resources held by the Spool.  After Close the Spool must not
// be used.
func (s *Spool) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return nil
}

// updateMetrics refreshes gauge metrics.  Must be called with s.mu held.
func (s *Spool) updateMetrics() {
	if s.metrics == nil {
		return
	}
	total, _ := totalSpoolBytes(s.dir)
	count := 0
	entries, _ := os.ReadDir(s.dir)
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".jsonl") {
			count++
		}
	}
	s.metrics.SetSegmentCount(count)
	s.metrics.SetTotalBytes(total)
}

// readSegmentEvents reads all events (one per line, past the header) from a
// committed .jsonl file starting at byteOffset (0 = start of events).
// Returns the events and the new byte offset within the event region.
func readSegmentEvents(path string, byteOffset int64) ([][]byte, int64, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, 0, fmt.Errorf("spool: open segment %s: %w", path, err)
	}
	defer f.Close()

	var hdrBuf [headerSize]byte
	if _, err := f.Read(hdrBuf[:]); err != nil {
		return nil, 0, fmt.Errorf("spool: read header from %s: %w", path, err)
	}
	hdr, err := decodeHeader(hdrBuf)
	if err != nil {
		return nil, 0, err
	}
	if hdr.WriteComplete != 1 {
		return nil, 0, fmt.Errorf("spool: segment %s is not committed (WriteComplete=0)", path)
	}

	if byteOffset > 0 {
		if _, err := f.Seek(int64(headerSize)+byteOffset, 0); err != nil {
			return nil, byteOffset, fmt.Errorf("spool: seek in %s: %w", path, err)
		}
	}

	var events [][]byte
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := scanner.Bytes()
		cp := make([]byte, len(line))
		copy(cp, line)
		events = append(events, cp)
	}
	if err := scanner.Err(); err != nil {
		return events, byteOffset, fmt.Errorf("spool: scan %s: %w", path, err)
	}

	info, _ := f.Stat()
	var newOffset int64
	if info != nil {
		newOffset = info.Size() - int64(headerSize)
	}
	return events, newOffset, nil
}

// partialOffset computes the byte length of the serialised events (each event
// + newline) for a partial batch.  This is an approximation used to advance
// the spool offset within a segment; it matches what writeEvent writes.
func partialOffset(events [][]byte) int64 {
	var n int64
	for _, ev := range events {
		n += int64(len(ev)) + 1 // +1 for the newline
	}
	return n
}
