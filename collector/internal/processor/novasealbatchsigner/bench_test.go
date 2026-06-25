package novasealbatchsigner

import (
	"context"
	"fmt"
	"sort"
	"testing"
	"time"

	"github.com/novafabric/collector/pkg/novaseal"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/collector/consumer/consumertest"
	"go.opentelemetry.io/collector/pdata/plog"
)

// raceEnabled is true when the test binary was built with -race.
// Its value is set by the build-tag-selected file bench_race_test.go or
// bench_norace_test.go.
var raceEnabled bool

// makeTestLogsBatched creates a plog.Logs with nRL ResourceLogs, each
// containing nRecords log records.  This reflects realistic OTel Collector
// batching where many log records share the same resource (service/node).
func makeTestLogsBatched(nRL, nRecords int) plog.Logs {
	ld := plog.NewLogs()
	for i := 0; i < nRL; i++ {
		rl := ld.ResourceLogs().AppendEmpty()
		rl.Resource().Attributes().PutStr("service.name", fmt.Sprintf("svc-%03d", i))
		rl.Resource().Attributes().PutStr("host.name", fmt.Sprintf("node-%03d", i))
		sl := rl.ScopeLogs().AppendEmpty()
		for j := 0; j < nRecords; j++ {
			lr := sl.LogRecords().AppendEmpty()
			lr.Body().SetStr(fmt.Sprintf(`{"event_id":"ev-%04d-%06d","envelope_version":"1"}`, i, j))
			lr.Attributes().PutStr("nova.event_type", "model_call")
		}
	}
	return ld
}


// TestNovaSealBatchSigner_ThroughputAndLatency verifies BQ-011 criterion:
// "NovaSeal batch processor signs at 100K events/sec within p99 < 200ms."
//
// Method: sign 100 batches of 10 ResourceLogs × 100 log records each (1000
// events per batch, 100K total events).  The signing unit is one ResourceLogs;
// the throughput is measured in log records (events) per second.
//
// Run without -race for a representative throughput measurement:
//
//	go test -run TestNovaSealBatchSigner_ThroughputAndLatency ./internal/processor/...
func TestNovaSealBatchSigner_ThroughputAndLatency(t *testing.T) {
	_, priv, err := novaseal.GenerateEd25519KeyPair()
	require.NoError(t, err)
	signer, err := novaseal.NewEd25519Signer("bench-key-bq011", priv)
	require.NoError(t, err)

	sink := &consumertest.LogsSink{}
	p := buildProcessor(signer, false /* failOpen */, sink)

	// Realistic batch: 10 ResourceLogs × 100 log records = 1000 events per batch.
	// 100 batches = 100K total events.
	const (
		nRL       = 10
		nRecords  = 100
		batchEvts = nRL * nRecords // 1000
		iters     = 100            // 100K total
	)

	// Pre-build the batch to isolate signing latency from batch construction.
	ld := makeTestLogsBatched(nRL, nRecords)

	latencies := make([]time.Duration, iters)
	totalStart := time.Now()

	for i := 0; i < iters; i++ {
		batchStart := time.Now()
		require.NoError(t, p.ConsumeLogs(context.Background(), ld),
			"ConsumeLogs must not fail on iteration %d", i)
		latencies[i] = time.Since(batchStart)
	}

	totalElapsed := time.Since(totalStart)
	totalEvents := iters * batchEvts

	// Sort for percentile computation.
	sort.Slice(latencies, func(i, j int) bool { return latencies[i] < latencies[j] })
	p99Idx := int(float64(iters)*0.99) - 1
	if p99Idx < 0 {
		p99Idx = 0
	}
	p99 := latencies[p99Idx]
	p50 := latencies[iters/2]

	eventsPerSec := float64(totalEvents) / totalElapsed.Seconds()

	t.Logf("NovaSeal batch signer: %d events (%d RL×%d records) in %v",
		totalEvents, nRL, nRecords, totalElapsed)
	t.Logf("  throughput: %.0f events/sec", eventsPerSec)
	t.Logf("  p50 batch latency: %v", p50)
	t.Logf("  p99 batch latency: %v (limit: 200ms)", p99)

	// Acceptance criteria (BQ-011).
	//
	// Latency (p99 < 200ms) is a correctness signal — it catches
	// deadlocks/livelocks even under -race instrumentation — so we assert it
	// in both race and non-race modes.
	if p99 >= 200*time.Millisecond {
		t.Errorf("p99 batch signing latency %v ≥ 200ms (BQ-011 requires p99 < 200ms)", p99)
	}
	// Throughput is a perf budget, not a correctness signal. The race detector
	// adds 5–10× overhead and the variance on CI runners is wide enough that
	// asserting a throughput floor under -race produces flaky failures (e.g.
	// 20K/sec achieved on GH Actions free runners vs a 25K floor). We assert
	// the 100K/sec floor only in non-race builds; benchmarks
	// (BenchmarkNovaSealBatchSigner below, run separately) carry the perf
	// budget for race=false runs and dedicated perf workflows.
	if !raceEnabled {
		floor := float64(100_000)
		if eventsPerSec < floor {
			t.Errorf("signing throughput %.0f events/sec < %.0f/sec (BQ-011 requirement)",
				eventsPerSec, floor)
		}
	} else {
		t.Logf("throughput assertion skipped under -race (informational: %.0f events/sec)",
			eventsPerSec)
	}
}

// BenchmarkNovaSealBatchSigner measures per-batch signing throughput for profiling.
func BenchmarkNovaSealBatchSigner(b *testing.B) {
	_, priv, err := novaseal.GenerateEd25519KeyPair()
	if err != nil {
		b.Fatal(err)
	}
	signer, err := novaseal.NewEd25519Signer("bench-key", priv)
	if err != nil {
		b.Fatal(err)
	}

	sink := &consumertest.LogsSink{}
	p := buildProcessor(signer, false, sink)
	ld := makeTestLogsBatched(10, 100) // 1000 events per batch

	b.ResetTimer()
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		if err := p.ConsumeLogs(context.Background(), ld); err != nil {
			b.Fatal(err)
		}
	}
	b.ReportMetric(float64(1000*b.N)/b.Elapsed().Seconds(), "events/sec")
}
