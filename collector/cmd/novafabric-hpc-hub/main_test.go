package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// Review go-collector-ts-packages-09: the hub looked like a working signer (it took a key,
// logged "starting" and waited for SIGTERM) but processed nothing.

func noEnv(string) string { return "" }

func TestHubExitsNonZeroAsNotImplemented(t *testing.T) {
	var stderr bytes.Buffer
	code := run([]string{"--cluster-id", "hpc01", "--hub", "nats://h:4222"}, noEnv, &stderr)
	assert.Equal(t, exitNotImplemented, code)
	assert.Contains(t, stderr.String(), "not implemented")
}

func TestHubReadsAndCreatesNoKey(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	var stderr bytes.Buffer

	code := run([]string{"--cluster-id", "dev", "--local-wal"}, noEnv, &stderr)

	assert.Equal(t, exitNotImplemented, code)
	matches, err := filepath.Glob(filepath.Join(home, ".novafabric", "*"))
	require.NoError(t, err)
	assert.Empty(t, matches, "--local-wal must not generate a dev private key on disk")
	_, statErr := os.Stat(filepath.Join(home, ".novafabric"))
	assert.True(t, os.IsNotExist(statErr))
}

func TestHubDoesNotContactTheKMS(t *testing.T) {
	var stderr bytes.Buffer
	// An unroutable endpoint: if the hub tried to build a keystore it would block or error here.
	code := run([]string{"--cluster-id", "hpc01", "--kms", "https://192.0.2.1:8443"}, noEnv, &stderr)
	assert.Equal(t, exitNotImplemented, code)
	assert.NotContains(t, stderr.String(), "keystore")
}

func TestHubStillRequiresAClusterID(t *testing.T) {
	var stderr bytes.Buffer
	assert.Equal(t, exitUsage, run(nil, noEnv, &stderr))
	assert.Contains(t, stderr.String(), "--cluster-id")
}

func TestHubAcceptsTheEnvironmentDefaults(t *testing.T) {
	env := func(k string) string {
		if k == "NOVAFABRIC_CLUSTER_ID" {
			return "from-env"
		}
		return ""
	}
	var stderr bytes.Buffer
	assert.Equal(t, exitNotImplemented, run(nil, env, &stderr))
	assert.True(t, strings.Contains(stderr.String(), "from-env"))
}

func TestHubReportsAnUnknownFlagAsUsage(t *testing.T) {
	var stderr bytes.Buffer
	assert.Equal(t, exitUsage, run([]string{"--no-such-flag"}, noEnv, &stderr))
}
