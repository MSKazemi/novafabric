package spool

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func segmentFiles(t *testing.T, dir string) []string {
	t.Helper()
	entries, err := os.ReadDir(dir)
	require.NoError(t, err)
	var names []string
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".jsonl") {
			names = append(names, e.Name())
		}
	}
	return names
}

func drain(t *testing.T, s *Spool, batch int) [][]byte {
	t.Helper()
	var all [][]byte
	for {
		got, err := s.ReadBatch(batch)
		require.NoError(t, err)
		if len(got) == 0 {
			return all
		}
		all = append(all, got...)
		require.NoError(t, s.Commit(len(got)))
	}
}

// Review go-collector-ts-packages-05: Commit only advanced the checkpoint, so every
// acknowledged segment stayed on disk (one file per event) until the directory reached
// max-bytes, every Write scanned all of them, and the in-memory segment list never shrank.

func TestCommitDeletesAcknowledgedSegments(t *testing.T) {
	m := &noopMetrics{}
	s, err := NewSpool(t.TempDir(), 1<<30, m)
	require.NoError(t, err)
	for i := 0; i < 200; i++ {
		require.NoError(t, s.Write([]byte(fmt.Sprintf(`{"i":%d}`, i))))
	}

	assert.Len(t, drain(t, s, 64), 200)

	assert.Empty(t, segmentFiles(t, s.dir), "acknowledged segments must be unlinked")
	_, err = os.Stat(filepath.Join(s.dir, "checkpoint.json"))
	assert.NoError(t, err, "the checkpoint must survive")
	assert.Zero(t, m.dropped, "routine housekeeping is not a dropped segment")
	assert.LessOrEqual(t, len(s.readSegments), 1, "the in-memory list must not grow with acknowledged history")
}

func TestCommitKeepsTheUnacknowledgedSegments(t *testing.T) {
	dir := t.TempDir()
	s, err := NewSpool(dir, 1<<30, &noopMetrics{})
	require.NoError(t, err)
	for i := 0; i < 50; i++ {
		require.NoError(t, s.Write([]byte(fmt.Sprintf(`{"i":%d}`, i))))
	}

	got, err := s.ReadBatch(10)
	require.NoError(t, err)
	require.Len(t, got, 10)
	require.NoError(t, s.Commit(10))
	assert.Len(t, segmentFiles(t, dir), 40)

	// A restarted forwarder sees exactly the remaining 40 events, in order.
	s2, err := NewSpool(dir, 1<<30, &noopMetrics{})
	require.NoError(t, err)
	rest := drain(t, s2, 100)
	require.Len(t, rest, 40)
	assert.Contains(t, string(rest[0]), `"i":10`)
	assert.Contains(t, string(rest[39]), `"i":49`)
}

func TestAnUncommittedBatchIsNotDeleted(t *testing.T) {
	dir := t.TempDir()
	s, err := NewSpool(dir, 1<<30, &noopMetrics{})
	require.NoError(t, err)
	for i := 0; i < 5; i++ {
		require.NoError(t, s.Write([]byte(fmt.Sprintf(`{"i":%d}`, i))))
	}
	_, err = s.ReadBatch(5) // read, never committed (e.g. the publish failed)
	require.NoError(t, err)
	assert.Len(t, segmentFiles(t, dir), 5, "events read but not acknowledged must stay on disk")
}

func TestWritesDoNotSlowDownWithHistory(t *testing.T) {
	s, err := NewSpool(t.TempDir(), 1<<30, &noopMetrics{})
	require.NoError(t, err)
	for round := 0; round < 3; round++ {
		for i := 0; i < 120; i++ {
			require.NoError(t, s.Write([]byte(`{"x":1}`)))
		}
		drain(t, s, 128)
		assert.Empty(t, segmentFiles(t, s.dir), "round %d left acknowledged segments behind", round)
	}
}

// Review go-collector-ts-packages-06: a line over bufio.Scanner's 64 KiB default made the
// whole segment unreadable, and the checkpoint then advanced past it with no counter.

func TestAnEventOver64KiBIsDrainedIntact(t *testing.T) {
	s, err := NewSpool(t.TempDir(), 1<<30, &noopMetrics{})
	require.NoError(t, err)
	big := []byte(`{"payload":"` + strings.Repeat("a", 100<<10) + `"}`)
	require.NoError(t, s.Write(big))
	require.NoError(t, s.Write([]byte(`{"after":1}`)))

	got := drain(t, s, 10)
	require.Len(t, got, 2)
	assert.True(t, bytes.Equal(big, got[0]), "the large event must come back byte for byte")
}

func TestWriteRejectsAnEventAboveTheLimit(t *testing.T) {
	dir := t.TempDir()
	s, err := NewSpool(dir, 1<<30, &noopMetrics{})
	require.NoError(t, err)
	tooBig := bytes.Repeat([]byte("a"), MaxEventBytes+1)
	require.ErrorIs(t, s.Write(tooBig), ErrEventTooLarge)
	assert.Empty(t, segmentFiles(t, dir), "a rejected event leaves nothing behind")

	atLimit := bytes.Repeat([]byte("a"), MaxEventBytes)
	require.NoError(t, s.Write(atLimit))
	got := drain(t, s, 1)
	require.Len(t, got, 1)
	assert.Len(t, got[0], MaxEventBytes)
}

func TestASkippedUnreadableSegmentIsCounted(t *testing.T) {
	dir := t.TempDir()
	m := &noopMetrics{}
	s, err := NewSpool(dir, 1<<30, m)
	require.NoError(t, err)
	require.NoError(t, s.Write([]byte(`{"ok":1}`)))
	// A committed-looking name whose content is not a segment.
	require.NoError(t, os.WriteFile(filepath.Join(dir, fmt.Sprintf("%020d.jsonl", 99)), []byte("garbage"), 0o640))

	_ = drain(t, s, 10)

	assert.Positive(t, m.dropped, "skipping an unreadable segment must be counted, not only logged")
}

func TestDLQReplaysAnEventOver64KiB(t *testing.T) {
	d, err := NewDLQ(filepath.Join(t.TempDir(), "dlq"))
	require.NoError(t, err)
	big := `{"payload":"` + strings.Repeat("b", 100<<10) + `"}`
	require.NoError(t, d.Write([]byte(big)))
	require.NoError(t, d.Write([]byte(`{"next":1}`)))

	var got []string
	require.NoError(t, d.Replay(func(b []byte) error { got = append(got, string(b)); return nil }))
	require.Len(t, got, 2)
	assert.Equal(t, big, got[0])
}
