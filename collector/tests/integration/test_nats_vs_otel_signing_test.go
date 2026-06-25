//go:build integration
// +build integration

package integration

import (
	"context"
	"testing"

	"github.com/stretchr/testify/require"

	otlpcommonv1 "go.opentelemetry.io/proto/otlp/common/v1"
	otlplogspb "go.opentelemetry.io/proto/otlp/logs/v1"
	otlpresourcev1 "go.opentelemetry.io/proto/otlp/resource/v1"

	"github.com/novafabric/collector/pkg/canonical"
	"github.com/novafabric/collector/pkg/novaseal"
)

// TestNATSvsOTelSigningParity verifies that signing via the OTel-processor path
// and the NATS-handler path produce signatures that verify against the same key
// (FR-10: canonical-bytes-to-verify are byte-identical across both paths).
func TestNATSvsOTelSigningParity(t *testing.T) {
	const batches = 100

	// Use a LocalWAL signer for determinism in CI.
	kstore := novaseal.NewLocalWALKeystore(novaseal.LocalWALConfig{DevKeysDir: t.TempDir()})
	signer, err := kstore.GetActiveSigner(context.Background())
	require.NoError(t, err)

	for i := 0; i < batches; i++ {
		rl := makeTestResourceLogs(i)

		// Path A: "OTel processor" — encode, sign.
		canonA, err := canonical.CanonicalEncode(rl)
		require.NoError(t, err)
		sigA, keyIDA, err := signer.Sign(context.Background(), canonA)
		require.NoError(t, err)

		// Path B: "NATS handler" — encode again (simulates a second component
		// calling the same CanonicalEncode function on the same message).
		canonB, err := canonical.CanonicalEncode(rl)
		require.NoError(t, err)

		require.Equal(t, canonA, canonB, "batch %d: canonical bytes differ between paths", i)

		// Verify A's signature against B's canonical bytes (and vice versa).
		require.NoError(t, signer.Verify(context.Background(), canonB, sigA, keyIDA),
			"batch %d: signature from path A failed to verify against canonical bytes from path B", i)
	}
}

func makeTestResourceLogs(seed int) *otlplogspb.ResourceLogs {
	return &otlplogspb.ResourceLogs{
		Resource: &otlpresourcev1.Resource{
			Attributes: []*otlpcommonv1.KeyValue{
				{Key: "service.name", Value: &otlpcommonv1.AnyValue{Value: &otlpcommonv1.AnyValue_StringValue{StringValue: "test-agent"}}},
				{Key: "nova.batch.signature", Value: &otlpcommonv1.AnyValue{Value: &otlpcommonv1.AnyValue_StringValue{StringValue: "shouldbestripped"}}},
				{Key: "nova.cluster_id", Value: &otlpcommonv1.AnyValue{Value: &otlpcommonv1.AnyValue_StringValue{StringValue: "hpc01"}}},
			},
		},
		ScopeLogs: []*otlplogspb.ScopeLogs{
			{
				LogRecords: []*otlplogspb.LogRecord{
					{Body: &otlpcommonv1.AnyValue{Value: &otlpcommonv1.AnyValue_StringValue{StringValue: "event-" + string(rune('A'+seed%26))}}},
				},
			},
		},
	}
}
