package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/confmap"
	"go.opentelemetry.io/collector/featuregate"
	"go.opentelemetry.io/collector/otelcol"
	"go.opentelemetry.io/collector/service/telemetry"
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
)

func testSettings(t *testing.T) otelcol.CollectorSettings {
	t.Helper()
	factories, err := buildFactories()
	if err != nil {
		t.Fatalf("buildFactories: %v", err)
	}
	set := collectorSettings(component.BuildInfo{Command: "novafabric-collector", Version: "test"}, factories)
	set.ConfigProviderSettings.ResolverSettings.ProviderSettings = confmap.ProviderSettings{Logger: zap.NewNop()}
	set.ConfigProviderSettings.ResolverSettings.ConverterSettings = confmap.ConverterSettings{Logger: zap.NewNop()}
	return set
}

// Regression: with no ProviderFactories every --config failed with
// "at least one Provider must be supplied".
func TestCollectorSettingsRegisterFileEnvYAMLProviders(t *testing.T) {
	set := testSettings(t)
	rs := set.ConfigProviderSettings.ResolverSettings

	if len(rs.ProviderFactories) == 0 {
		t.Fatal("no confmap ProviderFactories registered: --config cannot load")
	}
	got := map[string]bool{}
	for _, pf := range rs.ProviderFactories {
		got[pf.Create(rs.ProviderSettings).Scheme()] = true
	}
	for _, want := range []string{"file", "env", "yaml"} {
		if !got[want] {
			t.Errorf("provider scheme %q not registered (got %v)", want, got)
		}
	}
	// No network provider: loading a config must never need the network.
	for _, banned := range []string{"http", "https"} {
		if got[banned] {
			t.Errorf("network provider scheme %q must not be registered", banned)
		}
	}
	if rs.DefaultScheme != "env" {
		t.Errorf("DefaultScheme = %q, want env", rs.DefaultScheme)
	}
}

// Regression: with a nil Factories.Telemetry every config failed to load with
// "otelcol.Factories.Telemetry must not be nil".
func TestBuildFactoriesRegistersTelemetryAndProcessor(t *testing.T) {
	factories, err := buildFactories()
	if err != nil {
		t.Fatalf("buildFactories: %v", err)
	}
	if factories.Telemetry == nil {
		t.Fatal("Factories.Telemetry is nil: no config can be loaded")
	}
	if len(factories.Processors) != 1 {
		t.Fatalf("want exactly one processor, got %d", len(factories.Processors))
	}
	for typ := range factories.Processors {
		if typ.String() != "novaseal_batch_signer" {
			t.Errorf("unexpected processor %q", typ)
		}
	}
}

// The settings resolve a real file: config end to end. A pipeline needs a
// receiver and an exporter, which this distribution does not register, so
// validation must fail on that and not on provider or telemetry setup.
func TestConfigProviderLoadsFileConfig(t *testing.T) {
	dir := t.TempDir()
	cfgPath := filepath.Join(dir, "collector.yaml")
	const cfg = `processors:
  novaseal_batch_signer:
    local_wal: true
service:
  telemetry:
    logs:
      level: warn
  pipelines: {}
`
	if err := os.WriteFile(cfgPath, []byte(cfg), 0o600); err != nil {
		t.Fatal(err)
	}

	set := testSettings(t)
	set.ConfigProviderSettings.ResolverSettings.URIs = []string{cfgPath}
	provider, err := otelcol.NewConfigProvider(set.ConfigProviderSettings)
	if err != nil {
		t.Fatalf("NewConfigProvider: %v", err)
	}
	factories, err := set.Factories()
	if err != nil {
		t.Fatal(err)
	}
	conf, err := provider.Get(context.Background(), factories)
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	tc, ok := conf.Service.Telemetry.(*telemetryConfig)
	if !ok {
		t.Fatalf("service::telemetry type = %T, want *telemetryConfig", conf.Service.Telemetry)
	}
	if tc.Logs.Level != "warn" {
		t.Errorf("logs level = %q, want warn", tc.Logs.Level)
	}

	err = conf.Validate()
	if err == nil {
		t.Fatal("Validate passed for a config with no receivers")
	}
	for _, bad := range []string{"Provider must be supplied", "Telemetry must not be nil"} {
		if strings.Contains(err.Error(), bad) {
			t.Errorf("Validate failed on setup, not on the pipeline: %v", err)
		}
	}
}

func TestTelemetryConfigRejectsUnknownLogLevel(t *testing.T) {
	cfg := newTelemetryFactory().CreateDefaultConfig().(*telemetryConfig)
	if err := cfg.Validate(); err != nil {
		t.Fatalf("default telemetry config invalid: %v", err)
	}
	cfg.Logs.Level = "loud"
	if err := cfg.Validate(); err == nil {
		t.Fatal("Validate accepted log level \"loud\"")
	}
}

func TestTelemetryLoggerHonoursLevel(t *testing.T) {
	f := newTelemetryFactory()
	cfg := f.CreateDefaultConfig().(*telemetryConfig)
	cfg.Logs.Level = "error"
	logger, shutdown, err := f.CreateLogger(context.Background(), telemetry.LoggerSettings{}, cfg)
	if err != nil {
		t.Fatalf("CreateLogger: %v", err)
	}
	t.Cleanup(func() { _ = shutdown.Shutdown(context.Background()) })
	if logger.Core().Enabled(zapcore.DebugLevel) {
		t.Error("debug enabled at level error")
	}
	if !logger.Core().Enabled(zapcore.ErrorLevel) {
		t.Error("error not enabled at level error")
	}
}

// The documented smoke run: with the upstream service.AllowNoPipelines gate the
// collector starts with the custom telemetry factory and shuts down cleanly.
func TestCollectorStartsAndShutsDownWithNoPipelines(t *testing.T) {
	const gate = "service.AllowNoPipelines"
	if err := featuregate.GlobalRegistry().Set(gate, true); err != nil {
		t.Fatalf("enable %s: %v", gate, err)
	}
	t.Cleanup(func() { _ = featuregate.GlobalRegistry().Set(gate, false) })
	t.Setenv("HOME", t.TempDir()) // the processor's dev key store, if ever touched

	cfgPath := filepath.Join(t.TempDir(), "collector.yaml")
	const cfg = `processors:
  novaseal_batch_signer:
    fail_open: true
service:
  telemetry:
    logs:
      level: warn
  pipelines: {}
`
	if err := os.WriteFile(cfgPath, []byte(cfg), 0o600); err != nil {
		t.Fatal(err)
	}

	set := testSettings(t)
	set.ConfigProviderSettings.ResolverSettings.URIs = []string{cfgPath}
	col, err := otelcol.NewCollector(set)
	if err != nil {
		t.Fatalf("NewCollector: %v", err)
	}

	done := make(chan error, 1)
	go func() { done <- col.Run(context.Background()) }()

	deadline := time.Now().Add(20 * time.Second)
	for col.GetState() != otelcol.StateRunning {
		select {
		case err := <-done:
			t.Fatalf("collector exited before running: %v", err)
		default:
		}
		if time.Now().After(deadline) {
			col.Shutdown()
			t.Fatalf("collector not running after 20s (state %v)", col.GetState())
		}
		time.Sleep(20 * time.Millisecond)
	}

	col.Shutdown()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("Run returned %v after Shutdown", err)
		}
	case <-time.After(20 * time.Second):
		t.Fatal("Run did not return within 20s of Shutdown")
	}
	if got := col.GetState(); got != otelcol.StateClosed {
		t.Errorf("state after shutdown = %v, want Closed", got)
	}
}
