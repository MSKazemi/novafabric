package novaseal_test

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/MSKazemi/novafabric/collector/pkg/novaseal"
)

// Review go-collector-ts-packages-16: the dev key was created with O_EXCL and written
// without a temp file or fsync, so a crash left an empty .pem; loadExisting then failed
// ("no PEM block") on every start until someone deleted it by hand.

func TestLocalWALRegeneratesOverAnEmptyKeyFileLeftByACrash(t *testing.T) {
	t.Setenv("NOVAFABRIC_ENV", "")
	dir := t.TempDir()
	empty := filepath.Join(dir, "11111111-1111-1111-1111-111111111111.pem")
	require.NoError(t, os.WriteFile(empty, nil, 0o400))

	ks := novaseal.NewLocalWALKeystore(novaseal.LocalWALConfig{DevKeysDir: dir})
	signer, err := ks.GetActiveSigner(context.Background())

	require.NoError(t, err, "an empty key file holds no key; it must not wedge every start")
	require.NotNil(t, signer)
	_, statErr := os.Stat(empty)
	assert.True(t, os.IsNotExist(statErr), "the empty remnant is removed")
	entries, err := os.ReadDir(dir)
	require.NoError(t, err)
	require.Len(t, entries, 1)
}

func TestLocalWALLeavesNoTempFileAndKeepsTheKeyReadOnly(t *testing.T) {
	t.Setenv("NOVAFABRIC_ENV", "")
	dir := t.TempDir()
	ks := novaseal.NewLocalWALKeystore(novaseal.LocalWALConfig{DevKeysDir: dir})
	_, err := ks.GetActiveSigner(context.Background())
	require.NoError(t, err)

	entries, err := os.ReadDir(dir)
	require.NoError(t, err)
	require.Len(t, entries, 1, "exactly the key file: no .tmp left behind")
	info, err := entries[0].Info()
	require.NoError(t, err)
	assert.Equal(t, os.FileMode(0o400), info.Mode().Perm())

	// A second keystore finds and loads it.
	again, err := novaseal.NewLocalWALKeystore(novaseal.LocalWALConfig{DevKeysDir: dir}).GetActiveSigner(context.Background())
	require.NoError(t, err)
	require.NotNil(t, again)
}
