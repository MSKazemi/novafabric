// Package verifier implements NovaSeal signature verification for individual
// OTLP ResourceLogs messages.
//
// Callers pass a *otlplogspb.ResourceLogs that has been received from a
// collector pipeline; the verifier extracts the nova.batch.signature and
// nova.batch.signing_key_id attributes, recomputes the canonical bytes, and
// checks the signature via a novaseal.Signer.
package verifier

import (
	"context"
	"encoding/base64"
	"fmt"

	otlpcommonpb "go.opentelemetry.io/proto/otlp/common/v1"
	otlplogspb "go.opentelemetry.io/proto/otlp/logs/v1"

	"github.com/novafabric/collector/pkg/canonical"
	"github.com/novafabric/collector/pkg/novaseal"
)

// VerifyResult is the outcome of verifying a single ResourceLogs signature.
type VerifyResult struct {
	// Valid is true if and only if the signature was cryptographically valid.
	Valid bool
	// KeyID is the key identifier reported by the ResourceLogs attributes.
	KeyID string
	// Error is a human-readable explanation when Valid is false; empty on success.
	Error string
}

// VerifyResourceLogs verifies the nova.batch.signature on rl.
//
// It:
//  1. Extracts nova.batch.signature and nova.batch.signing_key_id from
//     rl.Resource.Attributes.
//  2. Base64-decodes the signature.
//  3. Calls canonical.CanonicalEncode(rl) — which strips the two signing
//     attributes before encoding, mirroring what the signer did.
//  4. Calls signer.Verify(canonicalBytes, sig, keyID).
//
// A missing or malformed signature always returns Valid=false with a
// descriptive Error; it never panics.
func VerifyResourceLogs(ctx context.Context, rl *otlplogspb.ResourceLogs, signer novaseal.Signer) VerifyResult {
	if rl == nil {
		return VerifyResult{Error: "verifier: nil ResourceLogs"}
	}

	sigB64, keyID, err := extractSigningAttrs(rl)
	if err != nil {
		return VerifyResult{Error: err.Error()}
	}

	sigBytes, err := base64.StdEncoding.DecodeString(sigB64)
	if err != nil {
		return VerifyResult{
			KeyID: keyID,
			Error: fmt.Sprintf("verifier: base64 decode signature: %v", err),
		}
	}

	// canonical.CanonicalEncode strips the signing attributes internally, so
	// the encoder sees exactly what the signer saw.
	canonicalBytes, err := canonical.CanonicalEncode(rl)
	if err != nil {
		return VerifyResult{
			KeyID: keyID,
			Error: fmt.Sprintf("verifier: canonical encode: %v", err),
		}
	}

	if verifyErr := signer.Verify(ctx, canonicalBytes, sigBytes, keyID); verifyErr != nil {
		return VerifyResult{
			KeyID: keyID,
			Error: verifyErr.Error(),
		}
	}

	return VerifyResult{Valid: true, KeyID: keyID}
}

// extractSigningAttrs returns the raw base64 signature string and keyID from
// rl.Resource.Attributes.  Returns an error if either attribute is missing.
func extractSigningAttrs(rl *otlplogspb.ResourceLogs) (sigB64 string, keyID string, err error) {
	if rl.Resource == nil {
		return "", "", fmt.Errorf("verifier: ResourceLogs.Resource is nil — no attributes to inspect")
	}

	for _, kv := range rl.Resource.Attributes {
		switch kv.Key {
		case "nova.batch.signature":
			sigB64 = stringVal(kv)
		case "nova.batch.signing_key_id":
			keyID = stringVal(kv)
		}
	}

	if sigB64 == "" {
		return "", "", fmt.Errorf("verifier: nova.batch.signature attribute is missing or empty")
	}
	if keyID == "" {
		return "", "", fmt.Errorf("verifier: nova.batch.signing_key_id attribute is missing or empty")
	}
	return sigB64, keyID, nil
}

// stringVal extracts the string value from an OTLP KeyValue, returning an
// empty string if the value is not a string type.
func stringVal(kv *otlpcommonpb.KeyValue) string {
	if kv == nil || kv.Value == nil {
		return ""
	}
	sv, ok := kv.Value.Value.(*otlpcommonpb.AnyValue_StringValue)
	if !ok {
		return ""
	}
	return sv.StringValue
}
