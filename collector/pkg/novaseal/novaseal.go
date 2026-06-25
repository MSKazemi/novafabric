// Package novaseal defines the Signer interface used by the NovaSeal batch
// signing processor and the verifier.
//
// The interface is intentionally minimal so that implementations can range
// from a local in-memory HMAC signer (for tests and development) to a
// full KMS-backed remote signing service.
package novaseal

import "context"

// Signer signs and verifies canonical byte sequences.  All methods must be
// safe for concurrent use by multiple goroutines.
type Signer interface {
	// Sign computes a signature over canonical and returns the opaque signature
	// bytes and the key ID used to produce them.  If signing fails the caller
	// should inspect the error and decide whether to fail-open or fail-closed
	// based on its configuration.
	Sign(ctx context.Context, canonical []byte) (signature []byte, keyID string, err error)

	// Verify checks that sig is a valid signature over canonical using the key
	// identified by keyID.  Returns nil if and only if the signature is valid.
	Verify(ctx context.Context, canonical []byte, sig []byte, keyID string) error
}
