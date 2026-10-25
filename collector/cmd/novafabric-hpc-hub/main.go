// Command novafabric-hpc-hub is the planned HPC cluster-hub NATS message handler.
//
// STATUS: planned, not implemented. The intended design subscribes to
// nova.<cluster_id>.aggregate, signs each batch with NovaSeal, and publishes the signed
// batches to a Kafka regional topic. No part of that is written: this binary parses its flags
// and then exits non-zero. It used to obtain a signing key (generating a dev private key on
// disk with --local-wal, or contacting the KMS with an EMPTY mTLS client certificate with
// --kms), log "starting", and wait for SIGTERM while processing nothing — and the docs
// described it as a working signer (review go-collector-ts-packages-09).
//
// Usage (flags are accepted so existing invocations get this message, not a flag error):
//
//	novafabric-hpc-hub --cluster-id hpc01 --hub nats://hub:4222 --kafka kafka:9092
package main

import (
	"flag"
	"fmt"
	"io"
	"log/slog"
	"os"
)

// Exit codes.
const (
	exitNotImplemented = 1
	exitUsage          = 2
)

const notImplemented = "novafabric-hpc-hub is not implemented (planned): it does not subscribe to " +
	"the hub, sign batches or publish to Kafka, and was exiting only after waiting for a signal. " +
	"Nothing was started and no signing key was read or created"

func main() {
	os.Exit(run(os.Args[1:], os.Getenv, os.Stderr))
}

// run parses args and reports that the hub is not implemented. It never reads or creates a
// key and never contacts a KMS.
func run(args []string, getenv func(string) string, stderr io.Writer) int {
	fs := flag.NewFlagSet("novafabric-hpc-hub", flag.ContinueOnError)
	fs.SetOutput(stderr)
	clusterID := fs.String("cluster-id", getenv("NOVAFABRIC_CLUSTER_ID"), "Cluster ID")
	_ = fs.String("hub", getenv("NOVAFABRIC_HUB_ADDRESS"), "NATS hub address (nats://...)")
	_ = fs.String("kms", getenv("NOVASEAL_KMS_ENDPOINT"), "NovaSeal KMS endpoint (planned)")
	_ = fs.Bool("local-wal", getenv("NOVAFABRIC_KMS_LOCAL_WAL") == "1", "Use local dev key (planned)")
	jobID := fs.String("job-id", getenv("SLURM_JOB_ID"), "Slurm job ID")
	_ = fs.String("spool", "", "Spool directory for this job")
	if err := fs.Parse(args); err != nil {
		return exitUsage
	}

	if *clusterID == "" {
		fmt.Fprintln(stderr, "--cluster-id or NOVAFABRIC_CLUSTER_ID is required")
		return exitUsage
	}

	slog.New(slog.NewTextHandler(stderr, nil)).Error(notImplemented,
		"cluster_id", *clusterID, "job_id", *jobID)
	return exitNotImplemented
}
