package spool

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// Review go-collector-ts-packages-11: DLQ.Write returned nil on every failure and the spool
// counted nova_collector_dlq_events_total anyway, so a full or read-only DLQ disk looked like
// every event was preserved while each was dropped.

// brokenDLQ is a DLQ whose directory has been replaced by a regular file, so no file can be
// created in it.
func brokenDLQ(t *testing.T) *DLQ {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "dlq")
	d, err := NewDLQ(dir)
	require.NoError(t, err)
	require.NoError(t, os.RemoveAll(dir))
	require.NoError(t, os.WriteFile(dir, []byte("not a directory"), 0o600))
	return d
}

func TestDLQWriteReturnsItsFailure(t *testing.T) {
	err := brokenDLQ(t).Write([]byte(`{"e":1}`))
	require.Error(t, err, "a write that stored nothing must say so")
}

type dlqMetrics struct {
	noopMetrics
	stored, lost int
}

func (m *dlqMetrics) IncDLQEvents()  { m.stored++ }
func (m *dlqMetrics) IncDLQDropped() { m.lost++ }

func fullSpool(t *testing.T, m SpoolMetrics) *Spool {
	t.Helper()
	// A budget the first event already exceeds: every later Write hits backpressure.
	s, err := NewSpool(t.TempDir(), 1, m)
	require.NoError(t, err)
	_ = s.Write([]byte(`{"seed":1}`))
	return s
}

func TestSpoolCountsADLQEventOnlyWhenTheDLQStoredIt(t *testing.T) {
	m := &dlqMetrics{}
	s := fullSpool(t, m)
	s.SetDLQ(brokenDLQ(t))

	err := s.Write([]byte(`{"e":2}`))
	require.ErrorIs(t, err, ErrBackpressure)

	assert.Zero(t, m.stored, "an event the DLQ failed to store must not count as a DLQ event")
	assert.Equal(t, 1, m.lost, "it is a loss, and is counted as one")
}

func TestSpoolStillCountsAStoredDLQEvent(t *testing.T) {
	m := &dlqMetrics{}
	s := fullSpool(t, m)
	d, err := NewDLQ(filepath.Join(t.TempDir(), "dlq"))
	require.NoError(t, err)
	s.SetDLQ(d)

	require.ErrorIs(t, s.Write([]byte(`{"e":2}`)), ErrBackpressure)

	assert.Equal(t, 1, m.stored)
	assert.Zero(t, m.lost)
	size, err := d.Size()
	require.NoError(t, err)
	assert.Positive(t, size)
}

func TestDLQWriteIsDurable(t *testing.T) {
	d, err := NewDLQ(filepath.Join(t.TempDir(), "dlq"))
	require.NoError(t, err)
	require.NoError(t, d.Write([]byte(`{"e":3}`)))
	// Replay reads it back through a fresh handle.
	var got []string
	require.NoError(t, d.Replay(func(b []byte) error { got = append(got, string(b)); return nil }))
	assert.Equal(t, []string{`{"e":3}`}, got)
}
