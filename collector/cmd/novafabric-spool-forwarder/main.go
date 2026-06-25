// Command novafabric-spool-forwarder is the resident spool-drain (ADR-0092
// slice C, C0.3c). It drains the local event spool and publishes each
// EventEnvelope v1 record to a NATS JetStream stream, from where leafnodes
// replication carries it to the hub for signing (OQ-C-1 hub-sign; this process
// holds no NovaSeal keystore).
//
// A drain error is fatal: the process exits non-zero so its supervisor (systemd
// / Slurm Prolog / K8s DaemonSet) restarts it, which re-reads the in-flight
// batch from the persisted spool checkpoint (no event loss).
//
// Usage:
//
//	novafabric-spool-forwarder --spool /var/spool/novafabric --nats nats://127.0.0.1:4222
package main

import (
	"context"
	"flag"
	"log/slog"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/novafabric/collector/internal/forwarder"
	"github.com/novafabric/collector/internal/hpc"
)

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func main() {
	spoolDir := flag.String("spool", os.Getenv("NOVAFABRIC_SPOOL_DIR"), "Spool directory to drain")
	natsURL := flag.String("nats", envOr("NOVA_NATS_URL", "nats://127.0.0.1:4222"), "NATS URL")
	stream := flag.String("stream", envOr("NOVAFABRIC_SPOOL_STREAM", "NOVA_EVIDENCE"), "JetStream stream name")
	prefix := flag.String("subject-prefix", envOr("NOVAFABRIC_SPOOL_SUBJECT", "nova.evidence"), "JetStream subject prefix; per-run subject is <prefix>.<run_id>")
	interval := flag.Duration("interval", 500*time.Millisecond, "Drain poll interval")
	maxEvents := flag.Int("max-events", 1000, "Max events per drain batch")
	maxBytes := flag.Int64("max-bytes", 1<<30, "Spool max bytes before backpressure eviction")
	flag.Parse()

	if *spoolDir == "" {
		slog.Error("spool directory required (--spool or NOVAFABRIC_SPOOL_DIR)")
		os.Exit(2)
	}

	store, err := hpc.NewSpoolStore(*spoolDir, *maxBytes, nil)
	if err != nil {
		slog.Error("open spool", "error", err)
		os.Exit(1)
	}
	defer func() { _ = store.Close() }()

	pub, err := forwarder.NewNATSPublisher(*natsURL, *stream, *prefix)
	if err != nil {
		slog.Error("connect NATS JetStream", "error", err)
		os.Exit(1)
	}
	defer func() { _ = pub.Close() }()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	slog.Info("novafabric-spool-forwarder: draining",
		"spool", *spoolDir, "nats", *natsURL, "stream", *stream, "subject_prefix", *prefix)
	if err := forwarder.New(store, pub, *prefix).Run(ctx, *interval, *maxEvents); err != nil {
		slog.Error("forwarder exited with error (restart to re-read the in-flight batch)", "error", err)
		os.Exit(1)
	}
	slog.Info("novafabric-spool-forwarder: stopped cleanly")
}
