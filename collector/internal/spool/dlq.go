package spool

// dlq.go — file-based dead-letter queue (A-3).
//
// When the spool is under backpressure and an event would be silently dropped,
// the DLQ writes it to a JSON-lines file under NOVA_DLQ_DIR (default:
// <spool_dir>/dlq/).  Events in the DLQ can be re-injected by the
// nova-collector replay-dlq subcommand.
//
// Design constraints:
//   - No kernel locks; writes are guarded by a sync.Mutex within the process.
//   - One DLQ file per day (rotated by UTC date) to keep replay manageable.
//   - The DLQ never blocks the caller — if the DLQ write itself fails, the
//     error is logged but not propagated (fail-open to protect the hot path).
//   - DLQ events are raw JSON bytes (same schema as spool events).

import (
	"bufio"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// DLQ is a file-based dead-letter queue for dropped collector events.
// All methods are goroutine-safe.
type DLQ struct {
	dir string
	mu  sync.Mutex
	f   *os.File
	day string // current log date (YYYY-MM-DD); triggers rotation
}

// NewDLQ creates a DLQ rooted at dir.  The directory is created if it does
// not exist.
func NewDLQ(dir string) (*DLQ, error) {
	if err := os.MkdirAll(dir, 0o750); err != nil {
		return nil, fmt.Errorf("dlq: create dir %s: %w", dir, err)
	}
	return &DLQ{dir: dir}, nil
}

// Write appends eventJSON (a single JSON object without embedded newlines) to
// the current DLQ file.  The file is rotated daily (UTC).
//
// If the write fails, the error is logged but not returned — the DLQ is
// fail-open to protect the caller's hot path.
func (d *DLQ) Write(eventJSON []byte) error {
	d.mu.Lock()
	defer d.mu.Unlock()

	today := time.Now().UTC().Format("2006-01-02")
	if err := d.ensureFile(today); err != nil {
		slog.Warn("dlq: could not open DLQ file; dropping event", "error", err, "date", today)
		return nil // fail-open
	}

	w := bufio.NewWriter(d.f)
	if _, err := w.Write(eventJSON); err != nil {
		slog.Warn("dlq: write failed", "error", err)
		return nil // fail-open
	}
	if err := w.WriteByte('\n'); err != nil {
		slog.Warn("dlq: write newline failed", "error", err)
		return nil // fail-open
	}
	if err := w.Flush(); err != nil {
		slog.Warn("dlq: flush failed", "error", err)
		return nil // fail-open
	}
	return nil
}

// Size returns the total bytes across all DLQ files in the directory.
// Returns 0 if the directory cannot be read.
func (d *DLQ) Size() (int64, error) {
	entries, err := os.ReadDir(d.dir)
	if err != nil {
		return 0, fmt.Errorf("dlq: read dir %s: %w", d.dir, err)
	}
	var total int64
	for _, e := range entries {
		if info, err := e.Info(); err == nil {
			total += info.Size()
		}
	}
	return total, nil
}

// Replay reads all events from all DLQ files in chronological order and
// calls fn for each raw JSON-encoded event.  fn should return nil to
// continue; returning an error stops replay and that error is returned.
//
// Files that cannot be opened are skipped with a warning.
func (d *DLQ) Replay(fn func([]byte) error) error {
	d.mu.Lock()
	entries, err := os.ReadDir(d.dir)
	d.mu.Unlock()

	if err != nil {
		return fmt.Errorf("dlq: replay: read dir %s: %w", d.dir, err)
	}

	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		path := filepath.Join(d.dir, entry.Name())
		if err := replayFile(path, fn); err != nil {
			return err
		}
	}
	return nil
}

// Close flushes and closes the current DLQ file.
func (d *DLQ) Close() error {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.f != nil {
		err := d.f.Close()
		d.f = nil
		d.day = ""
		return err
	}
	return nil
}

// ensureFile opens or rotates the DLQ file for today.  Must be called with
// d.mu held.
func (d *DLQ) ensureFile(today string) error {
	if d.f != nil && d.day == today {
		return nil // current file is correct
	}
	// Rotate: close old file.
	if d.f != nil {
		_ = d.f.Close()
		d.f = nil
	}
	path := filepath.Join(d.dir, fmt.Sprintf("dlq-%s.jsonl", today))
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o640)
	if err != nil {
		return fmt.Errorf("dlq: open %s: %w", path, err)
	}
	d.f = f
	d.day = today
	return nil
}

// replayFile streams events from a single DLQ JSON-lines file.
func replayFile(path string, fn func([]byte) error) error {
	f, err := os.Open(path)
	if err != nil {
		slog.Warn("dlq: replay: cannot open file; skipping", "path", path, "error", err)
		return nil
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := scanner.Bytes()
		if len(line) == 0 {
			continue
		}
		cp := make([]byte, len(line))
		copy(cp, line)
		if err := fn(cp); err != nil {
			return err
		}
	}
	return scanner.Err()
}
