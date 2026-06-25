package forwarder

import (
	"context"
	"errors"
	"fmt"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/novafabric/collector/internal/hpc"
)

// fakePublisher records every published event and can be made to fail.
type fakePublisher struct {
	published []pub
	failAt    int // 1-based index at which Publish returns an error; 0 = never
	calls     int
}

type pub struct {
	subject string
	event   []byte
}

func (f *fakePublisher) Publish(subject string, eventJSON []byte) error {
	f.calls++
	if f.failAt != 0 && f.calls >= f.failAt {
		return errors.New("publish boom")
	}
	cp := make([]byte, len(eventJSON))
	copy(cp, eventJSON)
	f.published = append(f.published, pub{subject: subject, event: cp})
	return nil
}

func openStore(t *testing.T, dir string) *hpc.SpoolStore {
	t.Helper()
	ss, err := hpc.NewSpoolStore(dir, 100*1024*1024, nil)
	require.NoError(t, err)
	return ss
}

func newStore(t *testing.T) *hpc.SpoolStore {
	t.Helper()
	ss := openStore(t, t.TempDir())
	t.Cleanup(func() { _ = ss.Close() })
	return ss
}

func writeEnvelopes(t *testing.T, ss *hpc.SpoolStore, runID string, n int) {
	t.Helper()
	for i := 0; i < n; i++ {
		ev := fmt.Sprintf(`{"run_id":%q,"event_type":"model_call","seq":%d}`, runID, i)
		require.NoError(t, ss.Write([]byte(ev)))
	}
}

func TestForwarder_DrainBatch_PublishesAndCommitsExactlyOnce(t *testing.T) {
	ss := newStore(t)
	writeEnvelopes(t, ss, "01HRESIDENT0000000000000AA", 3)

	pubr := &fakePublisher{}
	fwd := New(ss, pubr, "nova.evidence")

	require.NoError(t, fwd.DrainBatch(10))
	require.Len(t, pubr.published, 3)
	for _, p := range pubr.published {
		assert.Equal(t, "nova.evidence.01HRESIDENT0000000000000AA", p.subject)
	}

	// Committed: a second drain forwards nothing (exactly-once).
	require.NoError(t, fwd.DrainBatch(10))
	assert.Len(t, pubr.published, 3)
}

func TestForwarder_RestartReReadsUncommittedBatchOnPublishFailure(t *testing.T) {
	dir := t.TempDir()
	ss := openStore(t, dir)
	writeEnvelopes(t, ss, "01HRESIDENT0000000000000BB", 3)

	// 2nd publish fails → batch not committed; the drain loop would now exit.
	failing := &fakePublisher{failAt: 2}
	require.Error(t, New(ss, failing, "nova.evidence").DrainBatch(10))
	require.NoError(t, ss.Close()) // simulate the forwarder process exiting

	// Restart: a freshly opened spool resumes from the persisted checkpoint
	// (unadvanced) and re-reads all 3 uncommitted events — no loss.
	ss2 := openStore(t, dir)
	t.Cleanup(func() { _ = ss2.Close() })
	good := &fakePublisher{}
	require.NoError(t, New(ss2, good, "nova.evidence").DrainBatch(10))
	assert.Len(t, good.published, 3)
}

func TestForwarder_DrainBatch_EmptySpoolIsNoOp(t *testing.T) {
	pubr := &fakePublisher{}
	require.NoError(t, New(newStore(t), pubr, "nova.evidence").DrainBatch(10))
	assert.Empty(t, pubr.published)
}

func TestForwarder_Run_DrainsThenStopsOnContextCancel(t *testing.T) {
	ss := newStore(t)
	writeEnvelopes(t, ss, "01HRESIDENT0000000000000CC", 3)
	pubr := &fakePublisher{}

	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	// Graceful shutdown returns nil; the 3 events drain in the first iteration.
	require.NoError(t, New(ss, pubr, "nova.evidence").Run(ctx, time.Millisecond, 10))
	assert.Len(t, pubr.published, 3)
}

func TestForwarder_Run_ExitsOnDrainError(t *testing.T) {
	ss := newStore(t)
	writeEnvelopes(t, ss, "01HRESIDENT0000000000000DD", 2)
	failing := &fakePublisher{failAt: 1}

	// A publish failure is fatal: Run returns the error (not nil) so the
	// supervisor restarts the process and the uncommitted batch is re-read.
	err := New(ss, failing, "nova.evidence").Run(context.Background(), time.Millisecond, 10)
	require.Error(t, err)
}
