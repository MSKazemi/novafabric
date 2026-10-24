// Command novafabric-verifier verifies NovaSeal Ed25519 batch signatures offline.
//
// Usage:
//
//	novafabric-verifier --batch batch.pb --pubkey kms-public.pem --key-id <uuid>
//	novafabric-verifier --batch batch.pb --local-wal   # uses dev key from ~/.novafabric/dev-keys/
//
// Exit codes: 0 the signature verifies (prints "OK key_id=…"); 1 it does not, or an
// input could not be read (prints "INVALID …" for a signature failure); 2 usage error.
package main

import (
	"context"
	"crypto/ed25519"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"

	otlplogspb "go.opentelemetry.io/proto/otlp/logs/v1"
	"google.golang.org/protobuf/proto"

	"github.com/MSKazemi/novafabric/collector/internal/verifier"
	"github.com/MSKazemi/novafabric/collector/pkg/novaseal"
)

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

// run is the whole command, returning its exit code, so tests can drive it.
//
// Review go-collector-ts-packages-02: the verifier passed a nil context.Context to
// Signer.Verify, whose first act is ctx.Err(), so every signed batch panicked with
// a nil-pointer dereference — the tool could never print OK. It now uses
// context.Background().
func run(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("novafabric-verifier", flag.ContinueOnError)
	fs.SetOutput(stderr)
	batchPath := fs.String("batch", "", "Path to serialized OTLP ResourceLogs proto file (required)")
	pubkeyPath := fs.String("pubkey", "", "Path to a PEM Ed25519 public key (SPKI \"PUBLIC KEY\", or a raw 32-byte body)")
	localWAL := fs.Bool("local-wal", false, "Use local dev key from ~/.novafabric/dev-keys/ for verification")
	keyID := fs.String("key-id", "", "Key ID to verify against (required with --pubkey)")
	if err := fs.Parse(args); err != nil {
		return 2
	}

	if *batchPath == "" {
		fmt.Fprintln(stderr, "error: --batch is required")
		fs.Usage()
		return 2
	}

	batchBytes, err := os.ReadFile(*batchPath)
	if err != nil {
		fmt.Fprintf(stderr, "error: failed to read batch file %s: %v\n", *batchPath, err)
		return 1
	}

	var rl otlplogspb.ResourceLogs
	if err := proto.Unmarshal(batchBytes, &rl); err != nil {
		fmt.Fprintf(stderr, "error: failed to unmarshal ResourceLogs proto: %v\n", err)
		return 1
	}

	ctx := context.Background()
	var signer novaseal.Signer

	switch {
	case *localWAL:
		kstore := novaseal.NewLocalWALKeystore(novaseal.LocalWALConfig{})
		s, err := kstore.GetActiveSigner(ctx)
		if err != nil {
			fmt.Fprintf(stderr, "error: local-wal: failed to get signer: %v\n", err)
			return 1
		}
		signer = s

	case *pubkeyPath != "":
		if *keyID == "" {
			fmt.Fprintln(stderr, "error: --key-id is required with --pubkey")
			return 2
		}
		pemBytes, err := os.ReadFile(*pubkeyPath)
		if err != nil {
			fmt.Fprintf(stderr, "error: failed to read public key %s: %v\n", *pubkeyPath, err)
			return 1
		}
		pub, err := parsePublicKey(pemBytes)
		if err != nil {
			fmt.Fprintf(stderr, "error: %v\n", err)
			return 1
		}
		s, err := verifyOnlySigner(*keyID, pub)
		if err != nil {
			fmt.Fprintf(stderr, "error: failed to create verifier: %v\n", err)
			return 1
		}
		signer = s

	default:
		fmt.Fprintln(stderr, "error: one of --pubkey or --local-wal is required")
		fs.Usage()
		return 2
	}

	result := verifier.VerifyResourceLogs(ctx, &rl, signer)
	if result.Valid {
		fmt.Fprintf(stdout, "OK key_id=%s\n", result.KeyID)
		return 0
	}
	fmt.Fprintf(stdout, "INVALID key_id=%s error=%s\n", result.KeyID, result.Error)
	return 1
}

// parsePublicKey reads an Ed25519 public key from PEM: the standard SPKI
// "PUBLIC KEY" encoding a KMS exports, or a raw 32-byte body. It accepted only the
// raw body before, so a standard key file was refused as the wrong size.
func parsePublicKey(pemBytes []byte) (ed25519.PublicKey, error) {
	block, _ := pem.Decode(pemBytes)
	if block == nil {
		return nil, errors.New("failed to decode a PEM block from the public key file")
	}
	if len(block.Bytes) == ed25519.PublicKeySize {
		return ed25519.PublicKey(block.Bytes), nil
	}
	parsed, err := x509.ParsePKIXPublicKey(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("public key is neither a raw Ed25519 key nor SPKI: %w", err)
	}
	pub, ok := parsed.(ed25519.PublicKey)
	if !ok {
		return nil, fmt.Errorf("public key is %T, not Ed25519", parsed)
	}
	return pub, nil
}

// verifyOnlySigner wraps a public key in an Ed25519Signer for Verify. The private
// half is zero — it can verify, and any Sign attempt yields a signature that
// verifies under no key.
func verifyOnlySigner(keyID string, pub ed25519.PublicKey) (*novaseal.Ed25519Signer, error) {
	priv := make(ed25519.PrivateKey, ed25519.PrivateKeySize)
	copy(priv[ed25519.SeedSize:], pub)
	return novaseal.NewEd25519Signer(keyID, priv)
}
