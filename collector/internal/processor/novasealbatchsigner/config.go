// Package novasealbatchsigner is an OpenTelemetry Collector log processor that
// signs every ResourceLogs batch with NovaSeal before forwarding it downstream.
//
// Each ResourceLogs is canonically encoded (ADR-001), signed via
// novaseal.Signer, and annotated with nova.batch.signature and
// nova.batch.signing_key_id attributes on the Resource.  Verifiers can
// reproduce the canonical bytes and check the signature independently.
package novasealbatchsigner

import (
	"time"

	"go.opentelemetry.io/collector/component"
)

// Config holds the configuration for the novaseal_batch_signer processor.
type Config struct {
	// SendBatchSize is the maximum number of log records per signing batch.
	// Default: 1000.
	SendBatchSize int `mapstructure:"send_batch_size"`

	// Timeout is the maximum duration to wait for a sign operation before
	// returning an error.  Default: 5s.
	Timeout time.Duration `mapstructure:"timeout"`

	// FailOpen controls behaviour when signing fails.  When true the unsigned
	// batch is forwarded and a warning is logged; when false the error is
	// propagated and the batch is dropped.  Default: false (fail-closed).
	FailOpen bool `mapstructure:"fail_open"`

	// CacheTTL is how long a signing key is cached locally before the processor
	// re-fetches it from the keystore.  Default: 5m.
	CacheTTL time.Duration `mapstructure:"cache_ttl"`

	// KeyRotationInterval is how often the active signing key is rotated.
	// Default: 1h.
	KeyRotationInterval time.Duration `mapstructure:"key_rotation_interval"`

	// KeystoreEndpoint is the URL of the KMS/keystore service used to fetch and
	// rotate signing keys.  Leave empty to use the local WAL signer.
	KeystoreEndpoint string `mapstructure:"keystore_endpoint"`

	// LocalWAL enables the local WAL-backed signer for development and testing
	// without a keystore service.  Default: false.
	LocalWAL bool `mapstructure:"local_wal"`
}

// createDefaultConfig returns a Config with all production-safe defaults.
func createDefaultConfig() component.Config {
	return &Config{
		SendBatchSize:       1000,
		Timeout:             5 * time.Second,
		FailOpen:            false,
		CacheTTL:            5 * time.Minute,
		KeyRotationInterval: time.Hour,
	}
}
