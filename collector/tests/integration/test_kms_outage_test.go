//go:build integration
// +build integration

package integration

import (
	"context"
	"crypto/tls"
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/novafabric/collector/pkg/novaseal"
)

// TestKMSOutageFailClosed verifies that when the KMS is unreachable,
// fail_open=false causes GetActiveSigner to return an error (FR-9 / tc-003).
func TestKMSOutageFailClosed(t *testing.T) {
	cfg := novaseal.KeystoreConfig{
		Endpoint: "https://kms-unreachable.invalid:8443",
		FailOpen: false,
	}
	// With an unreachable KMS and fail_open=false the keystore must fail closed:
	// no usable signer may be produced (no unsigned egress — FR-9 / tc-003). The
	// implementation fetches eagerly, so the failure surfaces at construction;
	// if a future build defers it, GetActiveSigner must error instead. Accept
	// either point — both are correct fail-closed behavior.
	kstore, err := novaseal.NewKeystore(cfg, tls.Certificate{})
	if err != nil {
		return // failed closed at construction
	}
	defer kstore.Close()

	_, err = kstore.GetActiveSigner(context.Background())
	require.Error(t, err, "expected error when KMS is unreachable with fail_open=false")
}

// TestKMSOutageFailOpen verifies that when the KMS is unreachable and
// fail_open=true with a cached key, signing continues (FR-9 variant).
func TestKMSOutageFailOpen(t *testing.T) {
	t.Skip("Requires a pre-seeded cache — tested via unit tests in pkg/novaseal")
}
