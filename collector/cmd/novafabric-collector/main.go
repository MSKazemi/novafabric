// Command novafabric-collector is an OpenTelemetry Collector distribution that
// registers the NovaSeal batch signing processor (novaseal_batch_signer).
//
// Maturity: experimental, and it cannot run a pipeline today. It registers that
// one processor and nothing else: no receivers, exporters or extensions, and no
// configuration providers, so every --config fails with "at least one Provider
// must be supplied" (exit 1). --help, --version and the components subcommand
// work. The processor itself is unit-tested (stability Alpha).
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
		Description: "NovaFabric Collector — NovaSeal batch signing + OTLP ingestion",
		Version:     "0.1.0",
	}

	cmd := otelcol.NewCommand(otelcol.CollectorSettings{
		BuildInfo: info,
		Factories: func() (otelcol.Factories, error) { return factories, nil },
	})
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
that one processor and nothing else: no receivers, no exporters, no extensions and
no configuration providers. Every --config (and "validate") therefore fails with
"at least one Provider must be supplied" and exits 1. --help, --version and
"components" work.

novaseal_batch_signer options (processors: section): local_wal (dev key in
$HOME/.novafabric/dev-keys/, refused when NOVAFABRIC_ENV=production), fail_open
(default false: refuse to start without a signer), keystore_endpoint (refused: not
usable yet), timeout (per-batch signing deadline, default 5s). send_batch_size,
cache_ttl and key_rotation_interval are accepted but have no effect yet.

Exit codes: 0 success; 1 any error (no config, config load, pipeline start).

See docs/collector-reference.md.`

func buildFactories() (otelcol.Factories, error) {
	factories := otelcol.Factories{}

	// Register the NovaSeal batch signing processor.
	f := novasealbatchsigner.NewFactory()
	factories.Processors = map[component.Type]processor.Factory{
		f.Type(): f,
	}
	_ = component.NewID // suppress unused import if component is only used for BuildInfo

	return factories, nil
}
