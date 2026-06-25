package canonical_test

import (
	"context"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"testing"
	"time"

	"github.com/novafabric/collector/pkg/canonical"
	"github.com/novafabric/collector/pkg/novaseal"
	"github.com/stretchr/testify/require"
	otlpcommonv1 "go.opentelemetry.io/proto/otlp/common/v1"
	otlplogspb "go.opentelemetry.io/proto/otlp/logs/v1"
	otlpresourcepb "go.opentelemetry.io/proto/otlp/resource/v1"
)

const corpusSize = 10_000

// corpusManifest is the JSON structure written by the Go test and read by the
// Python verifier at scripts/verify_canonical_roundtrip.py.
type corpusManifest struct {
	// PublicKeyHex is the hex-encoded 32-byte Ed25519 public key.
	PublicKeyHex string        `json:"public_key_hex"`
	KeyID        string        `json:"key_id"`
	Events       []corpusEntry `json:"events"`
}

// corpusEntry holds the canonical bytes and detached Ed25519 signature for a
// single ResourceLogs object.
type corpusEntry struct {
	// CanonicalB64 is the base64-encoded output of canonical.CanonicalEncode.
	CanonicalB64 string `json:"canonical_b64"`
	// SignatureB64 is the base64-encoded 64-byte Ed25519 signature over CanonicalB64.
	SignatureB64 string `json:"signature_b64"`
}

// TestCanonicalEncode_CrossLanguageRoundTrip10K generates corpusSize (10K)
// ResourceLogs, encodes each canonically, signs with Ed25519, and verifies the
// round-trip in Go.  It then writes a JSON manifest that can be independently
// verified by the Python script at scripts/verify_canonical_roundtrip.py.
//
// The cross-language step runs automatically when python3 is on PATH.  It can
// be skipped in CI by setting SKIP_PYTHON_ROUNDTRIP=1.
//
// Acceptance criterion (BQ-011): canonical byte encoding passes cross-language
// round-trip test on 10K-batch corpus.
func TestCanonicalEncode_CrossLanguageRoundTrip10K(t *testing.T) {
	pub, priv, err := novaseal.GenerateEd25519KeyPair()
	require.NoError(t, err)

	const keyID = "roundtrip-test-key-bq011"
	signer, err := novaseal.NewEd25519Signer(keyID, priv)
	require.NoError(t, err)

	ctx := context.Background()
	manifest := corpusManifest{
		PublicKeyHex: hex.EncodeToString(pub),
		KeyID:        keyID,
		Events:       make([]corpusEntry, corpusSize),
	}

	start := time.Now()
	for i := 0; i < corpusSize; i++ {
		rl := makeCorpusResourceLogs(i)

		canonicalBytes, err := canonical.CanonicalEncode(rl)
		require.NoError(t, err, "event %d: CanonicalEncode failed", i)

		sig, sigKeyID, err := signer.Sign(ctx, canonicalBytes)
		require.NoError(t, err, "event %d: Sign failed", i)

		// Verify immediately in Go — this is the Go half of the round-trip.
		require.NoError(t, signer.Verify(ctx, canonicalBytes, sig, sigKeyID),
			"event %d: Verify failed", i)

		manifest.Events[i] = corpusEntry{
			CanonicalB64: base64.StdEncoding.EncodeToString(canonicalBytes),
			SignatureB64: base64.StdEncoding.EncodeToString(sig),
		}
	}
	t.Logf("Go round-trip: %d events signed and verified in %v", corpusSize, time.Since(start))

	// Write manifest for the Python verifier.
	manifestPath := filepath.Join(t.TempDir(), "corpus_10k.json")
	raw, err := json.Marshal(manifest)
	require.NoError(t, err)
	require.NoError(t, os.WriteFile(manifestPath, raw, 0o644))

	// Cross-language step: run Python verifier if available.
	runPythonRoundTrip(t, manifestPath)
}

// runPythonRoundTrip calls the Python verifier script with manifestPath.
// It skips gracefully if python3 is unavailable or SKIP_PYTHON_ROUNDTRIP=1.
func runPythonRoundTrip(t *testing.T, manifestPath string) {
	t.Helper()

	if os.Getenv("SKIP_PYTHON_ROUNDTRIP") == "1" {
		t.Log("SKIP_PYTHON_ROUNDTRIP=1: skipping cross-language step")
		return
	}

	python, err := exec.LookPath("python3")
	if err != nil {
		t.Log("python3 not in PATH: skipping cross-language verification step")
		return
	}

	// Locate the verifier script relative to this test file.
	// Test working directory is collector/pkg/canonical/; script is at
	// collector/scripts/verify_canonical_roundtrip.py → ../../scripts/
	_, thisFile, _, ok := runtime.Caller(0)
	if !ok {
		t.Log("runtime.Caller failed: skipping cross-language step")
		return
	}
	script := filepath.Join(filepath.Dir(thisFile), "..", "..", "scripts", "verify_canonical_roundtrip.py")
	if _, statErr := os.Stat(script); statErr != nil {
		t.Logf("Python verifier not found at %s: skipping cross-language step", script)
		return
	}

	cmd := exec.Command(python, script, manifestPath)
	out, runErr := cmd.CombinedOutput()
	if runErr != nil {
		t.Fatalf("Python cross-language verifier failed: %v\nOutput:\n%s", runErr, out)
	}
	t.Logf("Cross-language round-trip: %s", out)
}

// makeCorpusResourceLogs constructs a ResourceLogs with varied attributes so
// the corpus exercises different attribute orderings and values.
func makeCorpusResourceLogs(idx int) *otlplogspb.ResourceLogs {
	// Vary attributes to exercise the sort + strip algorithm.
	resourceAttrs := []*otlpcommonv1.KeyValue{
		makeKV(fmt.Sprintf("nova.run_id"), fmt.Sprintf("run-%06d", idx)),
		makeKV("nova.agent_id", fmt.Sprintf("agent-%03d", idx%100)),
		makeKV("nova.cluster_id", fmt.Sprintf("cluster-%02d", idx%10)),
		makeKV("service.name", "novafabric-collector"),
		makeKV("host.name", fmt.Sprintf("node-%04d", idx%1000)),
		// Intentionally unsorted so the encoder must sort them.
		makeKV("zzz.last", "value"),
		makeKV("aaa.first", "value"),
	}

	logRecord := &otlplogspb.LogRecord{
		TimeUnixNano: uint64(1_700_000_000_000_000_000 + int64(idx)*1_000_000),
		Attributes: []*otlpcommonv1.KeyValue{
			makeKV("nova.event_type", "model_call"),
			makeKV("nova.event_id", fmt.Sprintf("ev%010d", idx)),
			makeKV("nova.envelope_version", "1"),
			makeKV("z.last", "z"),
			makeKV("a.first", "a"),
		},
	}

	return &otlplogspb.ResourceLogs{
		Resource: &otlpresourcepb.Resource{Attributes: resourceAttrs},
		ScopeLogs: []*otlplogspb.ScopeLogs{
			{LogRecords: []*otlplogspb.LogRecord{logRecord}},
		},
	}
}

// makeKV builds a string-valued OTLP KeyValue attribute.
func makeKV(key, val string) *otlpcommonv1.KeyValue {
	return &otlpcommonv1.KeyValue{
		Key: key,
		Value: &otlpcommonv1.AnyValue{
			Value: &otlpcommonv1.AnyValue_StringValue{StringValue: val},
		},
	}
}
