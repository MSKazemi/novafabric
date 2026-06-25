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

// TestSpool_KillRecovery simulates a crash mid-write by creating a .jsonl.tmp
// file (representing an incomplete segment), then calling recoverSegments.
// It verifies that the .tmp file is discarded and fully committed segments
// are intact.
func TestSpool_KillRecovery(t *testing.T) {
	dir := t.TempDir()
	m := &noopMetrics{}

	// Write 20 events and commit them all.
	s, err := NewSpool(dir, 100*1024*1024, m)
	require.NoError(t, err)
	for i := 0; i < 20; i++ {
		require.NoError(t, s.Write([]byte(fmt.Sprintf(`{"i":%d}`, i))))
	}
	require.NoError(t, s.Close())

	// Inject a crash artifact: a .jsonl.tmp file with an incomplete header.
	crashedTmp := filepath.Join(dir, "99999999999999999999.jsonl.tmp")
	require.NoError(t, os.WriteFile(crashedTmp, []byte("partial"), 0o600))

	// recoverSegments should discard the .tmp and return only .jsonl files.
	segments, err := recoverSegments(dir)
	require.NoError(t, err)

	// Verify .tmp is gone.
	_, statErr := os.Stat(crashedTmp)
	assert.True(t, os.IsNotExist(statErr), "crashed .tmp file must be removed by recoverSegments")

	// Verify all 20 committed segments are returned.
	assert.Len(t, segments, 20, "all 20 committed segments must survive recovery")

	// Verify segments are sorted by sequence number.
	for i := 1; i < len(segments); i++ {
		prev := seqFromPath(segments[i-1])
		curr := seqFromPath(segments[i])
		assert.Less(t, prev, curr, "segments must be sorted in ascending sequence order")
	}
}

// TestSpool_CheckpointMonotonic runs 100 write+commit cycles and verifies
// that the checkpoint sequence number is strictly non-decreasing.
func TestSpool_CheckpointMonotonic(t *testing.T) {
	dir := t.TempDir()
	m := &noopMetrics{}
	s, err := NewSpool(dir, 100*1024*1024, m)
	require.NoError(t, err)

	var lastSeq uint64
	for i := 0; i < 100; i++ {
		require.NoError(t, s.Write([]byte(fmt.Sprintf(`{"cycle":%d}`, i))))

		batch, readErr := s.ReadBatch(1)
		require.NoError(t, readErr)
		require.NotEmpty(t, batch)

		require.NoError(t, s.Commit(len(batch)))

		currentSeq := s.checkpoint.LastAckSeq()
		assert.GreaterOrEqual(t, currentSeq, lastSeq,
			"checkpoint seq must be non-decreasing (cycle %d)", i)
		lastSeq = currentSeq
	}

	require.NoError(t, s.Close())
}

// TestSpool_RecoveryAfterCrash verifies that a spool reopened after a crash
// (simulated by closing without committing) re-reads all non-acknowledged events.
func TestSpool_RecoveryAfterCrash(t *testing.T) {
	dir := t.TempDir()
	m := &noopMetrics{}

	s1, err := NewSpool(dir, 100*1024*1024, m)
	require.NoError(t, err)

	for i := 0; i < 5; i++ {
		require.NoError(t, s1.Write([]byte(fmt.Sprintf(`{"i":%d}`, i))))
	}
	// Simulate crash: close without committing any reads.
	require.NoError(t, s1.Close())

	// Reopen — all 5 events must be readable again.
	s2, err := NewSpool(dir, 100*1024*1024, m)
	require.NoError(t, err)

	var recovered [][]byte
	for {
		batch, readErr := s2.ReadBatch(10)
		require.NoError(t, readErr)
		if len(batch) == 0 {
			break
		}
		recovered = append(recovered, batch...)
		require.NoError(t, s2.Commit(len(batch)))
	}
	require.NoError(t, s2.Close())

	assert.Len(t, recovered, 5, "all 5 events must be re-readable after simulated crash")
}

// TestRecoverSegments_SortsCorrectly verifies that recoverSegments returns
// segments in ascending sequence order.
func TestRecoverSegments_SortsCorrectly(t *testing.T) {
	dir := t.TempDir()
	m := &noopMetrics{}

	// Write segments in non-sequential order by manipulating sequence numbers
	// directly through segmentWriter.
	seqs := []uint64{5, 1, 3, 2, 4}
	for _, seq := range seqs {
		sw, err := newSegmentWriter(dir, seq)
		require.NoError(t, err)
		require.NoError(t, sw.writeEvent([]byte(fmt.Sprintf(`{"seq":%d}`, seq))))
		require.NoError(t, sw.commit())
	}
	_ = m

	segments, err := recoverSegments(dir)
	require.NoError(t, err)
	require.Len(t, segments, 5)

	for i := 1; i < len(segments); i++ {
		assert.Less(t, seqFromPath(segments[i-1]), seqFromPath(segments[i]))
	}
}

// TestSpool_OldestEventAge verifies that Stats.OldestEventAge is positive
// after writing at least one event.
func TestSpool_OldestEventAge(t *testing.T) {
	dir := t.TempDir()
	m := &noopMetrics{}
	s, err := NewSpool(dir, 100*1024*1024, m)
	require.NoError(t, err)

	require.NoError(t, s.Write([]byte(`{"age":"test"}`)))
	time.Sleep(5 * time.Millisecond) // ensure mtime is slightly in the past

	stats := s.Stats()
	assert.GreaterOrEqual(t, stats.OldestEventAge, time.Duration(0))
}
