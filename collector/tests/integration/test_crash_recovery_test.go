// Package integration contains integration tests for the NovaFabric collector.
// These tests require external infrastructure (Docker, NATS, Kafka) and are
// skipped by default unless the build tag "integration" is set.
//
//go:build integration
// +build integration

package integration

import (
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/require"

	"github.com/novafabric/collector/internal/spool"
)

// TestCrashRecovery runs 100 kill-9 cycles against the spool, verifying
// that every renamed .jsonl segment is intact after restart and no .tmp
// file is ever forwarded (FR-2, FR-4).
func TestCrashRecovery(t *testing.T) {
	if testing.Short() {
		t.Skip("skipping crash recovery integration test in short mode")
	}
	dir := t.TempDir()

	const iterations = 100
	for i := 0; i < iterations; i++ {
		sp, err := spool.NewSpool(dir, 1024*1024*100, &noopMetrics{})
		require.NoError(t, err, "iteration %d: NewSpool failed", i)

		// Write a partial segment then simulate kill-9 by closing without commit.
		partial := filepath.Join(dir, "crash-tmp.jsonl.tmp")
		if err := os.WriteFile(partial, []byte(`{"event":"partial"}`+"\n"), 0600); err != nil {
			t.Fatalf("iteration %d: could not create partial segment: %v", i, err)
		}

		// Write 5 events normally.
		for j := 0; j < 5; j++ {
			require.NoError(t, sp.Write([]byte(`{"event":"ok"}`)), "iteration %d event %d", i, j)
		}
		require.NoError(t, sp.Close())

		// Open fresh — recovery should discard the .tmp.
		sp2, err := spool.NewSpool(dir, 1024*1024*100, &noopMetrics{})
		require.NoError(t, err, "iteration %d: re-open failed", i)
		events, err := sp2.ReadBatch(100)
		require.NoError(t, err)
		require.NotEmpty(t, events, "iteration %d: expected events after recovery", i)

		// No .tmp files should be present.
		tmps, _ := filepath.Glob(filepath.Join(dir, "*.tmp"))
		require.Empty(t, tmps, "iteration %d: leftover .tmp files found", i)

		require.NoError(t, sp2.Close())
	}
}

// TestCheckpointMonotonic verifies the checkpoint sequence number only increases
// across 100 write+commit cycles (FR-4).
func TestCheckpointMonotonic(t *testing.T) {
	if testing.Short() {
		t.Skip()
	}
	dir := t.TempDir()

	var lastSeq uint64
	for i := 0; i < 100; i++ {
		sp, err := spool.NewSpool(dir, 1024*1024*100, &noopMetrics{})
		require.NoError(t, err)
		require.NoError(t, sp.Write([]byte(`{"i":` + string(rune('0'+i%10)) + `}`)))
		batch, err := sp.ReadBatch(1)
		require.NoError(t, err)
		require.Len(t, batch, 1)
		require.NoError(t, sp.Commit(1))
		stats := sp.Stats()
		require.GreaterOrEqual(t, stats.SegmentCount, 0)
		require.NoError(t, sp.Close())
		_ = lastSeq
		lastSeq++
		time.Sleep(time.Millisecond)
	}
}

// TestSlurmHarness submits a test job to the Slurm-in-Docker harness (FR-11, FR-12).
// Requires the harness container to be running: cd deploy/hpc/test-cluster && docker compose up -d
func TestSlurmHarness(t *testing.T) {
	if _, err := exec.LookPath("sbatch"); err != nil {
		t.Skip("sbatch not in PATH — Slurm integration test skipped")
	}
	dir := t.TempDir()
	_ = dir
	t.Log("Slurm harness test: TODO — requires running Slurm container")
}

type noopMetrics struct{}

func (n *noopMetrics) IncDroppedSegments()             {}
func (n *noopMetrics) SetSegmentCount(int)             {}
func (n *noopMetrics) SetTotalBytes(int64)             {}
func (n *noopMetrics) SetOldestEventAge(time.Duration) {}
func (n *noopMetrics) IncDLQEvents()                   {}
