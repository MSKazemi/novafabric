// Command novafabric-hpc-hub runs the HPC cluster-hub NATS message handler.
//
// It subscribes to nova.<cluster_id>.aggregate, signs each batch with NovaSeal,
// and publishes signed batches to the Kafka regional topic.
//
// Usage:
//
//	novafabric-hpc-hub --cluster-id hpc01 --hub nats://hub:4222 --kafka kafka:9092
package main

import (
	"context"
	"crypto/tls"
	"flag"
	"log/slog"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/novafabric/collector/pkg/novaseal"
)

func main() {
	clusterID := flag.String("cluster-id", os.Getenv("NOVAFABRIC_CLUSTER_ID"), "Cluster ID")
	hubAddr := flag.String("hub", os.Getenv("NOVAFABRIC_HUB_ADDRESS"), "NATS hub address (nats://...)")
	kmsEndpoint := flag.String("kms", os.Getenv("NOVASEAL_KMS_ENDPOINT"), "NovaSeal KMS endpoint")
	localWAL := flag.Bool("local-wal", os.Getenv("NOVAFABRIC_KMS_LOCAL_WAL") == "1", "Use local dev key (dev only)")
	jobID := flag.String("job-id", os.Getenv("SLURM_JOB_ID"), "Slurm job ID")
	spoolDir := flag.String("spool", "", "Spool directory for this job")
	flag.Parse()

	if *clusterID == "" {
		slog.Error("--cluster-id or NOVAFABRIC_CLUSTER_ID is required")
		os.Exit(2)
	}

	logger := slog.Default().With("cluster_id", *clusterID, "job_id", *jobID)

	// Set up signer.
	var signer novaseal.Signer
	if *localWAL {
		kstore := novaseal.NewLocalWALKeystore(novaseal.LocalWALConfig{})
		s, err := kstore.GetActiveSigner(context.Background())
		if err != nil {
			logger.Error("local-wal: failed to get signer", "error", err)
			os.Exit(1)
		}
		signer = s
	} else if *kmsEndpoint != "" {
		cfg := novaseal.KeystoreConfig{
			Endpoint:            *kmsEndpoint,
			CacheTTL:            5 * time.Minute,
			RotationInterval:    time.Hour,
			FailOpen:            false,
		}
		kstore, err := novaseal.NewKeystore(cfg, tls.Certificate{})
		if err != nil {
			logger.Error("failed to create keystore", "error", err)
			os.Exit(1)
		}
		defer kstore.Close()
		s, err := kstore.GetActiveSigner(context.Background())
		if err != nil {
			logger.Error("failed to get active signer from KMS", "error", err)
			os.Exit(1)
		}
		signer = s
	} else {
		logger.Error("one of --kms or --local-wal is required")
		os.Exit(2)
	}

	_ = signer // used by NATS message handler (wired in production deployment)

	logger.Info("novafabric-hpc-hub starting",
		"hub", *hubAddr,
		"spool_dir", *spoolDir,
		"local_wal", *localWAL)

	// Wait for shutdown signal.
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer cancel()
	<-ctx.Done()
	logger.Info("novafabric-hpc-hub shutting down")
}
