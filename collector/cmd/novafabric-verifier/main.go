// Command novafabric-verifier verifies NovaSeal Ed25519 batch signatures offline.
//
// Usage:
//
//	novafabric-verifier --batch batch.pb --pubkey kms-public.pem
//	novafabric-verifier --batch batch.pb --local-wal   # uses dev key from ~/.novafabric/dev-keys/
package main

import (
	"encoding/pem"
	"crypto/ed25519"
	"flag"
	"fmt"
	"log/slog"
	"os"

	otlplogspb "go.opentelemetry.io/proto/otlp/logs/v1"
	"google.golang.org/protobuf/proto"

	"github.com/novafabric/collector/internal/verifier"
	"github.com/novafabric/collector/pkg/novaseal"
)

func main() {
	batchPath := flag.String("batch", "", "Path to serialized OTLP ResourceLogs proto file (required)")
	pubkeyPath := flag.String("pubkey", "", "Path to PEM-encoded Ed25519 public key")
	localWAL := flag.Bool("local-wal", false, "Use local dev key from ~/.novafabric/dev-keys/ for verification")
	keyID := flag.String("key-id", "", "Key ID to verify against (required with --pubkey)")
	flag.Parse()

	if *batchPath == "" {
		fmt.Fprintln(os.Stderr, "error: --batch is required")
		flag.Usage()
		os.Exit(2)
	}

	batchBytes, err := os.ReadFile(*batchPath)
	if err != nil {
		slog.Error("failed to read batch file", "path", *batchPath, "error", err)
		os.Exit(1)
	}

	var rl otlplogspb.ResourceLogs
	if err := proto.Unmarshal(batchBytes, &rl); err != nil {
		slog.Error("failed to unmarshal ResourceLogs proto", "error", err)
		os.Exit(1)
	}

	var signer novaseal.Signer

	switch {
	case *localWAL:
		kstore := novaseal.NewLocalWALKeystore(novaseal.LocalWALConfig{})
		s, err := kstore.GetActiveSigner(nil) //nolint:staticcheck
		if err != nil {
			slog.Error("local-wal: failed to get signer", "error", err)
			os.Exit(1)
		}
		signer = s

	case *pubkeyPath != "":
		if *keyID == "" {
			fmt.Fprintln(os.Stderr, "error: --key-id is required with --pubkey")
			os.Exit(2)
		}
		pubBytes, err := os.ReadFile(*pubkeyPath)
		if err != nil {
			slog.Error("failed to read public key", "path", *pubkeyPath, "error", err)
			os.Exit(1)
		}
		block, _ := pem.Decode(pubBytes)
		if block == nil {
			slog.Error("failed to decode PEM block from public key file")
			os.Exit(1)
		}
		if len(block.Bytes) != ed25519.PublicKeySize {
			slog.Error("unexpected public key size", "got", len(block.Bytes), "want", ed25519.PublicKeySize)
			os.Exit(1)
		}
		pub := ed25519.PublicKey(block.Bytes)
		priv := make(ed25519.PrivateKey, ed25519.PrivateKeySize)
		copy(priv[32:], pub)
		s, err := novaseal.NewEd25519Signer(*keyID, priv)
		if err != nil {
			slog.Error("failed to create signer", "error", err)
			os.Exit(1)
		}
		signer = s

	default:
		fmt.Fprintln(os.Stderr, "error: one of --pubkey or --local-wal is required")
		flag.Usage()
		os.Exit(2)
	}

	result := verifier.VerifyResourceLogs(nil, &rl, signer) //nolint:staticcheck
	if result.Valid {
		fmt.Printf("OK key_id=%s\n", result.KeyID)
		os.Exit(0)
	} else {
		fmt.Printf("INVALID key_id=%s error=%s\n", result.KeyID, result.Error)
		os.Exit(1)
	}
}
