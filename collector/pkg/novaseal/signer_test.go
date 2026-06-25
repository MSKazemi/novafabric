package novaseal_test

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/ed25519"
	"crypto/rand"
	"testing"

	"github.com/novafabric/collector/pkg/novaseal"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

const testKeyID = "11111111-1111-1111-1111-111111111111"

// newTestSigner generates a fresh Ed25519Signer for use in tests.
func newTestSigner(t *testing.T) *novaseal.Ed25519Signer {
	t.Helper()
	_, priv, err := novaseal.GenerateEd25519KeyPair()
	require.NoError(t, err)
	s, err := novaseal.NewEd25519Signer(testKeyID, priv)
	require.NoError(t, err)
	return s
}

// TestEd25519Signer_RoundTrip verifies that Sign followed by Verify succeeds.
func TestEd25519Signer_RoundTrip(t *testing.T) {
	ctx := context.Background()
	s := newTestSigner(t)
	msg := []byte("canonical-bytes-from-adr-001")

	sig, keyID, err := s.Sign(ctx, msg)
	require.NoError(t, err)
	assert.Equal(t, testKeyID, keyID)
	assert.Len(t, sig, ed25519.SignatureSize, "signature must be 64 bytes")

	require.NoError(t, s.Verify(ctx, msg, sig, keyID))
}

// TestEd25519Signer_VerifyFailsOnMutation verifies that flipping a single
// byte in the signature causes Verify to return an error.
func TestEd25519Signer_VerifyFailsOnMutation(t *testing.T) {
	ctx := context.Background()
	s := newTestSigner(t)
	msg := []byte("canonical-bytes-from-adr-001")

	sig, keyID, err := s.Sign(ctx, msg)
	require.NoError(t, err)

	// Mutate the first byte of the signature.
	mutated := make([]byte, len(sig))
	copy(mutated, sig)
	mutated[0] ^= 0xFF

	err = s.Verify(ctx, msg, mutated, keyID)
	assert.Error(t, err, "verification must fail after signature mutation")
}

// TestEd25519Signer_VerifyFailsOnMutatedMessage verifies that changing the
// message after signing causes verification to fail.
func TestEd25519Signer_VerifyFailsOnMutatedMessage(t *testing.T) {
	ctx := context.Background()
	s := newTestSigner(t)
	original := []byte("canonical-bytes-from-adr-001")
	modified := []byte("canonical-bytes-from-adr-001X")

	sig, keyID, err := s.Sign(ctx, original)
	require.NoError(t, err)

	err = s.Verify(ctx, modified, sig, keyID)
	assert.Error(t, err, "verification must fail when message differs from what was signed")
}

// TestEd25519Signer_Deterministic verifies that Go's crypto/ed25519 Sign is
// deterministic: the same key + message always produce the same signature.
//
// Ed25519 in Go follows RFC 8032 §5.1, which specifies a deterministic nonce
// derived from the private key and message. This differs from ECDSA (which
// uses a random k). Callers should not rely on non-determinism for security
// properties.
func TestEd25519Signer_Deterministic(t *testing.T) {
	ctx := context.Background()
	s := newTestSigner(t)
	msg := []byte("canonical-bytes-from-adr-001")

	sig1, _, err := s.Sign(ctx, msg)
	require.NoError(t, err)

	sig2, _, err := s.Sign(ctx, msg)
	require.NoError(t, err)

	assert.Equal(t, sig1, sig2,
		"Ed25519 (RFC 8032) is deterministic: same key+message → same signature")
}

// TestEd25519Signer_PublicKeyCorrect verifies that PublicKey returns the key
// that corresponds to the private key used to construct the signer.
func TestEd25519Signer_PublicKeyCorrect(t *testing.T) {
	pub, priv, err := novaseal.GenerateEd25519KeyPair()
	require.NoError(t, err)

	s, err := novaseal.NewEd25519Signer(testKeyID, priv)
	require.NoError(t, err)

	got, err := s.PublicKey(testKeyID)
	require.NoError(t, err)
	assert.Equal(t, []byte(pub), []byte(got))
}

// TestEd25519Signer_PublicKeyWrongID verifies that PublicKey returns an error
// for an unknown key ID.
func TestEd25519Signer_PublicKeyWrongID(t *testing.T) {
	s := newTestSigner(t)
	_, err := s.PublicKey("wrong-id")
	assert.Error(t, err)
}

// TestEd25519Signer_VerifyWrongKeyID verifies that Verify returns an error
// when the provided key ID does not match the signer's key.
func TestEd25519Signer_VerifyWrongKeyID(t *testing.T) {
	ctx := context.Background()
	s := newTestSigner(t)
	msg := []byte("data")
	sig, _, err := s.Sign(ctx, msg)
	require.NoError(t, err)

	err = s.Verify(ctx, msg, sig, "different-key-id")
	assert.Error(t, err)
}

// TestNewEd25519Signer_InvalidKey verifies that a key of wrong length is
// rejected at construction time.
func TestNewEd25519Signer_InvalidKey(t *testing.T) {
	_, err := novaseal.NewEd25519Signer(testKeyID, ed25519.PrivateKey([]byte("short")))
	assert.Error(t, err)
}

// TestEd25519Signer_EmptyMessage verifies that signing an empty payload
// returns an error.
func TestEd25519Signer_EmptyMessage(t *testing.T) {
	ctx := context.Background()
	s := newTestSigner(t)
	_, _, err := s.Sign(ctx, []byte{})
	assert.Error(t, err)
}

// TestEd25519Signer_CancelledContext verifies that Sign returns an error when
// the context is already cancelled.
func TestEd25519Signer_CancelledContext(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel() // cancelled immediately

	s := newTestSigner(t)
	_, _, err := s.Sign(ctx, []byte("data"))
	assert.Error(t, err)
}

// TestSignatureBase64 verifies that the base64 helper round-trips correctly.
func TestSignatureBase64(t *testing.T) {
	ctx := context.Background()
	s := newTestSigner(t)
	sig, _, err := s.Sign(ctx, []byte("hello"))
	require.NoError(t, err)

	encoded := novaseal.SignatureBase64(sig)
	assert.Len(t, encoded, 88, "base64 of 64 bytes must be 88 chars (standard encoding)")

	decoded, err := novaseal.SignatureFromBase64(encoded)
	require.NoError(t, err)
	assert.Equal(t, sig, decoded)
}

// --- ECDSA P-256 tests ---

// newTestECDSASigner generates a fresh ECDSAP256Signer for use in tests.
func newTestECDSASigner(t *testing.T) *novaseal.ECDSAP256Signer {
	t.Helper()
	_, priv, err := novaseal.GenerateECDSAP256KeyPair()
	require.NoError(t, err)
	s, err := novaseal.NewECDSAP256Signer(testKeyID, priv)
	require.NoError(t, err)
	return s
}

// TestECDSAP256Signer_SignVerify verifies that Sign followed by Verify succeeds
// and that the DER signature length is in the expected range for P-256.
func TestECDSAP256Signer_SignVerify(t *testing.T) {
	ctx := context.Background()
	s := newTestECDSASigner(t)
	msg := []byte("canonical-bytes-from-adr-001")

	sig, keyID, err := s.Sign(ctx, msg)
	require.NoError(t, err)
	assert.Equal(t, testKeyID, keyID)
	// DER-encoded ECDSA P-256 signatures are 70–72 bytes in the typical case,
	// and always in [64, 80] for P-256.
	assert.GreaterOrEqual(t, len(sig), 64, "DER signature must be at least 64 bytes")
	assert.LessOrEqual(t, len(sig), 80, "DER signature must be at most 80 bytes")

	require.NoError(t, s.Verify(ctx, msg, sig, keyID))
}

// TestECDSAP256Signer_WrongKey verifies that verification fails when the
// signature was produced by a different key.
func TestECDSAP256Signer_WrongKey(t *testing.T) {
	ctx := context.Background()

	// signer1 produces the signature.
	_, priv1, err := novaseal.GenerateECDSAP256KeyPair()
	require.NoError(t, err)
	s1, err := novaseal.NewECDSAP256Signer(testKeyID, priv1)
	require.NoError(t, err)

	// signer2 owns a different key but the same keyID.
	_, priv2, err := novaseal.GenerateECDSAP256KeyPair()
	require.NoError(t, err)
	s2, err := novaseal.NewECDSAP256Signer(testKeyID, priv2)
	require.NoError(t, err)

	msg := []byte("canonical-bytes-from-adr-001")
	sig, keyID, err := s1.Sign(ctx, msg)
	require.NoError(t, err)

	// Verifying s1's signature using s2's public key must fail.
	err = s2.Verify(ctx, msg, sig, keyID)
	assert.Error(t, err, "verification must fail when key does not match the signer")
}

// TestECDSAP256Signer_Determinism verifies that ECDSA is non-deterministic by
// default: signing the same message twice produces different byte sequences,
// yet both signatures verify successfully. The test asserts only that both
// verify — it does not require the signatures to differ (they almost always
// will, but a collision is theoretically possible with negligible probability).
func TestECDSAP256Signer_Determinism(t *testing.T) {
	ctx := context.Background()
	s := newTestECDSASigner(t)
	msg := []byte("canonical-bytes-from-adr-001")

	sig1, keyID, err := s.Sign(ctx, msg)
	require.NoError(t, err)

	sig2, _, err := s.Sign(ctx, msg)
	require.NoError(t, err)

	// Both signatures must verify against the same message.
	require.NoError(t, s.Verify(ctx, msg, sig1, keyID), "first signature must verify")
	require.NoError(t, s.Verify(ctx, msg, sig2, keyID), "second signature must verify")
}

// TestECDSAP256Signer_PublicKeyCorrect verifies that PublicKey returns the
// key matching the private key used at construction time.
func TestECDSAP256Signer_PublicKeyCorrect(t *testing.T) {
	pub, priv, err := novaseal.GenerateECDSAP256KeyPair()
	require.NoError(t, err)

	s, err := novaseal.NewECDSAP256Signer(testKeyID, priv)
	require.NoError(t, err)

	got, err := s.PublicKey(testKeyID)
	require.NoError(t, err)
	assert.True(t, pub.Equal(got), "returned public key must match the generated key")
}

// TestECDSAP256Signer_WrongKeyID verifies that Verify returns an error when the
// provided key ID does not match the signer's own key ID.
func TestECDSAP256Signer_WrongKeyID(t *testing.T) {
	ctx := context.Background()
	s := newTestECDSASigner(t)
	msg := []byte("data")

	sig, _, err := s.Sign(ctx, msg)
	require.NoError(t, err)

	err = s.Verify(ctx, msg, sig, "different-key-id")
	assert.Error(t, err, "Verify must fail when key ID does not match")
}

// TestECDSAP256Signer_EmptyMessage verifies that signing an empty payload
// returns an error.
func TestECDSAP256Signer_EmptyMessage(t *testing.T) {
	ctx := context.Background()
	s := newTestECDSASigner(t)
	_, _, err := s.Sign(ctx, []byte{})
	assert.Error(t, err)
}

// TestECDSAP256Signer_CancelledContext verifies that Sign returns an error
// when the context is already cancelled.
func TestECDSAP256Signer_CancelledContext(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	s := newTestECDSASigner(t)
	_, _, err := s.Sign(ctx, []byte("data"))
	assert.Error(t, err)
}

// TestNewECDSAP256Signer_NilKey verifies that a nil private key is rejected.
func TestNewECDSAP256Signer_NilKey(t *testing.T) {
	_, err := novaseal.NewECDSAP256Signer(testKeyID, nil)
	assert.Error(t, err)
}

// TestNewECDSAP256Signer_WrongCurve verifies that a non-P-256 key is rejected.
func TestNewECDSAP256Signer_WrongCurve(t *testing.T) {
	// Use P-384, which is a different curve.
	priv384, err := ecdsa.GenerateKey(elliptic.P384(), rand.Reader)
	require.NoError(t, err)
	_, err = novaseal.NewECDSAP256Signer(testKeyID, priv384)
	assert.Error(t, err, "P-384 key must be rejected by ECDSAP256Signer")
}

// TestECDSASignatureFromBase64_Valid verifies the ECDSA base64 helper
// round-trips a real DER signature.
func TestECDSASignatureFromBase64_Valid(t *testing.T) {
	ctx := context.Background()
	s := newTestECDSASigner(t)
	sig, _, err := s.Sign(ctx, []byte("hello"))
	require.NoError(t, err)

	encoded := novaseal.SignatureBase64(sig)
	decoded, err := novaseal.ECDSASignatureFromBase64(encoded)
	require.NoError(t, err)
	assert.Equal(t, sig, decoded)
}

// TestECDSAP256Signer_VerifyMutatedSig verifies that flipping a byte in the
// DER signature causes Verify to return an error.
func TestECDSAP256Signer_VerifyMutatedSig(t *testing.T) {
	ctx := context.Background()
	s := newTestECDSASigner(t)
	msg := []byte("canonical-bytes-from-adr-001")

	sig, keyID, err := s.Sign(ctx, msg)
	require.NoError(t, err)

	mutated := make([]byte, len(sig))
	copy(mutated, sig)
	mutated[len(mutated)-1] ^= 0xFF

	err = s.Verify(ctx, msg, mutated, keyID)
	assert.Error(t, err, "verification must fail after signature mutation")
}

// TestECDSAP256Implements_SignerInterface is a compile-time check that
// *ECDSAP256Signer satisfies the novaseal.Signer interface.
func TestECDSAP256Implements_SignerInterface(t *testing.T) {
	var _ novaseal.Signer = (*novaseal.ECDSAP256Signer)(nil)
}

// TestEd25519Implements_SignerInterface is a compile-time check that
// *Ed25519Signer satisfies the novaseal.Signer interface.
func TestEd25519Implements_SignerInterface(t *testing.T) {
	var _ novaseal.Signer = (*novaseal.Ed25519Signer)(nil)
}

// TestECDSAP256Signer_VerifyCancelledContext verifies that Verify returns an
// error when the context is already cancelled.
func TestECDSAP256Signer_VerifyCancelledContext(t *testing.T) {
	bg := context.Background()
	s := newTestECDSASigner(t)
	sig, keyID, err := s.Sign(bg, []byte("data"))
	require.NoError(t, err)

	ctx, cancel := context.WithCancel(bg)
	cancel()
	err = s.Verify(ctx, []byte("data"), sig, keyID)
	assert.Error(t, err)
}

