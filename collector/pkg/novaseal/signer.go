package novaseal

import (
	"context"
	"crypto/ecdsa"
	"crypto/ed25519"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"encoding/asn1"
	"encoding/base64"
	"fmt"
	"math/big"
)

// Ed25519Signer signs batches with a specific Ed25519 key. It implements the
// [Signer] interface defined in novaseal.go. It is immutable after
// construction and safe for concurrent use.
//
// Note on determinism: Go's crypto/ed25519 follows RFC 8032 §5.1, which
// specifies a deterministic algorithm. The same private key and message
// always produce the same signature — there is no per-call randomness. This
// differs from ECDSA, which uses a random nonce k.
type Ed25519Signer struct {
	keyID      string
	privateKey ed25519.PrivateKey
	publicKey  ed25519.PublicKey
}

// NewEd25519Signer constructs an Ed25519Signer from an existing private key.
// keyID must be the UUID assigned to this key in the KMS or local key store.
// priv must be a 64-byte Ed25519 private key (seed + public key).
func NewEd25519Signer(keyID string, priv ed25519.PrivateKey) (*Ed25519Signer, error) {
	if len(priv) != ed25519.PrivateKeySize {
		return nil, fmt.Errorf("novaseal: invalid Ed25519 private key length %d (want %d)",
			len(priv), ed25519.PrivateKeySize)
	}
	if keyID == "" {
		return nil, fmt.Errorf("novaseal: keyID must not be empty")
	}
	pub := priv.Public().(ed25519.PublicKey)
	return &Ed25519Signer{
		keyID:      keyID,
		privateKey: priv,
		publicKey:  pub,
	}, nil
}

// Sign computes a detached Ed25519 signature over canonical. The ctx parameter
// is reserved for future KMS-delegation; it is checked for cancellation before
// signing but Ed25519 itself does not block.
//
// Returns the raw 64-byte signature and the key UUID.
func (s *Ed25519Signer) Sign(ctx context.Context, canonical []byte) ([]byte, string, error) {
	if err := ctx.Err(); err != nil {
		return nil, "", fmt.Errorf("novaseal: context cancelled before Sign: %w", err)
	}
	if len(canonical) == 0 {
		return nil, "", fmt.Errorf("novaseal: canonical bytes must not be empty")
	}
	sig := ed25519.Sign(s.privateKey, canonical)
	return sig, s.keyID, nil
}

// Verify checks that sig was produced by the key identified by keyID over
// canonical. Returns nil on success.
//
// The keyID argument must match this signer's key; if it does not, an error is
// returned. This check ensures that the verifier does not silently accept
// signatures from the wrong key.
func (s *Ed25519Signer) Verify(ctx context.Context, canonical, sig []byte, keyID string) error {
	if err := ctx.Err(); err != nil {
		return fmt.Errorf("novaseal: context cancelled before Verify: %w", err)
	}
	if keyID != s.keyID {
		return fmt.Errorf("novaseal: key ID mismatch: got %q, signer owns %q", keyID, s.keyID)
	}
	if !ed25519.Verify(s.publicKey, canonical, sig) {
		return fmt.Errorf("novaseal: signature verification failed")
	}
	return nil
}

// PublicKey returns the Ed25519 public key associated with keyID.
// Returns an error if keyID does not match this signer's key.
func (s *Ed25519Signer) PublicKey(keyID string) (ed25519.PublicKey, error) {
	if keyID != s.keyID {
		return nil, fmt.Errorf("novaseal: unknown key ID %q", keyID)
	}
	// Return a copy so callers cannot modify our internal key.
	out := make(ed25519.PublicKey, len(s.publicKey))
	copy(out, s.publicKey)
	return out, nil
}

// KeyID returns this signer's key UUID.
func (s *Ed25519Signer) KeyID() string {
	return s.keyID
}

// SignatureBase64 returns the base64-encoded (standard encoding) form of a raw
// Ed25519 signature (64 bytes → 88 base64 chars), as required by the JSON
// Schema field nova.batch.signature.
func SignatureBase64(sig []byte) string {
	return base64.StdEncoding.EncodeToString(sig)
}

// SignatureFromBase64 decodes a base64-encoded signature from an EventEnvelope.
// Returns an error if the string is not valid base64 or the decoded length is
// not 64 bytes (ed25519.SignatureSize).
func SignatureFromBase64(s string) ([]byte, error) {
	b, err := base64.StdEncoding.DecodeString(s)
	if err != nil {
		return nil, fmt.Errorf("novaseal: base64 decode: %w", err)
	}
	if len(b) != ed25519.SignatureSize {
		return nil, fmt.Errorf("novaseal: signature length %d, want %d", len(b), ed25519.SignatureSize)
	}
	return b, nil
}

// ECDSASignatureFromBase64 decodes a base64-encoded DER ECDSA signature.
// ECDSA DER signatures are variable-length ASN.1 SEQUENCE structures; for
// P-256 they are typically 70–72 bytes. This function accepts 64–80 bytes to
// accommodate the full legal DER range for P-256 without false rejections.
func ECDSASignatureFromBase64(s string) ([]byte, error) {
	b, err := base64.StdEncoding.DecodeString(s)
	if err != nil {
		return nil, fmt.Errorf("novaseal: base64 decode: %w", err)
	}
	const minLen, maxLen = 64, 80
	if len(b) < minLen || len(b) > maxLen {
		return nil, fmt.Errorf("novaseal: ECDSA DER signature length %d out of expected range [%d, %d]",
			len(b), minLen, maxLen)
	}
	return b, nil
}

// ecdsaDERSig is the ASN.1 structure for a DER-encoded ECDSA signature.
type ecdsaDERSig struct {
	R, S *big.Int
}

// ECDSAP256Signer signs and verifies using ECDSA P-256 (secp256r1). Signatures
// are DER-encoded ASN.1 SEQUENCE { INTEGER r, INTEGER s } so they are
// compatible with Python's cryptography library default output format.
//
// ECDSAP256Signer is immutable after construction and safe for concurrent use.
type ECDSAP256Signer struct {
	keyID      string
	privateKey *ecdsa.PrivateKey
	publicKey  *ecdsa.PublicKey
}

// NewECDSAP256Signer constructs an ECDSAP256Signer from an existing private key.
// keyID must be the UUID assigned to this key in the KMS or local key store.
// priv must be a P-256 private key (elliptic.P256() curve).
func NewECDSAP256Signer(keyID string, priv *ecdsa.PrivateKey) (*ECDSAP256Signer, error) {
	if priv == nil {
		return nil, fmt.Errorf("novaseal: ECDSA private key must not be nil")
	}
	if priv.Curve != elliptic.P256() {
		return nil, fmt.Errorf("novaseal: expected P-256 curve, got %s", priv.Curve.Params().Name)
	}
	if keyID == "" {
		return nil, fmt.Errorf("novaseal: keyID must not be empty")
	}
	pub := priv.Public().(*ecdsa.PublicKey)
	return &ECDSAP256Signer{
		keyID:      keyID,
		privateKey: priv,
		publicKey:  pub,
	}, nil
}

// Sign computes a DER-encoded ECDSA P-256 signature over the SHA-256 digest of
// canonical. The ctx parameter is checked for cancellation before signing.
//
// Returns the DER-encoded signature bytes and the key UUID. Because ECDSA uses
// a random nonce k, the same message signed twice produces different (but both
// valid) signatures.
func (s *ECDSAP256Signer) Sign(ctx context.Context, canonical []byte) ([]byte, string, error) {
	if err := ctx.Err(); err != nil {
		return nil, "", fmt.Errorf("novaseal: context cancelled before Sign: %w", err)
	}
	if len(canonical) == 0 {
		return nil, "", fmt.Errorf("novaseal: canonical bytes must not be empty")
	}
	digest := sha256.Sum256(canonical)
	r, s2, err := ecdsa.Sign(rand.Reader, s.privateKey, digest[:])
	if err != nil {
		return nil, "", fmt.Errorf("novaseal: ECDSA sign: %w", err)
	}
	der, err := asn1.Marshal(ecdsaDERSig{R: r, S: s2})
	if err != nil {
		return nil, "", fmt.Errorf("novaseal: DER marshal: %w", err)
	}
	return der, s.keyID, nil
}

// Verify checks that sig (DER-encoded ECDSA signature) was produced by the key
// identified by keyID over canonical. Returns nil on success.
//
// The keyID argument must match this signer's key; if it does not, an error is
// returned.
func (s *ECDSAP256Signer) Verify(ctx context.Context, canonical, sig []byte, keyID string) error {
	if err := ctx.Err(); err != nil {
		return fmt.Errorf("novaseal: context cancelled before Verify: %w", err)
	}
	if keyID != s.keyID {
		return fmt.Errorf("novaseal: key ID mismatch: got %q, signer owns %q", keyID, s.keyID)
	}
	var parsed ecdsaDERSig
	rest, err := asn1.Unmarshal(sig, &parsed)
	if err != nil {
		return fmt.Errorf("novaseal: DER unmarshal: %w", err)
	}
	if len(rest) != 0 {
		return fmt.Errorf("novaseal: DER signature has %d trailing bytes", len(rest))
	}
	digest := sha256.Sum256(canonical)
	if !ecdsa.Verify(s.publicKey, digest[:], parsed.R, parsed.S) {
		return fmt.Errorf("novaseal: ECDSA signature verification failed")
	}
	return nil
}

// PublicKey returns the ECDSA P-256 public key associated with keyID.
// Returns an error if keyID does not match this signer's key.
func (s *ECDSAP256Signer) PublicKey(keyID string) (*ecdsa.PublicKey, error) {
	if keyID != s.keyID {
		return nil, fmt.Errorf("novaseal: unknown key ID %q", keyID)
	}
	return s.publicKey, nil
}

// KeyID returns this signer's key UUID.
func (s *ECDSAP256Signer) KeyID() string {
	return s.keyID
}

// GenerateECDSAP256KeyPair generates a new ECDSA P-256 keypair using a
// cryptographically secure random source. Exported for use by tests and
// local key-store helpers.
func GenerateECDSAP256KeyPair() (*ecdsa.PublicKey, *ecdsa.PrivateKey, error) {
	priv, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, nil, fmt.Errorf("novaseal: ECDSA P-256 key generation: %w", err)
	}
	return &priv.PublicKey, priv, nil
}
