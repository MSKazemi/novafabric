// Command novafabric-collector is an OpenTelemetry Collector distribution that
// registers the NovaSeal batch signing processor (novaseal_batch_signer).
//
// Maturity: experimental. It registers that one processor, the file, env and
// yaml configuration providers and a stderr-only telemetry factory, so
// --config, validate and print-config load and check a configuration. It
// registers no receivers, exporters or extensions, so it cannot run a pipeline:
// the service requires at least one pipeline with a receiver and an exporter,
// and config validation reports that. The processor itself is unit-tested
// (stability Alpha).
//
// Usage:
//
//	novafabric-collector --config collector.yaml
//	novafabric-collector components
//
// See docs/collector-reference.md.
package main

import (
	"log/slog"
	"os"

	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/confmap"
	"go.opentelemetry.io/collector/confmap/provider/envprovider"
	"go.opentelemetry.io/collector/confmap/provider/fileprovider"
	"go.opentelemetry.io/collector/confmap/provider/yamlprovider"
	"go.opentelemetry.io/collector/otelcol"
	"go.opentelemetry.io/collector/processor"

	"github.com/MSKazemi/novafabric/collector/internal/processor/novasealbatchsigner"
)

func main() {
	factories, err := buildFactories()
	if err != nil {
		slog.Error("failed to build component factories", "error", err)
		os.Exit(1)
	}

	info := component.BuildInfo{
		Command:     "novafabric-collector",
		Description: "NovaFabric Collector — NovaSeal batch signing processor only (experimental; no receivers or exporters)",
		Version:     "0.1.0",
	}

	cmd := otelcol.NewCommand(collectorSettings(info, factories))
	cmd.Long = longHelp

	if err := cmd.Execute(); err != nil {
		slog.Error("collector exited with error", "error", err)
		os.Exit(1)
	}
}

// longHelp is the description shown by --help. Help text only: it does not
// change what the command does.
const longHelp = `novafabric-collector: an OpenTelemetry Collector distribution that registers the
NovaSeal batch signing processor (novaseal_batch_signer).

Maturity: experimental, and it cannot run a pipeline today. This build registers
that one processor, the file:, env: and yaml: configuration providers (a --config
value without a scheme is a file path; no http:/https: provider) and a telemetry
factory that logs to stderr (service::telemetry::logs::level, default info; no
metrics listener). It registers no receivers, exporters or extensions, so
--config and "validate" load the file, check the novaseal_batch_signer section,
and then exit 1 because a pipeline needs a receiver and an exporter.
"print-config", --help, --version and "components" work.

novaseal_batch_signer options (processors: section): local_wal (dev key in
$HOME/.novafabric/dev-keys/, refused when NOVAFABRIC_ENV=production), fail_open
(default false: refuse to start without a signer), keystore_endpoint (refused: not
usable yet), timeout (per-batch signing deadline, default 5s). send_batch_size,
cache_ttl and key_rotation_interval are accepted but have no effect yet.

Exit codes: 0 success; 1 any error (no config, config load or validation,
pipeline start).

See docs/collector-reference.md.`

// defaultConfigScheme is the scheme used for a ${NAME} reference that names no
// scheme. "env" matches the OpenTelemetry Configuration Specification and the
// upstream otelcol default.
const defaultConfigScheme = "env"

// configProviderFactories returns the confmap providers this distribution
// registers: file: (also used for a --config value without a scheme), env:
// and yaml:. No network provider (http:, https:) is registered, so a
// configuration never needs network access to load.
func configProviderFactories() []confmap.ProviderFactory {
	return []confmap.ProviderFactory{
		fileprovider.NewFactory(),
		envprovider.NewFactory(),
		yamlprovider.NewFactory(),
	}
}

// collectorSettings builds the settings passed to otelcol.NewCommand. Without
// at least one ProviderFactory every --config fails with "at least one
// Provider must be supplied".
func collectorSettings(info component.BuildInfo, factories otelcol.Factories) otelcol.CollectorSettings {
	return otelcol.CollectorSettings{
		BuildInfo: info,
		Factories: func() (otelcol.Factories, error) { return factories, nil },
		ConfigProviderSettings: otelcol.ConfigProviderSettings{
			ResolverSettings: confmap.ResolverSettings{
				ProviderFactories: configProviderFactories(),
				DefaultScheme:     defaultConfigScheme,
			},
		},
	}
}

func buildFactories() (otelcol.Factories, error) {
	factories := otelcol.Factories{
		// Required by otelcol to load any config; see telemetry.go.
		Telemetry: newTelemetryFactory(),
	}

	// Register the NovaSeal batch signing processor.
	f := novasealbatchsigner.NewFactory()
	factories.Processors = map[component.Type]processor.Factory{
		f.Type(): f,
	}
	return factories, nil
}
