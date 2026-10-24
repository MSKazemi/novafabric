package spool

import (
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// Review go-collector-ts-packages-04: the resident forwarder only drains; events
// come from other processes. Its segment list was fixed at NewSpool, so nothing
// written after it started was ever forwarded, and every restart deleted the
// .jsonl.tmp a live writer was about to rename.

func TestASegmentWrittenByAnotherProcessAfterOpenIsRead(t *testing.T) {
	dir := t.TempDir()
	reader, err := NewSpool(dir, 100*1024*1024, &noopMetrics{})
	require.NoError(t, err)
	batch, err := reader.ReadBatch(10)
	require.NoError(t, err)
	require.Empty(t, batch)

	// A second Spool on the same directory stands in for a capture process.
	writer, err := NewSpool(dir, 100*1024*1024, &noopMetrics{})
	require.NoError(t, err)
	for i := 0; i < 3; i++ {
		require.NoError(t, writer.Write([]byte(fmt.Sprintf(`{"i":%d}`, i))))
	}

	batch, err = reader.ReadBatch(10)
	require.NoError(t, err)
	assert.Len(t, batch, 3, "the reader must pick up segments committed after it opened")
	require.NoError(t, reader.Commit(len(batch)))

	// Nothing is read twice.
	batch, err = reader.ReadBatch(10)
	require.NoError(t, err)
	assert.Empty(t, batch)
}

func TestRecoveryKeepsAFreshTempFileAndRemovesAStaleOne(t *testing.T) {
	dir := t.TempDir()
	fresh := filepath.Join(dir, "00000000000000000007.123.jsonl.tmp")
	stale := filepath.Join(dir, "00000000000000000003.99.jsonl.tmp")
	require.NoError(t, os.WriteFile(fresh, []byte("in flight"), 0o600))
	require.NoError(t, os.WriteFile(stale, []byte("crashed"), 0o600))
	old := time.Now().Add(-2 * tmpGrace)
	require.NoError(t, os.Chtimes(stale, old, old))

	_, err := recoverSegments(dir)
	require.NoError(t, err)

	_, err = os.Stat(fresh)
	assert.NoError(t, err, "a live writer's temp file must survive a forwarder restart")
	_, err = os.Stat(stale)
	assert.True(t, os.IsNotExist(err), "a crashed writer's temp file is still discarded")
}
