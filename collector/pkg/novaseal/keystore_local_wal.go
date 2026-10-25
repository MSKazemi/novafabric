package novaseal

import (
	"context"
	"crypto/ed25519"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sync"
	"syscall"

	"github.com/google/uuid"
)

// LocalWALKeystore is a single-node key store for development and CI that
// generates an Ed25519 keypair on first use and persists it to a local PEM
// file.  It is NOT suitable for production use.
//
// By default the key is stored under ~/.novafabric/dev-keys/<keyID>.pem with
// file mode 0400. Supply DevKeysDir in [LocalWALConfig] to override this
// location (useful in tests).
//
// LocalWALKeystore implements the same surface as [Keystore] so that
// downstream code can treat both interchangeably through a narrow interface.
type LocalWALKeystore struct {
	cfg LocalWALConfig

	mu     sync.Mutex
	signer *Ed25519Signer
	keyID  string
}

// LocalWALConfig configures a [LocalWALKeystore].
type LocalWALConfig struct {
	// DevKeysDir overrides the directory used to store the PEM file.
	// Defaults to ~/.novafabric/dev-keys/.
	DevKeysDir string
}

// devKeysDirDefault returns the default directory for dev key PEM files.
func devKeysDirDefault() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("novaseal: cannot determine home directory: %w", err)
	}
	return filepath.Join(home, ".novafabric", "dev-keys"), nil
}

// NewLocalWALKeystore constructs a LocalWALKeystore. The key is not generated
// until the first call to GetActiveSigner.
func NewLocalWALKeystore(cfg LocalWALConfig) *LocalWALKeystore {
	return &LocalWALKeystore{cfg: cfg}
}

// GetActiveSigner returns a [Signer] backed by the local dev key.
//
// On first call it generates a new Ed25519 keypair, writes the private key
// to DevKeysDir/<uuid>.pem (mode 0400), and caches the signer. Subsequent
// calls return the same signer without re-reading the file.
//
// Returns an error if NOVAFABRIC_ENV is set to "production".
func (lw *LocalWALKeystore) GetActiveSigner(_ context.Context) (Signer, error) {
	if os.Getenv("NOVAFABRIC_ENV") == "production" {
		return nil, errors.New("novaseal: LocalWALKeystore must not be used in production (NOVAFABRIC_ENV=production)")
	}

	slog.Warn("novaseal: USING LOCAL DEV KEY — NOT FOR PRODUCTION")

	lw.mu.Lock()
	defer lw.mu.Unlock()

	if lw.signer != nil {
		return lw.signer, nil
	}

	dir, err := lw.keysDir()
	if err != nil {
		return nil, err
	}

	// Try to load an existing key first (idempotent across process restarts).
	signer, keyID, err := lw.loadExisting(dir)
	if err != nil {
		return nil, err
	}
	if signer != nil {
		lw.signer = signer
		lw.keyID = keyID
		return signer, nil
	}

	// No existing key found — generate a new one.
	return lw.generateAndPersist(dir)
}

// Close is a no-op for LocalWALKeystore. It satisfies the same surface as
// Keystore so callers can defer ks.Close() unconditionally.
func (lw *LocalWALKeystore) Close() {}

// keysDir returns the resolved dev-keys directory, creating it if necessary.
func (lw *LocalWALKeystore) keysDir() (string, error) {
	dir := lw.cfg.DevKeysDir
	if dir == "" {
		var err error
		dir, err = devKeysDirDefault()
		if err != nil {
			return "", err
		}
	}
	if err := os.MkdirAll(dir, 0700); err != nil {
		return "", fmt.Errorf("novaseal: create dev-keys dir %q: %w", dir, err)
	}
	return dir, nil
}

// loadExisting scans dir for a *.pem file. If exactly one is found it is
// loaded and a signer is returned. If none are found, (nil, "", nil) is
// returned. If multiple are found, an error is returned.
func (lw *LocalWALKeystore) loadExisting(dir string) (*Ed25519Signer, string, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, "", fmt.Errorf("novaseal: read dev-keys dir: %w", err)
	}

	var pemFiles []string
	for _, e := range entries {
		if !e.IsDir() && filepath.Ext(e.Name()) == ".pem" {
			path := filepath.Join(dir, e.Name())
			// A zero-length key file holds no key: an earlier version created it with O_EXCL
			// and crashed before writing. Remove it so a new key can be generated.
			if info, statErr := e.Info(); statErr == nil && info.Size() == 0 {
				slog.Warn("novaseal: removing an empty dev key file left by a crash", "path", path)
				if rmErr := os.Remove(path); rmErr != nil {
					return nil, "", fmt.Errorf("novaseal: remove empty dev key %q: %w", path, rmErr)
				}
				continue
			}
			pemFiles = append(pemFiles, path)
		}
	}

	if len(pemFiles) == 0 {
		return nil, "", nil
	}
	if len(pemFiles) > 1 {
		return nil, "", fmt.Errorf("novaseal: multiple dev key PEM files found in %q; remove all but one", dir)
	}

	return loadPEMSigner(pemFiles[0])
}

// generateAndPersist generates a new Ed25519 keypair, persists it, and
// returns the signer. Must be called with lw.mu held.
func (lw *LocalWALKeystore) generateAndPersist(dir string) (*Ed25519Signer, error) {
	keyID := uuid.New().String()

	_, priv, err := GenerateEd25519KeyPair()
	if err != nil {
		return nil, err
	}

	pemPath := filepath.Join(dir, keyID+".pem")
	if err := writePEM(pemPath, keyID, priv); err != nil {
		return nil, err
	}

	signer, err := NewEd25519Signer(keyID, priv)
	if err != nil {
		return nil, err
	}

	lw.signer = signer
	lw.keyID = keyID

	slog.Info("novaseal: generated new local dev key",
		"key_id", keyID,
		"pem_path", pemPath)
	return signer, nil
}

// writePEM encodes priv as a PKCS#8 PEM block with the key ID in the header
// and writes it to path with mode 0400.
func writePEM(path, keyID string, priv ed25519.PrivateKey) error {
	pkcs8, err := x509.MarshalPKCS8PrivateKey(priv)
	if err != nil {
		return fmt.Errorf("novaseal: marshal PKCS8: %w", err)
	}

	block := &pem.Block{
		Type: "PRIVATE KEY",
		Headers: map[string]string{
			"Nova-Key-ID": keyID,
		},
		Bytes: pkcs8,
	}

	// Write to a temp file, fsync it, link it into place (link fails if the name exists, like
	// O_EXCL did) and fsync the directory. A crash used to leave an empty .pem that wedged
	// every later start (review go-collector-ts-packages-16); now the name only ever appears
	// with its full content.
	tmp, err := os.CreateTemp(filepath.Dir(path), ".dev-key-*.tmp")
	if err != nil {
		return fmt.Errorf("novaseal: create PEM temp file in %q: %w", filepath.Dir(path), err)
	}
	tmpPath := tmp.Name()
	defer func() { _ = os.Remove(tmpPath) }()
	if err := tmp.Chmod(0400); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("novaseal: chmod PEM temp file: %w", err)
	}
	if err := pem.Encode(tmp, block); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("novaseal: write PEM: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("novaseal: fsync PEM: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("novaseal: close PEM: %w", err)
	}
	if err := os.Link(tmpPath, path); err != nil {
		return fmt.Errorf("novaseal: create PEM file %q: %w", path, err)
	}
	return syncDir(filepath.Dir(path))
}

// syncDir fsyncs a directory so the new key file's entry survives power loss. A filesystem
// without directory fsync is not an error.
func syncDir(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return fmt.Errorf("novaseal: open dir %q for fsync: %w", dir, err)
	}
	defer d.Close()
	if err := d.Sync(); err != nil && !errors.Is(err, syscall.EINVAL) && !errors.Is(err, syscall.ENOTSUP) {
		return fmt.Errorf("novaseal: fsync dir %q: %w", dir, err)
	}
	return nil
}

// loadPEMSigner reads a PEM file written by [writePEM] and reconstructs the
// Ed25519Signer. The key ID is taken from the PEM header "Nova-Key-ID".
func loadPEMSigner(path string) (*Ed25519Signer, string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, "", fmt.Errorf("novaseal: read PEM %q: %w", path, err)
	}

	block, _ := pem.Decode(data)
	if block == nil {
		return nil, "", fmt.Errorf("novaseal: no PEM block in %q", path)
	}

	keyID, ok := block.Headers["Nova-Key-ID"]
	if !ok || keyID == "" {
		// Fall back to filename (without extension) as key ID for older files.
		keyID = filepath.Base(path[:len(path)-len(filepath.Ext(path))])
	}

	privIface, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, "", fmt.Errorf("novaseal: parse PKCS8 from %q: %w", path, err)
	}

	priv, ok := privIface.(ed25519.PrivateKey)
	if !ok {
		return nil, "", fmt.Errorf("novaseal: PEM in %q contains a %T, want ed25519.PrivateKey", path, privIface)
	}

	signer, err := NewEd25519Signer(keyID, priv)
	if err != nil {
		return nil, "", err
	}

	slog.Info("novaseal: loaded existing local dev key",
		"key_id", keyID,
		"pem_path", path)
	return signer, keyID, nil
}
