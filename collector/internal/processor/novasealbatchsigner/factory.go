package novasealbatchsigner

import (
	"context"
	"fmt"

	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/consumer"
	"go.opentelemetry.io/collector/processor"

	"github.com/novafabric/collector/pkg/novaseal"
)

// typeStr is the registered component type for this processor.
const typeStr = "novaseal_batch_signer"

// NewFactory returns the processor.Factory for the novaseal_batch_signer
// processor.  The factory is registered once at program startup via
// processor.NewFactory.
func NewFactory() processor.Factory {
	return processor.NewFactory(
		component.MustNewType(typeStr),
		createDefaultConfig,
		processor.WithLogs(createLogsProcessor, component.StabilityLevelAlpha),
	)
}

// createLogsProcessor constructs the novaSealBatchSignerProcessor.  The signer
// is not set at construction time; it must be injected via WithSigner before
// the processor is started so that tests can substitute a mock.
func createLogsProcessor(
	_ context.Context,
	set processor.Settings,
	cfg component.Config,
	nextConsumer consumer.Logs,
) (processor.Logs, error) {
	c, ok := cfg.(*Config)
	if !ok {
		return nil, fmt.Errorf("novaseal_batch_signer: invalid config type %T", cfg)
	}

	p := &novaSealBatchSignerProcessor{
		cfg:          c,
		nextConsumer: nextConsumer,
		telemetry:    set.TelemetrySettings,
		signLatency:  defaultSignLatency(),
	}
	return p, nil
}

// WithSigner is a functional option used in tests and by cmd binaries to
// inject a concrete novaseal.Signer into a newly created processor.
func WithSigner(s novaseal.Signer) func(*novaSealBatchSignerProcessor) {
	return func(p *novaSealBatchSignerProcessor) {
		p.signer = s
	}
}
