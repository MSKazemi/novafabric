package novasealbatchsigner

import (
	"context"
	"strings"
	"testing"

	"go.opentelemetry.io/collector/component/componenttest"
	"go.opentelemetry.io/collector/consumer/consumertest"
	"go.opentelemetry.io/collector/pdata/plog"
	"go.opentelemetry.io/collector/processor/processortest"
)

// Review go-collector-ts-packages-01: createLogsProcessor never built a signer, so
// every build forwarded every batch UNSIGNED — even with the documented
// `fail_open: false  # fail-closed — unsigned batches are refused`. These tests
// go through the factory, the path the collector binary uses.

func createVia(t *testing.T, cfg *Config, sink *consumertest.LogsSink) (*novaSealBatchSignerProcessor, error) {
	t.Helper()
	f := NewFactory()
	p, err := f.CreateLogs(context.Background(), processortest.NewNopSettings(f.Type()), cfg, sink)
	if err != nil {
		return nil, err
	}
	return p.(*novaSealBatchSignerProcessor), nil
}

func oneBatch() plog.Logs {
	ld := plog.NewLogs()
	rl := ld.ResourceLogs().AppendEmpty()
	rl.Resource().Attributes().PutStr("service.name", "capture")
	rl.ScopeLogs().AppendEmpty().LogRecords().AppendEmpty().Body().SetStr(`{"envelope_version":"1"}`)
	return ld
}

func TestFactoryRefusesToForwardUnsignedWhenFailClosed(t *testing.T) {
	cfg := createDefaultConfig().(*Config) // fail_open: false, no keystore
	_, err := createVia(t, cfg, new(consumertest.LogsSink))
	if err == nil || !strings.Contains(err.Error(), "no signer") {
		t.Fatalf("want a 'no signer' creation error, got %v", err)
	}
}

func TestFactoryPassesThroughOnlyWithExplicitFailOpen(t *testing.T) {
	cfg := createDefaultConfig().(*Config)
	cfg.FailOpen = true
	sink := new(consumertest.LogsSink)
	p, err := createVia(t, cfg, sink)
	if err != nil {
		t.Fatal(err)
	}
	if err := p.ConsumeLogs(context.Background(), oneBatch()); err != nil {
		t.Fatal(err)
	}
	if got := sink.LogRecordCount(); got != 1 {
		t.Fatalf("forwarded %d records, want 1", got)
	}
}

func TestFactoryBuildsTheLocalWALSigner(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	t.Setenv("NOVAFABRIC_ENV", "")
	cfg := createDefaultConfig().(*Config)
	cfg.LocalWAL = true
	sink := new(consumertest.LogsSink)
	p, err := createVia(t, cfg, sink)
	if err != nil {
		t.Fatal(err)
	}
	if err := p.Start(context.Background(), componenttest.NewNopHost()); err != nil {
		t.Fatal(err)
	}
	if err := p.ConsumeLogs(context.Background(), oneBatch()); err != nil {
		t.Fatal(err)
	}
	logs := sink.AllLogs()
	if len(logs) != 1 {
		t.Fatalf("forwarded %d batches, want 1", len(logs))
	}
	attrs := logs[0].ResourceLogs().At(0).Resource().Attributes()
	if _, ok := attrs.Get("nova.batch.signature"); !ok {
		t.Fatal("the forwarded batch carries no nova.batch.signature")
	}
}

func TestFactoryRefusesAKeystoreEndpointItCannotUse(t *testing.T) {
	cfg := createDefaultConfig().(*Config)
	cfg.KeystoreEndpoint = "https://kms.internal:8443"
	cfg.FailOpen = true // even fail-open must not silently ignore a configured keystore
	_, err := createVia(t, cfg, new(consumertest.LogsSink))
	if err == nil || !strings.Contains(err.Error(), "keystore_endpoint") {
		t.Fatalf("want a keystore_endpoint error, got %v", err)
	}
}
