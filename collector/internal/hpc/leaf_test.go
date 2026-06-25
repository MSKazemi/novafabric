package hpc

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestIsSpoolMode verifies the NOVAFABRIC_HPC_STORAGE environment variable
// controls spool mode.
func TestIsSpoolMode(t *testing.T) {
	// Ensure clean state.
	t.Setenv(envSpoolMode, "")
	assert.False(t, IsSpoolMode(), "IsSpoolMode must return false when env var is empty")

	t.Setenv(envSpoolMode, "spool")
	assert.True(t, IsSpoolMode(), "IsSpoolMode must return true when env var is non-empty")

	t.Setenv(envSpoolMode, "1")
	assert.True(t, IsSpoolMode(), "IsSpoolMode must return true for any non-empty value")
}

// TestLeaf_StartStop starts a Leaf, verifies the spool dir is created, then
// stops it.  This test skips automatically when nats-server is not on PATH so
// that it can run in CI without a NATS binary.
func TestLeaf_StartStop(t *testing.T) {
	if _, err := lookupNATSServer(); err != nil {
		t.Skip("nats-server not found in PATH — skipping integration test")
	}

	dir := t.TempDir()
	spoolDir := filepath.Join(dir, "spool")

	leaf := NewLeaf(LeafConfig{
		SpoolDir:   spoolDir,
		JobID:      "test-job-1",
		ClusterID:  "ci-cluster",
		NodeID:     "node-0",
		HubAddress: "nats://127.0.0.1:14222", // non-listening port; leaf will fail to connect but that's OK for lifecycle test
	})

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	err := leaf.Start(ctx)
	require.NoError(t, err, "Start must succeed when nats-server is available")

	// Verify spool dir was created.
	_, statErr := os.Stat(spoolDir)
	assert.NoError(t, statErr, "spool dir must exist after Start")

	// Verify NATS config file was written.
	cfgPath := filepath.Join(spoolDir, "nats-leaf.conf")
	_, cfgErr := os.Stat(cfgPath)
	assert.NoError(t, cfgErr, "nats-leaf.conf must exist after Start")

	// Stop must always return nil (FR-12).
	stopErr := leaf.Stop(ctx, 2*time.Second)
	assert.NoError(t, stopErr, "Stop must always return nil (FR-12)")
}

// TestLeaf_SpoolDirCreated verifies that Start creates the spool directory
// even when it did not exist before.
func TestLeaf_SpoolDirCreated(t *testing.T) {
	dir := t.TempDir()
	nestedSpoolDir := filepath.Join(dir, "a", "b", "c", "spool")

	leaf := NewLeaf(LeafConfig{
		SpoolDir:   nestedSpoolDir,
		JobID:      "job-nested",
		ClusterID:  "ci",
		NodeID:     "n0",
		HubAddress: "nats://127.0.0.1:14222",
	})

	// We don't actually start nats-server; we only want to verify the dir
	// creation side effect.  Use a cancelled context so Start returns quickly
	// after creating dirs (it will fail at nats-server lookup or start, but
	// the dirs should already exist).
	ctx := context.Background()
	_ = leaf.Start(ctx) // ignore error — nats-server may not be present

	_, statErr := os.Stat(nestedSpoolDir)
	assert.NoError(t, statErr, "nested spool dir must be created by Start via os.MkdirAll")
}

// TestLeaf_NATSConfigContent verifies that the rendered NATS config contains
// the expected node ID and hub address.
func TestLeaf_NATSConfigContent(t *testing.T) {
	dir := t.TempDir()
	spoolDir := filepath.Join(dir, "spool")

	leaf := NewLeaf(LeafConfig{
		SpoolDir:   spoolDir,
		JobID:      "job-cfg",
		ClusterID:  "ci",
		NodeID:     "node-42",
		HubAddress: "nats://hub.example.com:4222",
	})

	// Trigger directory + config creation without starting nats-server.
	_ = os.MkdirAll(spoolDir, 0o750)
	cfgPath := filepath.Join(spoolDir, "nats-leaf.conf")
	err := leaf.writeNATSConfig(cfgPath)
	require.NoError(t, err)

	raw, err := os.ReadFile(cfgPath)
	require.NoError(t, err)

	content := string(raw)
	assert.Contains(t, content, "node-42", "config must contain the node ID")
	assert.Contains(t, content, "nats://hub.example.com:4222", "config must contain the hub address")
}

// TestLeaf_StopWithNilProc verifies that Stop is safe to call when the
// underlying process was never started.
func TestLeaf_StopWithNilProc(t *testing.T) {
	leaf := NewLeaf(LeafConfig{
		SpoolDir:   t.TempDir(),
		JobID:      "no-start",
		ClusterID:  "ci",
		NodeID:     "n0",
		HubAddress: "nats://127.0.0.1:14222",
	})

	err := leaf.Stop(context.Background(), time.Second)
	assert.NoError(t, err, "Stop must be safe to call before Start")
}

// lookupNATSServer is a helper that checks whether nats-server is on PATH.
func lookupNATSServer() (string, error) {
	// We import os/exec only via the hpc package itself; here we call os.
	path, err := os.Stat("/dev/null") // just to use os
	_ = path
	_ = err
	// Use exec.LookPath indirectly via the leaf package import.
	// Since this is an internal test we can call the OS directly.
	for _, dir := range filepath.SplitList(os.Getenv("PATH")) {
		candidate := filepath.Join(dir, "nats-server")
		if _, statErr := os.Stat(candidate); statErr == nil {
			return candidate, nil
		}
	}
	return "", os.ErrNotExist
}
