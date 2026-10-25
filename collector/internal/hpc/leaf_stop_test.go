package hpc

import (
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// startFake wires a stand-in "nats-server" (a shell script) into a Leaf the way Start does.
func startFake(t *testing.T, script string) *Leaf {
	t.Helper()
	l := NewLeaf(LeafConfig{SpoolDir: t.TempDir(), JobID: "j", NodeID: "n"})
	l.proc = exec.Command("sh", "-c", script)
	require.NoError(t, l.proc.Start())
	go func() {
		defer close(l.done)
		_ = l.proc.Wait()
	}()
	return l
}

// fakeNATSCLI puts a `nats` executable on PATH that records every invocation.
func fakeNATSCLI(t *testing.T) (calls string) {
	t.Helper()
	bin := t.TempDir()
	calls = filepath.Join(bin, "calls.txt")
	script := "#!/bin/sh\necho \"$@\" >> " + calls + "\n"
	require.NoError(t, os.WriteFile(filepath.Join(bin, "nats"), []byte(script), 0o755))
	t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))
	return calls
}

// Review go-collector-ts-packages-10: Stop "flushed" JetStream by running `nats stream
// purge`, a delete-all command. No nats CLI command may run at all.
func TestLeafStopNeverRunsTheNatsCLI(t *testing.T) {
	calls := fakeNATSCLI(t)
	l := startFake(t, `trap 'exit 0' TERM; while :; do sleep 0.05; done`)

	require.NoError(t, l.Stop(t.Context(), 3*time.Second))

	_, err := os.Stat(calls)
	assert.True(t, os.IsNotExist(err), "Stop must not invoke the nats CLI (purge deletes every message)")
}

func TestLeafStopLetsTheServerExitGracefullyOnSIGTERM(t *testing.T) {
	marker := filepath.Join(t.TempDir(), "term-received")
	l := startFake(t, `trap 'touch `+marker+`; exit 0' TERM; while :; do sleep 0.05; done`)
	time.Sleep(200 * time.Millisecond) // let the trap install

	start := time.Now()
	require.NoError(t, l.Stop(t.Context(), 5*time.Second))

	_, err := os.Stat(marker)
	assert.NoError(t, err, "the server must get SIGTERM, which is what makes nats-server persist JetStream state")
	assert.Less(t, time.Since(start), 3*time.Second, "a server that exits on SIGTERM is not waited on for the full timeout")
}

func TestLeafStopKillsAServerThatIgnoresSIGTERMAfterTheTimeout(t *testing.T) {
	l := startFake(t, `trap '' TERM; while :; do sleep 0.05; done`)
	time.Sleep(200 * time.Millisecond)

	require.NoError(t, l.Stop(t.Context(), 500*time.Millisecond))

	select {
	case <-l.done:
	default:
		t.Fatal("the process must be gone after Stop")
	}
}
