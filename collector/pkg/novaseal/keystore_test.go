package novaseal_test

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/novafabric/collector/pkg/novaseal"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestLocalWALKeystore_FirstUseCreatesKeyFile verifies that the first call
// to GetActiveSigner creates a PEM file in DevKeysDir.
func TestLocalWALKeystore_FirstUseCreatesKeyFile(t *testing.T) {
	dir := t.TempDir()
	ks := novaseal.NewLocalWALKeystore(novaseal.LocalWALConfig{DevKeysDir: dir})

	signer, err := ks.GetActiveSigner(context.Background())
	require.NoError(t, err)
	require.NotNil(t, signer)

	entries, err := os.ReadDir(dir)
	require.NoError(t, err)

	var pemFiles []string
	for _, e := range entries {
		if filepath.Ext(e.Name()) == ".pem" {
			pemFiles = append(pemFiles, e.Name())
		}
	}
	require.Len(t, pemFiles, 1, "exactly one PEM file should be created on first use")

	// Verify the file has restricted permissions (0400).
	info, err := os.Stat(filepath.Join(dir, pemFiles[0]))
	require.NoError(t, err)
	assert.Equal(t, os.FileMode(0400), info.Mode().Perm(),
		"PEM file must have mode 0400")
}

// TestLocalWALKeystore_SecondUseLoadsSameKey verifies that two GetActiveSigner
// calls on the same instance return a signer with the same key ID, without
// creating a second PEM file.
func TestLocalWALKeystore_SecondUseLoadsSameKey(t *testing.T) {
	dir := t.TempDir()
	ks := novaseal.NewLocalWALKeystore(novaseal.LocalWALConfig{DevKeysDir: dir})

	s1, err := ks.GetActiveSigner(context.Background())
	require.NoError(t, err)

	s2, err := ks.GetActiveSigner(context.Background())
	require.NoError(t, err)

	// Both calls must return the same signer instance (cached).
	assert.Same(t, s1, s2, "subsequent calls must return the cached signer")

	// Only one PEM file should exist.
	entries, err := os.ReadDir(dir)
	require.NoError(t, err)
	var pemFiles []string
	for _, e := range entries {
		if filepath.Ext(e.Name()) == ".pem" {
			pemFiles = append(pemFiles, e.Name())
		}
	}
	assert.Len(t, pemFiles, 1, "second GetActiveSigner call must not create a new PEM file")
}

// TestLocalWALKeystore_PersistsAcrossInstances verifies that a new
// LocalWALKeystore pointing to the same directory loads the same key
// (stable key ID).
func TestLocalWALKeystore_PersistsAcrossInstances(t *testing.T) {
	dir := t.TempDir()

	// First instance — creates the key.
	ks1 := novaseal.NewLocalWALKeystore(novaseal.LocalWALConfig{DevKeysDir: dir})
	s1, err := ks1.GetActiveSigner(context.Background())
	require.NoError(t, err)

	// Capture the public key for later comparison.
	// We use the signer to sign a known message and check the key ID.
	ctx2 := context.Background()
	msg := []byte("hello-stability-test")
	sig1, keyID1, err := s1.Sign(ctx2, msg)
	require.NoError(t, err)

	// Second instance pointing to the same dir — must load the existing key.
	ks2 := novaseal.NewLocalWALKeystore(novaseal.LocalWALConfig{DevKeysDir: dir})
	s2, err := ks2.GetActiveSigner(context.Background())
	require.NoError(t, err)

	_, keyID2, err := s2.Sign(ctx2, msg)
	require.NoError(t, err)
	assert.Equal(t, keyID1, keyID2, "key ID must be stable across LocalWALKeystore instances")

	// The second instance must be able to verify signatures from the first.
	err = s2.Verify(ctx2, msg, sig1, keyID1)
	assert.NoError(t, err, "second instance must verify signatures from first instance")
}

// TestLocalWALKeystore_ErrorsInProduction verifies that GetActiveSigner
// returns an error when NOVAFABRIC_ENV=production.
func TestLocalWALKeystore_ErrorsInProduction(t *testing.T) {
	dir := t.TempDir()
	ks := novaseal.NewLocalWALKeystore(novaseal.LocalWALConfig{DevKeysDir: dir})

	t.Setenv("NOVAFABRIC_ENV", "production")

	_, err := ks.GetActiveSigner(context.Background())
	require.Error(t, err, "LocalWALKeystore must refuse to operate in production")
	assert.Contains(t, err.Error(), "production")
}

// TestLocalWALKeystore_SignerIsWorking verifies that the returned signer can
// complete a Sign → Verify round trip.
func TestLocalWALKeystore_SignerIsWorking(t *testing.T) {
	dir := t.TempDir()
	ks := novaseal.NewLocalWALKeystore(novaseal.LocalWALConfig{DevKeysDir: dir})

	signer, err := ks.GetActiveSigner(context.Background())
	require.NoError(t, err)

	ctx := context.Background()
	msg := []byte("canonical-payload")
	sig, keyID, err := signer.Sign(ctx, msg)
	require.NoError(t, err)
	require.NotEmpty(t, sig)

	err = signer.Verify(ctx, msg, sig, keyID)
	assert.NoError(t, err)
}

// TestLocalWALKeystore_CloseIsNoop verifies that Close can be called multiple
// times without panic.
func TestLocalWALKeystore_CloseIsNoop(t *testing.T) {
	ks := novaseal.NewLocalWALKeystore(novaseal.LocalWALConfig{DevKeysDir: t.TempDir()})
	assert.NotPanics(t, ks.Close)
	assert.NotPanics(t, ks.Close)
}
