// Command novafabric-spool-forwarder is the resident spool-drain (ADR-0092
// slice C, C0.3c). It drains the local event spool and publishes each
// EventEnvelope v1 record to a NATS JetStream stream. It holds no NovaSeal
// keystore (OQ-C-1 hub-sign): the envelopes it publishes are unsigned, and the
// hub that would sign them (novafabric-hpc-hub) is not implemented yet.
// Maturity: experimental.
//
// A drain error is fatal: the process exits non-zero so its supervisor (systemd
// / Slurm Prolog / K8s DaemonSet) restarts it, which re-reads the in-flight
// batch from the persisted spool checkpoint (no event loss).
//
// Usage:
//
//	novafabric-spool-forwarder --spool /var/spool/novafabric --nats nats://127.0.0.1:4222
//
// Run with -h for every flag, environment variable and exit code, or see
// docs/collector-reference.md.
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/MSKazemi/novafabric/collector/internal/forwarder"
	"github.com/MSKazemi/novafabric/collector/internal/hpc"
)

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// usageText is printed by -h/--help and on a flag error.
const usageText = `novafabric-spool-forwarder: drain the local NovaFabric event spool into NATS JetStream.

Maturity: experimental. It publishes UNSIGNED EventEnvelope v1 records and holds no
signing key; signing at the hub is not implemented yet (novafabric-hpc-hub is a stub).

Usage:
  novafabric-spool-forwarder --spool DIR [--nats URL] [--stream NAME] [--subject-prefix P]
                             [--interval D] [--max-events N]

Each pass reads up to --max-events committed events, publishes each one to
<subject-prefix>.<run_id> and waits for the JetStream ack, then advances the spool
checkpoint (DIR/checkpoint.json) and deletes the segment files it has fully forwarded.
It runs until SIGINT or SIGTERM.

Flags:
`

// usageFooter follows the flag list in the help output.
const usageFooter = `
Environment (a flag given on the command line wins):
  NOVAFABRIC_SPOOL_DIR      default for --spool
  NOVA_NATS_URL             default for --nats
  NOVAFABRIC_SPOOL_STREAM   default for --stream
  NOVAFABRIC_SPOOL_SUBJECT  default for --subject-prefix
  NOVA_DLQ_DIR              creates that dead-letter directory at start; nothing is written
                            to it here, since only a spool write routes an event there

Exit codes:
  0  stopped cleanly on SIGINT/SIGTERM (also for -h)
  1  the spool could not be opened, NATS/JetStream was unreachable or the stream could
     not be created, or a spool read or publish failed. The in-flight batch is not
     acknowledged; a restart re-reads it, so run this under a supervisor that
     restarts it
  2  usage error: no spool directory, or an unknown flag

See docs/collector-reference.md.
`

func usage() {
	out := flag.CommandLine.Output()
	_, _ = fmt.Fprint(out, usageText)
	flag.PrintDefaults()
	_, _ = fmt.Fprint(out, usageFooter)
}

func main() {
	spoolDir := flag.String("spool", os.Getenv("NOVAFABRIC_SPOOL_DIR"),
		"Spool directory to drain (required; env NOVAFABRIC_SPOOL_DIR). Created if missing")
	natsURL := flag.String("nats", envOr("NOVA_NATS_URL", "nats://127.0.0.1:4222"),
		"NATS server URL (env NOVA_NATS_URL)")
	stream := flag.String("stream", envOr("NOVAFABRIC_SPOOL_STREAM", "NOVA_EVIDENCE"),
		"JetStream stream name, created or updated at start to cover <subject-prefix>.> (env NOVAFABRIC_SPOOL_STREAM)")
	prefix := flag.String("subject-prefix", envOr("NOVAFABRIC_SPOOL_SUBJECT", "nova.evidence"),
		"JetStream subject prefix; each event goes to <prefix>.<run_id>, or <prefix>.unknown without a run_id (env NOVAFABRIC_SPOOL_SUBJECT)")
	interval := flag.Duration("interval", 500*time.Millisecond, "Pause between drain passes (Go duration, e.g. 500ms, 2s)")
	maxEvents := flag.Int("max-events", 1000, "Max events read and published per drain pass")
	maxBytes := flag.Int64("max-bytes", 1<<30,
		"Spool size bound in bytes. No effect in this process: the bound is enforced when an event is written, and the forwarder only reads")
	flag.Usage = usage
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
