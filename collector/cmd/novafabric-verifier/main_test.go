package main

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/x509"
	"encoding/base64"
	"encoding/pem"
	"os"
	"path/filepath"
	"strings"
	"testing"

	otlpcommonpb "go.opentelemetry.io/proto/otlp/common/v1"
	otlplogspb "go.opentelemetry.io/proto/otlp/logs/v1"
	otlpresourcepb "go.opentelemetry.io/proto/otlp/resource/v1"
	"google.golang.org/protobuf/proto"

	"github.com/MSKazemi/novafabric/collector/pkg/canonical"
	"github.com/MSKazemi/novafabric/collector/pkg/novaseal"
)

const testKeyID = "key-verifier-test"

func strAttr(k, v string) *otlpcommonpb.KeyValue {
	return &otlpcommonpb.KeyValue{
		Key:   k,
		Value: &otlpcommonpb.AnyValue{Value: &otlpcommonpb.AnyValue_StringValue{StringValue: v}},
	}
}

// signedBatch writes a ResourceLogs signed the way the batch-signer processor signs it.
func signedBatch(t *testing.T, dir string, priv ed25519.PrivateKey) string {
	t.Helper()
	rl := &otlplogspb.ResourceLogs{
		Resource: &otlpresourcepb.Resource{Attributes: []*otlpcommonpb.KeyValue{
			strAttr("service.name", "capture"),
		}},
		ScopeLogs: []*otlplogspb.ScopeLogs{{LogRecords: []*otlplogspb.LogRecord{{
			Body: &otlpcommonpb.AnyValue{Value: &otlpcommonpb.AnyValue_StringValue{StringValue: "event"}},
		}}}},
	}
	signer, err := novaseal.NewEd25519Signer(testKeyID, priv)
	if err != nil {
		t.Fatal(err)
	}
	canonicalBytes, err := canonical.CanonicalEncode(rl)
	if err != nil {
		t.Fatal(err)
	}
	sig, keyID, err := signer.Sign(context.Background(), canonicalBytes)
	if err != nil {
		t.Fatal(err)
	}
	rl.Resource.Attributes = append(rl.Resource.Attributes,
		strAttr("nova.batch.signature", base64.StdEncoding.EncodeToString(sig)),
		strAttr("nova.batch.signing_key_id", keyID),
	)
	raw, err := proto.Marshal(rl)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "batch.pb")
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func writePEM(t *testing.T, dir, name string, der []byte) string {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: der}), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

// Review go-collector-ts-packages-02: the verifier passed a nil context to
// Signer.Verify, so every signed batch panicked and it could never print OK.
func TestVerifierAcceptsASignedBatch(t *testing.T) {
	dir := t.TempDir()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	batch := signedBatch(t, dir, priv)
	spki, err := x509.MarshalPKIXPublicKey(pub)
	if err != nil {
		t.Fatal(err)
	}
	for name, keyPath := range map[string]string{
		// The standard "PUBLIC KEY" PEM a KMS exports is SPKI-encoded; a raw
		// 32-byte body is accepted too.
		"spki": writePEM(t, dir, "spki.pem", spki),
		"raw":  writePEM(t, dir, "raw.pem", pub),
	} {
		var stdout, stderr bytes.Buffer
		code := run([]string{"--batch", batch, "--pubkey", keyPath, "--key-id", testKeyID}, &stdout, &stderr)
		if code != 0 || !strings.Contains(stdout.String(), "OK key_id="+testKeyID) {
			t.Fatalf("%s: exit %d, stdout %q, stderr %q", name, code, stdout.String(), stderr.String())
		}
	}
}

func TestVerifierRejectsATamperedBatch(t *testing.T) {
	dir := t.TempDir()
	_, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	batch := signedBatch(t, dir, priv)
	otherPub, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	spki, err := x509.MarshalPKIXPublicKey(otherPub)
	if err != nil {
		t.Fatal(err)
	}
	var stdout, stderr bytes.Buffer
	code := run([]string{"--batch", batch, "--pubkey", writePEM(t, dir, "other.pem", spki), "--key-id", testKeyID}, &stdout, &stderr)
	if code != 1 || !strings.Contains(stdout.String(), "INVALID") {
		t.Fatalf("exit %d, stdout %q", code, stdout.String())
	}
}
