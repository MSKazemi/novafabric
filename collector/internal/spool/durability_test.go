package spool

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// Review go-collector-ts-packages-16: a rename is durable only once its directory is
// fsynced, and recovery deleted every old .tmp — including a completed segment whose rename
// the power loss had not persisted.

func recordSyncDir(t *testing.T) *[]string {
	t.Helper()
	var dirs []string
	prev := syncDirFn
	syncDirFn = func(dir string) error { dirs = append(dirs, dir); return prev(dir) }
	t.Cleanup(func() { syncDirFn = prev })
	return &dirs
}

func TestSegmentCommitFsyncsTheDirectoryAfterTheRename(t *testing.T) {
	dir := t.TempDir()
	synced := recordSyncDir(t)
	s, err := NewSpool(dir, 1<<30, &noopMetrics{})
	require.NoError(t, err)
	*synced = nil

	require.NoError(t, s.Write([]byte(`{"e":1}`)))

	assert.Contains(t, *synced, dir, "the rename of <seq>.jsonl.tmp must be followed by a directory fsync")
}

func TestCheckpointAdvanceFsyncsTheDirectoryAfterTheRename(t *testing.T) {
	dir := t.TempDir()
	s, err := NewSpool(dir, 1<<30, &noopMetrics{})
	require.NoError(t, err)
	require.NoError(t, s.Write([]byte(`{"e":1}`)))
	_, err = s.ReadBatch(1)
	require.NoError(t, err)
	synced := recordSyncDir(t)

	require.NoError(t, s.Commit(1))

	assert.Contains(t, *synced, dir)
}

// A complete temp segment (WriteComplete=1, fsynced) whose rename was not persisted.
func completeTmp(t *testing.T, dir string, seq uint64, event string) string {
	t.Helper()
	sw, err := newSegmentWriter(dir, seq)
	require.NoError(t, err)
	require.NoError(t, sw.writeEvent([]byte(event)))
	_, err = sw.file.Seek(12, 0)
	require.NoError(t, err)
	_, err = sw.file.Write([]byte{1})
	require.NoError(t, err)
	require.NoError(t, sw.file.Sync())
	require.NoError(t, sw.file.Close())
	old := time.Now().Add(-2 * tmpGrace)
	require.NoError(t, os.Chtimes(sw.tmpPath, old, old))
	return sw.tmpPath
}

func TestRecoveryPromotesACompletedTempSegment(t *testing.T) {
	dir := t.TempDir()
	tmp := completeTmp(t, dir, 7, `{"survivor":1}`)

	segments, err := recoverSegments(dir)
	require.NoError(t, err)

	require.Len(t, segments, 1, "a segment that finished writing must not be deleted")
	assert.Equal(t, filepath.Join(dir, "00000000000000000007.jsonl"), segments[0])
	_, statErr := os.Stat(tmp)
	assert.True(t, os.IsNotExist(statErr))

	s, err := NewSpool(dir, 1<<30, &noopMetrics{})
	require.NoError(t, err)
	got := drain(t, s, 10)
	require.Len(t, got, 1)
	assert.JSONEq(t, `{"survivor":1}`, string(got[0]))
}

func TestRecoveryStillDiscardsAnIncompleteTempSegment(t *testing.T) {
	dir := t.TempDir()
	sw, err := newSegmentWriter(dir, 3) // header says WriteComplete=0
	require.NoError(t, err)
	require.NoError(t, sw.writeEvent([]byte(`{"half":1}`)))
	require.NoError(t, sw.file.Close())
	old := time.Now().Add(-2 * tmpGrace)
	require.NoError(t, os.Chtimes(sw.tmpPath, old, old))

	segments, err := recoverSegments(dir)
	require.NoError(t, err)

	assert.Empty(t, segments)
	_, statErr := os.Stat(sw.tmpPath)
	assert.True(t, os.IsNotExist(statErr))
}

func TestRecoveryDoesNotPromoteOverAnExistingSegment(t *testing.T) {
	dir := t.TempDir()
	s, err := NewSpool(dir, 1<<30, &noopMetrics{})
	require.NoError(t, err)
	require.NoError(t, s.Write([]byte(`{"real":1}`)))
	tmp := completeTmp(t, dir, 0, `{"stale":1}`) // same sequence as the committed one

	segments, err := recoverSegments(dir)
	require.NoError(t, err)

	require.Len(t, segments, 1)
	raw, err := os.ReadFile(segments[0])
	require.NoError(t, err)
	assert.Contains(t, string(raw), `"real":1`, "the committed segment must not be replaced")
	_, statErr := os.Stat(tmp)
	assert.True(t, os.IsNotExist(statErr))
}
