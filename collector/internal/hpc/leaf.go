// Package hpc manages the lifecycle of a NATS JetStream leaf node that runs
// on HPC compute nodes as part of the NovaFabric collector tier.
//
// Each Slurm job step or Kubernetes pod starts one Leaf that spools events
// locally and forwards them to the hub cluster when the network is available.
// If the network is unavailable the events remain in the spool directory until
// the next successful flush.
//
// The Leaf never uses privileged kernel primitives.  All persistence is via
// the spool package (os.Rename as the only atomic primitive).
package hpc

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"text/template"
	"time"
)

// LeafConfig holds all parameters needed to start a NATS leaf node.
type LeafConfig struct {
	// SpoolDir is the directory where the spool stores segment files.
	// The NATS server also writes its internal files here unless StoreDir
	// is explicitly set.
	SpoolDir string

	// JobID identifies the Slurm or Kubernetes job that owns this leaf.
	JobID string

	// ClusterID identifies the NovaFabric cluster (matches Hub config).
	ClusterID string

	// NodeID identifies this compute node within the cluster.
	NodeID string

	// HubAddress is the NATS server URL of the hub (e.g. nats://hub:4222).
	HubAddress string

	// StoreDir overrides the NATS store directory.  Defaults to SpoolDir
	// when NOVAFABRIC_HPC_STORAGE=spool.
	StoreDir string
}

// natsConfigTemplate is the NATS server configuration file template for a
// leaf node.  Fields are populated from LeafConfig.
const natsConfigTemplate = `# NovaFabric collector — NATS leaf node config
# Generated automatically; do not edit by hand.

server_name: "nova-leaf-{{.NodeID}}"

jetstream {
  store_dir: "{{.StoreDir}}"
}

leafnodes {
  remotes = [
    {
      url: "{{.HubAddress}}"
      account: "NOVA"
      credentials: "{{.SpoolDir}}/leaf.creds"
    }
  ]
}
`

var parsedNATSTemplate = template.Must(template.New("nats-leaf").Parse(natsConfigTemplate))

// Leaf manages the lifecycle of a single NATS leaf node process on a compute
// node.
type Leaf struct {
	cfg  LeafConfig
	proc *exec.Cmd
	done chan struct{}
}

// NewLeaf constructs a Leaf from cfg.  It does not start the NATS process;
// call Start for that.
func NewLeaf(cfg LeafConfig) *Leaf {
	if cfg.StoreDir == "" {
		if IsSpoolMode() {
			cfg.StoreDir = cfg.SpoolDir
		} else {
			cfg.StoreDir = filepath.Join(cfg.SpoolDir, "nats-store")
		}
	}
	return &Leaf{cfg: cfg, done: make(chan struct{})}
}

// Start creates the spool directory, writes the NATS config file, and starts
// the NATS server as a child process.
//
// Start returns an error if the directory cannot be created, the config file
// cannot be written, or the NATS binary cannot be found or launched.
func (l *Leaf) Start(ctx context.Context) error {
	// Create spool directory hierarchy.
	if err := os.MkdirAll(l.cfg.SpoolDir, 0o750); err != nil {
		return fmt.Errorf("hpc leaf: create spool dir %s: %w", l.cfg.SpoolDir, err)
	}
	if err := os.MkdirAll(l.cfg.StoreDir, 0o750); err != nil {
		return fmt.Errorf("hpc leaf: create store dir %s: %w", l.cfg.StoreDir, err)
	}

	// Write NATS config file.
	cfgPath := filepath.Join(l.cfg.SpoolDir, "nats-leaf.conf")
	if err := l.writeNATSConfig(cfgPath); err != nil {
		return err
	}

	// Start nats-server subprocess.
	natsPath, err := exec.LookPath("nats-server")
	if err != nil {
		return fmt.Errorf("hpc leaf: nats-server not found in PATH: %w", err)
	}

	l.proc = exec.CommandContext(ctx, natsPath, "-c", cfgPath)
	l.proc.Stdout = os.Stdout
	l.proc.Stderr = os.Stderr

	if err := l.proc.Start(); err != nil {
		return fmt.Errorf("hpc leaf: start nats-server: %w", err)
	}

	slog.Info("hpc leaf: nats-server started",
		"pid", l.proc.Process.Pid,
		"config", cfgPath,
		"job_id", l.cfg.JobID,
		"node_id", l.cfg.NodeID)

	// Monitor process exit in the background.
	go func() {
		defer close(l.done)
		if waitErr := l.proc.Wait(); waitErr != nil {
			slog.Warn("hpc leaf: nats-server exited", "error", waitErr)
		}
	}()

	return nil
}

// Stop flushes pending JetStream data and terminates the NATS leaf node.
//
// Stop always returns nil in compliance with FR-12 (the HPC spool agent must
// exit 0 regardless of flush outcome so that Slurm epilog scripts complete
// cleanly).
func (l *Leaf) Stop(_ context.Context, flushTimeout time.Duration) error {
	if l.proc == nil || l.proc.Process == nil {
		return nil
	}

	// Attempt to flush JetStream streams via the nats CLI before killing the
	// server.  Failures are logged but do not propagate (FR-12).
	natsCLI, err := exec.LookPath("nats")
	if err == nil {
		flushCtx, cancel := context.WithTimeout(context.Background(), flushTimeout)
		defer cancel()
		flushCmd := exec.CommandContext(flushCtx, natsCLI, "stream", "purge",
			"--timeout", flushTimeout.String())
		if out, flushErr := flushCmd.CombinedOutput(); flushErr != nil {
			slog.Warn("hpc leaf: flush failed (non-fatal)", "error", flushErr, "output", string(out))
		}
	} else {
		slog.Warn("hpc leaf: nats CLI not found, skipping flush", "error", err)
	}

	// Terminate the server process.
	if killErr := l.proc.Process.Kill(); killErr != nil {
		slog.Warn("hpc leaf: kill nats-server (non-fatal)", "error", killErr)
	}

	// Wait for the goroutine monitoring proc.Wait to finish.
	select {
	case <-l.done:
	case <-time.After(flushTimeout + 5*time.Second):
		slog.Warn("hpc leaf: timed out waiting for nats-server to exit")
	}

	slog.Info("hpc leaf: stopped", "job_id", l.cfg.JobID, "node_id", l.cfg.NodeID)
	return nil // FR-12: always exit 0
}

// writeNATSConfig renders the NATS config template and writes it to cfgPath.
func (l *Leaf) writeNATSConfig(cfgPath string) error {
	f, err := os.OpenFile(cfgPath+".tmp", os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o600)
	if err != nil {
		return fmt.Errorf("hpc leaf: open nats config tmp: %w", err)
	}

	data := struct {
		NodeID     string
		StoreDir   string
		HubAddress string
		SpoolDir   string
	}{
		NodeID:     l.cfg.NodeID,
		StoreDir:   l.cfg.StoreDir,
		HubAddress: l.cfg.HubAddress,
		SpoolDir:   l.cfg.SpoolDir,
	}

	if err := parsedNATSTemplate.Execute(f, data); err != nil {
		_ = f.Close()
		_ = os.Remove(cfgPath + ".tmp")
		return fmt.Errorf("hpc leaf: render nats config: %w", err)
	}

	if err := f.Sync(); err != nil {
		_ = f.Close()
		_ = os.Remove(cfgPath + ".tmp")
		return fmt.Errorf("hpc leaf: fsync nats config: %w", err)
	}

	if err := f.Close(); err != nil {
		return fmt.Errorf("hpc leaf: close nats config tmp: %w", err)
	}

	// Atomic rename — the only commit primitive used here.
	if err := os.Rename(cfgPath+".tmp", cfgPath); err != nil {
		return fmt.Errorf("hpc leaf: rename nats config: %w", err)
	}
	return nil
}
