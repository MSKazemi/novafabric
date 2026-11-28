package main

import (
	"context"
	"fmt"

	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/service/telemetry"
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
)

// telemetryConfig is the service::telemetry section this distribution accepts.
//
// Only the log level is configurable. The collector's own logs go to stderr;
// its internal metrics and traces use no-op providers, so the binary opens no
// metrics listener and sends no telemetry anywhere. Any other key under
// service::telemetry is rejected when the config is loaded.
type telemetryConfig struct {
	Logs telemetryLogsConfig `mapstructure:"logs"`
}

type telemetryLogsConfig struct {
	// Level is a zap level name: debug, info, warn or error. Default info.
	Level string `mapstructure:"level"`
}

// Validate rejects an unknown log level when the config is loaded, so
// "validate" reports it instead of the first run.
func (c *telemetryConfig) Validate() error {
	if _, err := zapcore.ParseLevel(c.Logs.Level); err != nil {
		return fmt.Errorf("logs::level: %w", err)
	}
	return nil
}

// defaultTelemetryLogLevel is the log level used when service::telemetry is absent.
const defaultTelemetryLogLevel = "info"

// newTelemetryFactory returns the telemetry factory for otelcol.Factories.
//
// otelcol requires one: with a nil Factories.Telemetry every config fails to
// load with "otelcol.Factories.Telemetry must not be nil". The upstream
// otelconftelemetry factory is not used because it pulls in cloud resource
// detectors and a Kubernetes client, and serves metrics on localhost:8888 by
// default; this distribution needs neither.
func newTelemetryFactory() telemetry.Factory {
	return telemetry.NewFactory(
		func() component.Config {
			return &telemetryConfig{Logs: telemetryLogsConfig{Level: defaultTelemetryLogLevel}}
		},
		telemetry.WithCreateLogger(createTelemetryLogger),
	)
}

// createTelemetryLogger builds the service logger: production JSON encoding to
// stderr at the configured level.
func createTelemetryLogger(
	_ context.Context, set telemetry.LoggerSettings, cfg component.Config,
) (*zap.Logger, component.ShutdownFunc, error) {
	tc, ok := cfg.(*telemetryConfig)
	if !ok {
		return nil, nil, fmt.Errorf("telemetry: unexpected config type %T", cfg)
	}
	level, err := zapcore.ParseLevel(tc.Logs.Level)
	if err != nil {
		return nil, nil, fmt.Errorf("telemetry: service::telemetry::logs::level: %w", err)
	}

	zc := zap.NewProductionConfig()
	zc.Level = zap.NewAtomicLevelAt(level)
	zc.OutputPaths = []string{"stderr"}
	zc.ErrorOutputPaths = []string{"stderr"}

	build := set.BuildZapLogger
	if build == nil {
		build = func(c zap.Config, opts ...zap.Option) (*zap.Logger, error) { return c.Build(opts...) }
	}
	logger, err := build(zc)
	if err != nil {
		return nil, nil, fmt.Errorf("telemetry: build logger: %w", err)
	}
	shutdown := func(context.Context) error {
		// Sync on stderr returns EINVAL/ENOTTY on some platforms; it is not a
		// shutdown failure.
		_ = logger.Sync()
		return nil
	}
	return logger, shutdown, nil
}
