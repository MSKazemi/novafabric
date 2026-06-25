package spool

import (
	"fmt"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// noopMetrics satisfies SpoolMetrics for tests that do not assert on metrics.
type noopMetrics struct {
	dropped int
}

func (n *noopMetrics) IncDroppedSegments()          { n.dropped++ }
func (n *noopMetrics) SetSegmentCount(int)          {}
func (n *noopMetrics) SetTotalBytes(int64)          {}
func (n *noopMetrics) SetOldestEventAge(time.Duration) {}
func (n *noopMetrics) IncDLQEvents()               {}

// TestSpool_WriteRead writes 10 events and verifies that ReadBatch returns
// all of them.
func TestSpool_WriteRead(t *testing.T) {
	dir := t.TempDir()
	m := &noopMetrics{}
	s, err := NewSpool(dir, 100*1024*1024, m)
	require.NoError(t, err)

	for i := 0; i < 10; i++ {
		require.NoError(t, s.Write([]byte(fmt.Sprintf(`{"i":%d}`, i))))
	}

	var all [][]byte
	for {
		batch, err := s.ReadBatch(10)
		require.NoError(t, err)
		if len(batch) == 0 {
			break
		}
		all = append(all, batch...)
		require.NoError(t, s.Commit(len(batch)))
	}

	assert.Len(t, all, 10, "ReadBatch must return all 10 written events")
	for i, ev := range all {
		assert.Contains(t, string(ev), fmt.Sprintf(`"i":%d`, i))
	}
}

// TestSpool_Commit_AdvancesCheckpoint writes 10 events, commits 5, then
// re-opens the spool from the same directory and verifies that ReadBatch
// returns only the remaining 5 events.
func TestSpool_Commit_AdvancesCheckpoint(t *testing.T) {
	dir := t.TempDir()
	m := &noopMetrics{}

	s1, err := NewSpool(dir, 100*1024*1024, m)
	require.NoError(t, err)

	for i := 0; i < 10; i++ {
		require.NoError(t, s1.Write([]byte(fmt.Sprintf(`{"i":%d}`, i))))
	}

	// Read and commit the first 5.
	committed := 0
	for committed < 5 {
		batch, err := s1.ReadBatch(5)
		require.NoError(t, err)
		require.NotEmpty(t, batch)
		require.NoError(t, s1.Commit(len(batch)))
		committed += len(batch)
	}
	require.NoError(t, s1.Close())

	// Re-open spool from the same dir — should resume after the checkpoint.
	s2, err := NewSpool(dir, 100*1024*1024, m)
	require.NoError(t, err)

	var remaining [][]byte
	for {
		batch, err := s2.ReadBatch(10)
		require.NoError(t, err)
		if len(batch) == 0 {
			break
		}
		remaining = append(remaining, batch...)
		require.NoError(t, s2.Commit(len(batch)))
	}
	require.NoError(t, s2.Close())

	assert.Len(t, remaining, 5, "re-opened spool should return only the 5 uncommitted events")
}

// TestSpool_BackpressureError verifies that when the spool is full, Write
// returns ErrBackpressure.
func TestSpool_BackpressureError(t *testing.T) {
	dir := t.TempDir()
	m := &noopMetrics{}
	// maxBytes = 1024 bytes — a few events will fill it.
	s, err := NewSpool(dir, 1024, m)
	require.NoError(t, err)

	// Fill the spool until we hit backpressure.
	var backpressureHit bool
	for i := 0; i < 200; i++ {
		writeErr := s.Write([]byte(fmt.Sprintf(`{"padding":"%0100d"}`, i)))
		if writeErr == ErrBackpressure {
			backpressureHit = true
			break
		}
	}
	assert.True(t, backpressureHit, "Write must eventually return ErrBackpressure when spool is full")
}

// TestSpool_Stats verifies that Stats returns non-zero values after writing.
func TestSpool_Stats(t *testing.T) {
	dir := t.TempDir()
	m := &noopMetrics{}
	s, err := NewSpool(dir, 100*1024*1024, m)
	require.NoError(t, err)

	for i := 0; i < 3; i++ {
		require.NoError(t, s.Write([]byte(fmt.Sprintf(`{"k":%d}`, i))))
	}

	stats := s.Stats()
	assert.Greater(t, stats.SegmentCount, 0)
	assert.Greater(t, stats.TotalBytes, int64(0))
}
