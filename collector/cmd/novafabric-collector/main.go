// Command novafabric-collector is the OCB-built OTel Collector with the
// NovaSeal batch signing processor pre-bundled.
//
// Usage:
//
//	novafabric-collector --config collector.yaml
//
// The collector accepts standard OTel Collector configuration syntax. The
// novaseal_batch_signer processor is available in the processors section.
// See deploy/k8s/configmap-collector.yaml for a reference configuration.
package main

import (
	"log/slog"
	"os"

	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/otelcol"
	"go.opentelemetry.io/collector/processor"

	"github.com/novafabric/collector/internal/processor/novasealbatchsigner"
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

	if err := cmd.Execute(); err != nil {
		slog.Error("collector exited with error", "error", err)
		os.Exit(1)
	}
}

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
