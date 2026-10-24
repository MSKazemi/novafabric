package novasealbatchsigner

import (
	"context"
	"fmt"

	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/consumer"
	"go.opentelemetry.io/collector/processor"

	"github.com/MSKazemi/novafabric/collector/pkg/novaseal"
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

// createLogsProcessor constructs the novaSealBatchSignerProcessor from its config.
//
// Review go-collector-ts-packages-01: it used to build no signer at all, so every
// build forwarded every batch UNSIGNED — even with `fail_open: false`. Now:
//   - local_wal: true → the local dev-key keystore; the signer is fetched in Start
//     (the keystore refuses NOVAFABRIC_ENV=production);
//   - keystore_endpoint set → refused: the KMS keystore needs an mTLS client
//     certificate the config has no fields for yet (planned), and silently
//     ignoring a configured keystore is the defect this replaces;
//   - neither, fail_open: false → refused: unsigned batches would be forwarded;
//   - neither, fail_open: true → explicit pass-through (logged at Start).
//
// Tests may still inject a signer with WithSigner.
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

	if c.KeystoreEndpoint != "" {
		return nil, fmt.Errorf(
			"novaseal_batch_signer: keystore_endpoint %q is set, but this build cannot use a "+
				"KMS keystore: it needs an mTLS client certificate the processor config has no "+
				"fields for yet (planned). Remove keystore_endpoint, or set local_wal: true for "+
				"development", c.KeystoreEndpoint)
	}
	p := &novaSealBatchSignerProcessor{
		cfg:          c,
		nextConsumer: nextConsumer,
		telemetry:    set.TelemetrySettings,
		signLatency:  defaultSignLatency(),
	}
	switch {
	case c.LocalWAL:
		p.keystore = novaseal.NewLocalWALKeystore(novaseal.LocalWALConfig{})
	case !c.FailOpen:
		return nil, fmt.Errorf(
			"novaseal_batch_signer: no signer configured (set local_wal: true) and fail_open " +
				"is false: refusing to start a processor that would forward every batch unsigned")
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
