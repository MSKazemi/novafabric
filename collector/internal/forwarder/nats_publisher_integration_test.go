//go:build integration

package forwarder

import (
	"context"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/test"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// startJetStream brings up an in-process JetStream server (hermetic; no external
// nats-server binary or live cluster needed) and returns its client URL.
func startJetStream(t *testing.T) string {
	t.Helper()
	opts := natsserver.DefaultTestOptions
	opts.Port = -1 // random free port
	opts.JetStream = true
	opts.StoreDir = t.TempDir()
	srv := natsserver.RunServer(&opts)
	t.Cleanup(srv.Shutdown)
	return srv.ClientURL()
}

// End-to-end edge half: spool → Forwarder → NATSPublisher → JetStream stream.
func TestNATSPublisher_ForwardsSpoolToJetStream(t *testing.T) {
	url := startJetStream(t)

	pub, err := NewNATSPublisher(url, "C0TEST", "c0test.evidence")
	require.NoError(t, err)
	t.Cleanup(func() { _ = pub.Close() })

	ss := openStore(t, t.TempDir())
	t.Cleanup(func() { _ = ss.Close() })
	const runID = "01HRESIDENT000000000000NATS"
	writeEnvelopes(t, ss, runID, 3)

	require.NoError(t, New(ss, pub, "c0test.evidence").DrainBatch(10))

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	nc, err := nats.Connect(url)
	require.NoError(t, err)
	t.Cleanup(nc.Close)
	js, err := jetstream.New(nc)
	require.NoError(t, err)

	// All 3 events are durably persisted to the stream.
	stream, err := js.Stream(ctx, "C0TEST")
	require.NoError(t, err)
	info, err := stream.Info(ctx)
	require.NoError(t, err)
	assert.Equal(t, uint64(3), info.State.Msgs)

	// Each event landed on the per-run subject "<prefix>.<run_id>".
	cons, err := js.OrderedConsumer(ctx, "C0TEST", jetstream.OrderedConsumerConfig{})
	require.NoError(t, err)
	batch, err := cons.Fetch(3)
	require.NoError(t, err)
	got := 0
	for msg := range batch.Messages() {
		assert.Equal(t, "c0test.evidence."+runID, msg.Subject())
		got++
	}
	assert.Equal(t, 3, got)
}
