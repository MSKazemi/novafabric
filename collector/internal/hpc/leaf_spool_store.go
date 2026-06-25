package hpc

import (
	"fmt"
	"log/slog"
	"os"
	"time"

	"github.com/novafabric/collector/internal/spool"
)

// envDLQDir is the environment variable that activates the dead-letter queue
// for the HPC leaf spool store (G-A3).  When set to a non-empty path, events
// dropped under backpressure are written to JSONL files under that directory
// instead of being silently lost.
const envDLQDir = "NOVA_DLQ_DIR"

// envSpoolMode is the environment variable that activates spool-backed storage
// for the NATS JetStream leaf node.  When set to any non-empty value the leaf
// node's store_dir is pointed at the spool directory, allowing the NATS server
// to persist messages using the same directory as the NovaFabric spool.
const envSpoolMode = "NOVAFABRIC_HPC_STORAGE"

// IsSpoolMode returns true if NOVAFABRIC_HPC_STORAGE is set to a non-empty
// value, indicating that the NATS leaf node should use the spool directory as
// its JetStream store_dir.
func IsSpoolMode() bool {
	return os.Getenv(envSpoolMode) != ""
}

// SpoolStore wraps a spool.Spool to present a thin file-based store interface
// that the NATS leaf node configuration can reference.
//
// When IsSpoolMode() returns true, the Leaf sets store_dir to the spool
// directory.  SpoolStore provides the Write/Drain/Close operations that the
// collector pipeline uses independently of NATS internals.
//
// When NOVA_DLQ_DIR is set, events dropped under backpressure are preserved
// in a file-based dead-letter queue at that path (G-A3).
type SpoolStore struct {
	sp     *spool.Spool
	dlq    *spool.DLQ
	dlqDir string
}

// NewSpoolStore creates a SpoolStore backed by the spool at dir.
// maxBytes is the backpressure threshold in bytes.
// metrics may be nil (a no-op implementation is used).
//
// If the environment variable NOVA_DLQ_DIR is set to a non-empty path, a
// dead-letter queue is created at that path and attached to the spool so that
// events dropped under backpressure are preserved rather than silently lost
// (G-A3).  A DLQ initialisation failure is logged as a warning but does NOT
// prevent the SpoolStore from opening — the store operates without a DLQ in
// that case (fail-open).
func NewSpoolStore(dir string, maxBytes int64, metrics spool.SpoolMetrics) (*SpoolStore, error) {
	if metrics == nil {
		metrics = &noopSpoolMetrics{}
	}
	sp, err := spool.NewSpool(dir, maxBytes, metrics)
	if err != nil {
		return nil, fmt.Errorf("hpc spool store: %w", err)
	}

	ss := &SpoolStore{sp: sp}

	if dlqDir := os.Getenv(envDLQDir); dlqDir != "" {
		dlq, dlqErr := spool.NewDLQ(dlqDir)
		if dlqErr != nil {
			slog.Warn("hpc spool store: DLQ init failed; continuing without DLQ (events may be lost under backpressure)",
				"dlq_dir", dlqDir, "error", dlqErr)
		} else {
			sp.SetDLQ(dlq)
			ss.dlq = dlq
			ss.dlqDir = dlqDir
			slog.Info("hpc spool store: DLQ activated (G-A3)", "dlq_dir", dlqDir)
		}
	}

	slog.Info("hpc spool store: opened", "dir", dir, "max_bytes", maxBytes)
	return ss, nil
}

// Write appends eventJSON to the spool.  It returns spool.ErrBackpressure when
// the spool is full.
func (ss *SpoolStore) Write(eventJSON []byte) error {
	return ss.sp.Write(eventJSON)
}

// Drain reads up to maxEvents events from the spool, calls fn for each one,
// and commits them on success.  If fn returns an error the batch is not
// committed and the error is returned.
func (ss *SpoolStore) Drain(maxEvents int, fn func([]byte) error) error {
	batch, err := ss.sp.ReadBatch(maxEvents)
	if err != nil {
		return fmt.Errorf("hpc spool store: ReadBatch: %w", err)
	}
	for _, ev := range batch {
		if fnErr := fn(ev); fnErr != nil {
			return fnErr
		}
	}
	if len(batch) > 0 {
		return ss.sp.Commit(len(batch))
	}
	return nil
}

// Stats returns a point-in-time snapshot of spool state.
func (ss *SpoolStore) Stats() spool.SpoolStats {
	return ss.sp.Stats()
}

// Close releases spool resources and, if a DLQ is attached, flushes and closes
// it as well.
func (ss *SpoolStore) Close() error {
	spErr := ss.sp.Close()
	if ss.dlq != nil {
		if dlqErr := ss.dlq.Close(); dlqErr != nil && spErr == nil {
			return dlqErr
		}
	}
	return spErr
}

// DLQDir returns the path of the dead-letter queue directory configured via
// NOVA_DLQ_DIR, or an empty string if no DLQ is active.
func (ss *SpoolStore) DLQDir() string {
	return ss.dlqDir
}

// noopSpoolMetrics is a do-nothing SpoolMetrics implementation used when no
// metrics backend is configured.
type noopSpoolMetrics struct{}

func (n *noopSpoolMetrics) IncDroppedSegments()             {}
func (n *noopSpoolMetrics) SetSegmentCount(int)             {}
func (n *noopSpoolMetrics) SetTotalBytes(int64)             {}
func (n *noopSpoolMetrics) SetOldestEventAge(time.Duration) {}
func (n *noopSpoolMetrics) IncDLQEvents()                   {}
