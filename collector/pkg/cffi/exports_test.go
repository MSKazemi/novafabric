//go:build cgo

package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"testing"
)

// Review go-collector-ts-packages-03: libnovaspool.so is loaded into every Python
// capture process, and its sequence counter started at 0 in each one, so run B
// silently replaced run A's undrained segment 0, and every event written after the
// forwarder had acknowledged up to K reused seqs 0..K — skipped and evicted as
// "already acknowledged". These tests reset the counter the way a new process does.

func newProcess() { spoolSeq = 0 }

func segments(t *testing.T, dir string) []string {
	t.Helper()
	matches, err := filepath.Glob(filepath.Join(dir, "*.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	sort.Strings(matches)
	return matches
}

func TestASecondProcessNeverOverwritesAnUndrainedSegment(t *testing.T) {
	dir := t.TempDir()
	newProcess()
	if err := writeSegment(dir, []byte(`{"run":"A"}`)); err != nil {
		t.Fatal(err)
	}
	newProcess()
	if err := writeSegment(dir, []byte(`{"run":"B"}`)); err != nil {
		t.Fatal(err)
	}
	segs := segments(t, dir)
	if len(segs) != 2 {
		t.Fatalf("want 2 segments, got %v", segs)
	}
	first, err := os.ReadFile(segs[0])
	if err != nil {
		t.Fatal(err)
	}
	if string(first[headerSize:]) != "{\"run\":\"A\"}\n" {
		t.Fatalf("run A's segment was replaced: %q", first[headerSize:])
	}
}

func TestANewProcessWritesAboveTheAcknowledgedCheckpoint(t *testing.T) {
	dir := t.TempDir()
	// The forwarder acknowledged up to seq 41 and eviction removed those segments.
	cp, err := json.Marshal(map[string]any{"last_ack_seq": 41, "last_ack_offset": 0, "initialized": true})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "checkpoint.json"), cp, 0o600); err != nil {
		t.Fatal(err)
	}
	newProcess()
	if err := writeSegment(dir, []byte(`{"after":"ack"}`)); err != nil {
		t.Fatal(err)
	}
	segs := segments(t, dir)
	if len(segs) != 1 || filepath.Base(segs[0]) != "00000000000000000042.jsonl" {
		t.Fatalf("want seq 42 (above the checkpoint), got %v", segs)
	}
}

func TestNoTempFileIsLeftBehind(t *testing.T) {
	dir := t.TempDir()
	newProcess()
	for i := 0; i < 3; i++ {
		if err := writeSegment(dir, []byte(`{"i":1}`)); err != nil {
			t.Fatal(err)
		}
	}
	tmps, _ := filepath.Glob(filepath.Join(dir, "*.tmp"))
	if len(tmps) != 0 {
		t.Fatalf("temp files left: %v", tmps)
	}
	if got := len(segments(t, dir)); got != 3 {
		t.Fatalf("want 3 segments, got %d", got)
	}
}
