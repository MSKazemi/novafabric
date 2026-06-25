package hpc

import (
	"errors"
	"fmt"
	"os"
	"testing"

	"github.com/novafabric/collector/internal/spool"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestSpoolStore_DLQActivatedFromEnv verifies G-A3: when NOVA_DLQ_DIR is set,
// events dropped under backpressure are written to the DLQ file instead of
// being silently lost.
func TestSpoolStore_DLQActivatedFromEnv(t *testing.T) {
	dlqDir := t.TempDir()
	spoolDir := t.TempDir()

	t.Setenv(envDLQDir, dlqDir)

	// Use a very small spool (1 KB) so backpressure triggers quickly.
	const maxBytes = 1024
	ss, err := NewSpoolStore(spoolDir, maxBytes, nil)
	require.NoError(t, err, "NewSpoolStore must succeed")
	defer ss.Close()

	// DLQDir must reflect the configured directory.
	assert.Equal(t, dlqDir, ss.DLQDir(), "DLQDir() must return the configured NOVA_DLQ_DIR")

	// Write events until we get backpressure from the spool.
	const eventPayload = `{"event_id":"dlq-test","type":"model_call","payload":"` +
		"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" +
		"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" +
		`"}`
	var backpressureHit bool
	for i := 0; i < 500; i++ {
		ev := fmt.Sprintf(`{"event_id":"ev%06d","type":"model_call","seq":%d}`, i, i)
		writeErr := ss.Write([]byte(ev))
		if errors.Is(writeErr, spool.ErrBackpressure) {
			backpressureHit = true
			// Write a few more to ensure DLQ has content.
			for j := 0; j < 5; j++ {
				extra := fmt.Sprintf(`{"event_id":"dlq%06d","type":"model_call"}`, j)
				_ = ss.Write([]byte(extra))
			}
			break
		}
	}
	// If backpressure was never hit with the default payload, push larger events.
	if !backpressureHit {
		for i := 0; i < 100; i++ {
			writeErr := ss.Write([]byte(eventPayload))
			if errors.Is(writeErr, spool.ErrBackpressure) {
				backpressureHit = true
				for j := 0; j < 5; j++ {
					extra := fmt.Sprintf(`{"event_id":"dlq-big%06d","type":"model_call"}`, j)
					_ = ss.Write([]byte(extra))
				}
				break
			}
		}
	}

	require.True(t, backpressureHit, "spool must reach backpressure with maxBytes=%d", maxBytes)

	// The DLQ directory must now contain at least one non-empty file.
	entries, err := os.ReadDir(dlqDir)
	require.NoError(t, err, "DLQ directory must be readable")
	require.NotEmpty(t, entries, "DLQ directory must contain at least one file after backpressure")

	var totalSize int64
	for _, entry := range entries {
		info, infoErr := entry.Info()
		require.NoError(t, infoErr)
		totalSize += info.Size()
	}
	assert.Greater(t, totalSize, int64(0),
		"DLQ files must be non-empty after events were dropped under backpressure")

	t.Logf("DLQ: %d file(s), %d bytes total", len(entries), totalSize)
}

// TestSpoolStore_NoDLQWithoutEnv verifies that when NOVA_DLQ_DIR is not set,
// SpoolStore.DLQDir() returns an empty string and no DLQ is attached.
func TestSpoolStore_NoDLQWithoutEnv(t *testing.T) {
	// Ensure the env var is absent.
	t.Setenv(envDLQDir, "")

	spoolDir := t.TempDir()
	ss, err := NewSpoolStore(spoolDir, 10*1024*1024, nil)
	require.NoError(t, err, "NewSpoolStore must succeed without NOVA_DLQ_DIR")
	defer ss.Close()

	assert.Equal(t, "", ss.DLQDir(),
		"DLQDir() must return empty string when NOVA_DLQ_DIR is not set")
	assert.Nil(t, ss.dlq,
		"no DLQ must be attached when NOVA_DLQ_DIR is not set")
}
