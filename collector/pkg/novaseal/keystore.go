package novaseal

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"sync"
	"time"
)

// kmsKeyResponse is the JSON shape returned by the KMS active-key endpoint.
type kmsKeyResponse struct {
	KeyID      string `json:"key_id"`
	PrivateKey []byte `json:"private_key"` // raw 64-byte Ed25519 key
}

// KeystoreConfig holds the configuration for a production [Keystore] backed
// by an mTLS KMS endpoint.
type KeystoreConfig struct {
	// Endpoint is the base URL of the NovaSeal KMS, e.g.
	// "https://kms.internal:8443". Required.
	Endpoint string

	// CacheTTL is how long a fetched key is considered fresh before the
	// Keystore re-fetches from the KMS. Defaults to 5 minutes.
	CacheTTL time.Duration

	// RotationInterval is how often the background rotation goroutine wakes
	// up to check for a new active key. Defaults to 1 hour.
	RotationInterval time.Duration

	// FailOpen controls behaviour when the KMS is unreachable. When false
	// (the default), GetActiveSigner returns an error if the cache is stale
	// and the KMS cannot be reached. When true, the most recent cached key
	// is returned instead.
	FailOpen bool
}

// withDefaults returns a copy of cfg with zero-value durations replaced by
// the documented defaults.
func (cfg KeystoreConfig) withDefaults() KeystoreConfig {
	if cfg.CacheTTL == 0 {
		cfg.CacheTTL = 5 * time.Minute
	}
	if cfg.RotationInterval == 0 {
		cfg.RotationInterval = time.Hour
	}
	return cfg
}

// Keystore fetches and caches signing keys from a remote KMS over mTLS.
// It maintains the current active key and the previous key (for a 30-second
// overlap window after rotation). Keystore is safe for concurrent use.
type Keystore struct {
	cfg    KeystoreConfig
	client *http.Client

	mu        sync.RWMutex
	active    *Ed25519Signer
	previous  *Ed25519Signer
	fetchedAt time.Time
	rotatedAt time.Time

	stopCh chan struct{}
	wg     sync.WaitGroup
}

// NewKeystore creates a Keystore that authenticates to the KMS using the
// supplied TLS client certificate. It starts a background goroutine that
// rotates the key on [KeystoreConfig.RotationInterval].
//
// Returns an error if the initial key fetch from the KMS fails.
func NewKeystore(cfg KeystoreConfig, tlsCert tls.Certificate) (*Keystore, error) {
	cfg = cfg.withDefaults()
	if cfg.Endpoint == "" {
		return nil, fmt.Errorf("novaseal: KeystoreConfig.Endpoint must not be empty")
	}

	transport := &http.Transport{
		TLSClientConfig: &tls.Config{
			Certificates: []tls.Certificate{tlsCert},
			MinVersion:   tls.VersionTLS13,
		},
	}

	ks := &Keystore{
		cfg:    cfg,
		client: &http.Client{Transport: transport, Timeout: 10 * time.Second},
		stopCh: make(chan struct{}),
	}

	// Perform an initial fetch so the keystore is usable immediately.
	initCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if err := ks.fetchActive(initCtx); err != nil {
		return nil, fmt.Errorf("novaseal: initial key fetch failed: %w", err)
	}

	ks.wg.Add(1)
	go ks.rotationLoop()

	return ks, nil
}

// GetActiveSigner returns a [Signer] for the current active key.
//
// If the cached key is fresher than [KeystoreConfig.CacheTTL], it is returned
// without a network call. When the cache is stale and the KMS is unreachable:
//   - FailOpen == false (default): returns an error.
//   - FailOpen == true: returns the most recently cached key, if any.
func (ks *Keystore) GetActiveSigner(ctx context.Context) (Signer, error) {
	ks.mu.RLock()
	active := ks.active
	fetchedAt := ks.fetchedAt
	ks.mu.RUnlock()

	if active != nil && time.Since(fetchedAt) < ks.cfg.CacheTTL {
		return active, nil
	}

	// Cache is stale — attempt a fresh fetch.
	if err := ks.fetchActive(ctx); err != nil {
		ks.mu.RLock()
		cached := ks.active
		ks.mu.RUnlock()

		if ks.cfg.FailOpen && cached != nil {
			slog.Warn("novaseal: KMS unreachable, using cached key (FailOpen=true)",
				"error", err)
			return cached, nil
		}
		return nil, fmt.Errorf("novaseal: failed to fetch active key: %w", err)
	}

	ks.mu.RLock()
	defer ks.mu.RUnlock()
	return ks.active, nil
}

// RotateKey fetches a new active key from the KMS immediately, promoting the
// current active key to previous for the 30-second overlap window.
func (ks *Keystore) RotateKey(ctx context.Context) error {
	return ks.rotate(ctx)
}

// Close stops the background rotation goroutine and waits for it to exit.
func (ks *Keystore) Close() {
	close(ks.stopCh)
	ks.wg.Wait()
}

// PreviousSigner returns the signer for the key that was active before the
// most recent rotation, or nil if no rotation has occurred or the 30-second
// overlap window has expired.
func (ks *Keystore) PreviousSigner() Signer {
	const overlapWindow = 30 * time.Second

	ks.mu.RLock()
	defer ks.mu.RUnlock()

	if ks.previous == nil {
		return nil
	}
	if time.Since(ks.rotatedAt) > overlapWindow {
		return nil
	}
	return ks.previous
}

// fetchActive retrieves the current active key from the KMS and updates the
// cache. Caller must not hold ks.mu.
func (ks *Keystore) fetchActive(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, ks.cfg.Endpoint+"/v1/keys/active", nil)
	if err != nil {
		return err
	}
	req.Header.Set("Accept", "application/json")

	resp, err := ks.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
		return fmt.Errorf("KMS returned HTTP %d: %s", resp.StatusCode, body)
	}

	var kmsResp kmsKeyResponse
	if err := json.NewDecoder(resp.Body).Decode(&kmsResp); err != nil {
		return fmt.Errorf("decode KMS response: %w", err)
	}

	signer, err := NewEd25519Signer(kmsResp.KeyID, ed25519.PrivateKey(kmsResp.PrivateKey))
	if err != nil {
		return fmt.Errorf("construct signer from KMS key: %w", err)
	}

	ks.mu.Lock()
	ks.active = signer
	ks.fetchedAt = time.Now()
	ks.mu.Unlock()

	slog.Info("novaseal: active signing key refreshed", "key_id", kmsResp.KeyID)
	return nil
}

// rotate fetches a new key and moves the current active to previous with a
// 30-second expiry.
func (ks *Keystore) rotate(ctx context.Context) error {
	ks.mu.RLock()
	oldActive := ks.active
	ks.mu.RUnlock()

	if err := ks.fetchActive(ctx); err != nil {
		return err
	}

	ks.mu.Lock()
	// Preserve the old key as previous for the 30-second overlap window.
	if oldActive != nil {
		ks.previous = oldActive
	}
	ks.rotatedAt = time.Now()
	ks.mu.Unlock()

	slog.Info("novaseal: key rotation complete", "rotated_at", ks.rotatedAt)
	return nil
}

// rotationLoop is the background goroutine that rotates the key periodically.
func (ks *Keystore) rotationLoop() {
	defer ks.wg.Done()
	ticker := time.NewTicker(ks.cfg.RotationInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ks.stopCh:
			return
		case <-ticker.C:
			rotCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
			if err := ks.rotate(rotCtx); err != nil {
				slog.Error("novaseal: background key rotation failed", "error", err)
			}
			cancel()
		}
	}
}

// GenerateEd25519KeyPair generates a new Ed25519 keypair using a
// cryptographically secure random source. It is exported for use by
// [LocalWALKeystore] and testing helpers.
func GenerateEd25519KeyPair() (ed25519.PublicKey, ed25519.PrivateKey, error) {
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return nil, nil, fmt.Errorf("novaseal: ed25519 key generation: %w", err)
	}
	return pub, priv, nil
}
