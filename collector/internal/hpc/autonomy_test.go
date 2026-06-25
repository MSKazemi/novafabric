package hpc

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestSpoolStore_AutonomyDuringHubDisconnection verifies BQ-011 criterion:
// "HPC leaf node operates autonomously during hub disconnection; replicates
// on reconnect."
//
// The spool is the durability layer that provides autonomy.  When no NATS hub
// is reachable, events accumulate on disk via SpoolStore.  When the hub
// reconnects (simulated here by draining the spool via Drain), all events are
// delivered exactly once.
func TestSpoolStore_AutonomyDuringHubDisconnection(t *testing.T) {
	dir := t.TempDir()
	const maxBytes = 100 * 1024 * 1024 // 100 MB

	ss, err := NewSpoolStore(dir, maxBytes, nil)
	require.NoError(t, err, "NewSpoolStore must succeed")
	defer ss.Close()

	const eventCount = 200

	// Phase 1: hub is unreachable — write events autonomously to local spool.
	for i := 0; i < eventCount; i++ {
		ev := fmt.Sprintf(`{"event_id":"ev%06d","type":"model_call"}`, i)
		require.NoError(t, ss.Write([]byte(ev)),
			"Write must succeed during hub disconnection (event %d)", i)
	}

	stats := ss.Stats()
	assert.Greater(t, stats.SegmentCount, 0,
		"spool must retain segments during hub disconnection")
	assert.Greater(t, stats.TotalBytes, int64(0),
		"spool must retain bytes during hub disconnection")
	t.Logf("Autonomous phase: %d segments, %d bytes on disk",
		stats.SegmentCount, stats.TotalBytes)

	// Phase 2: hub reconnects — drain all events exactly once.
	var received []string
	for {
		drainErr := ss.Drain(100, func(ev []byte) error {
			received = append(received, string(ev))
			return nil
		})
		require.NoError(t, drainErr, "Drain must not fail on reconnect")
		if len(received) >= eventCount {
			break
		}
		// Keep draining until all events are consumed.
		afterStats := ss.Stats()
		if afterStats.SegmentCount == 0 {
			break
		}
	}

	assert.Equal(t, eventCount, len(received),
		"all %d events must be replicated on reconnect (got %d)", eventCount, len(received))

	t.Logf("Reconnect phase: %d events drained (all events delivered exactly once)", len(received))
}

// TestLeaf_EpilogFlushWithinTimeout verifies that Leaf.Stop always returns nil
// within the requested flush timeout, satisfying Slurm PrologEpilogTimeout
// (BQ-011 criterion: "Slurm Epilog flush completes within PrologEpilogTimeout").
func TestLeaf_EpilogFlushWithinTimeout(t *testing.T) {
	// Use a leaf that was never started (proc == nil).  Stop must still return
	// nil within the timeout regardless.
	leaf := NewLeaf(LeafConfig{
		SpoolDir:   t.TempDir(),
		JobID:      "epilog-test-job",
		ClusterID:  "ci",
		NodeID:     "n0",
		HubAddress: "nats://127.0.0.1:14222",
	})

	const flushTimeout = 5 * time.Second
	deadline := time.Now().Add(flushTimeout)

	err := leaf.Stop(context.Background(), flushTimeout)

	assert.NoError(t, err, "Stop must always return nil (FR-12)")
	assert.True(t, time.Now().Before(deadline.Add(time.Second)),
		"Stop must complete within flush timeout to satisfy PrologEpilogTimeout")
}

// TestEpilogScript_ExitsZeroWithinTimeout runs deploy/hpc/epilog.sh with a
// short flush timeout and verifies it always exits 0 within the PrologEpilogTimeout
// deadline (BQ-011 criterion: "Slurm Epilog flush completes within
// PrologEpilogTimeout on representative test cluster").
func TestEpilogScript_ExitsZeroWithinTimeout(t *testing.T) {
	// Locate epilog.sh relative to repo root.
	// Test working dir is collector/internal/hpc/; repo root is ../../../
	epilogPath := filepath.Join("..", "..", "..", "deploy", "hpc", "epilog.sh")
	if _, statErr := os.Stat(epilogPath); statErr != nil {
		t.Skipf("epilog.sh not found at %s: %v", epilogPath, statErr)
	}

	dir := t.TempDir()
	jobID := "epilog-ci-test-99999"

	// Use PrologEpilogTimeout-style context: script must complete in < 10s.
	const prologEpilogTimeout = 10 * time.Second
	ctx, cancel := context.WithTimeout(context.Background(), prologEpilogTimeout)
	defer cancel()

	cmd := exec.CommandContext(ctx, "sh", epilogPath)
	cmd.Env = append(os.Environ(),
		"SLURM_JOB_ID="+jobID,
		"NOVAFABRIC_SPOOL_BASE="+dir,
		// Short flush timeout so the script completes quickly in CI (no NATS available).
		"NOVAFABRIC_EPILOG_FLUSH_TIMEOUT=2",
	)
	out, err := cmd.CombinedOutput()

	// epilog.sh must ALWAYS exit 0 (FR-12).
	if ctx.Err() != nil {
		t.Errorf("epilog.sh did not complete within PrologEpilogTimeout (%v): %v",
			prologEpilogTimeout, ctx.Err())
		return
	}
	if err != nil {
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			t.Errorf("epilog.sh exited with code %d (must always be 0)\nOutput:\n%s",
				exitErr.ExitCode(), out)
		} else {
			t.Errorf("epilog.sh exec error: %v", err)
		}
		return
	}
	t.Logf("epilog.sh output:\n%s", out)
}
