// Package forwarder is the resident spool-drain that forwards EventEnvelope v1
// records from the local spool to the collector tier (ADR-0092 slice C).
//
// The forwarder is keyless: it publishes UNSIGNED envelopes over NATS to the
// hub, where the novaseal_batch_signer processor signs them (OQ-C-1 hub-sign
// default; compute nodes hold no NovaSeal keystore, preserving the HPC air-gap).
//
// Exactly-once across the spool checkpoint comes from the Drainer: a batch is
// committed only after every event in it has been published. If publishing
// fails mid-batch the batch is NOT committed, but the spool's in-memory read
// cursor has already advanced while the persisted checkpoint has not — so the
// uncommitted events are re-read only after a RESTART (a freshly opened spool
// resumes from the first un-acked event). The resident drain loop therefore
// treats a DrainBatch error as fatal and exits, so a new process re-reads the
// in-flight batch (at-least-once on the wire; downstream dedups by event_id).
package forwarder

import (
	"context"
	"encoding/json"
	"fmt"
	"time"
)

// Publisher sends a single event payload to the collector tier under subject.
type Publisher interface {
	Publish(subject string, eventJSON []byte) error
}

// Drainer reads up to maxEvents from the spool, calls fn for each, and commits
// the batch only if every fn call succeeds. *hpc.SpoolStore satisfies this.
type Drainer interface {
	Drain(maxEvents int, fn func([]byte) error) error
}

// Forwarder drains the spool and publishes each event to <prefix>.<run_id>.
type Forwarder struct {
	drainer Drainer
	pub     Publisher
	prefix  string
}

// New returns a Forwarder. prefix is the NATS subject prefix (e.g.
// "nova.evidence"); the per-event subject is "<prefix>.<run_id>".
func New(drainer Drainer, pub Publisher, prefix string) *Forwarder {
	return &Forwarder{drainer: drainer, pub: pub, prefix: prefix}
}

// DrainBatch drains up to maxEvents and publishes each one. The underlying
// Drainer commits the batch iff every publish succeeds.
func (f *Forwarder) DrainBatch(maxEvents int) error {
	return f.drainer.Drain(maxEvents, func(ev []byte) error {
		if err := f.pub.Publish(f.subjectFor(ev), ev); err != nil {
			return fmt.Errorf("forwarder: publish: %w", err)
		}
		return nil
	})
}

// Run is the resident drain loop: it drains the spool every interval until ctx
// is cancelled (graceful shutdown → nil). A DrainBatch error is fatal and is
// returned immediately: the spool's in-memory cursor has advanced past the
// uncommitted batch, so only a fresh process (restart) re-reads it. The caller
// (the binary) is expected to exit non-zero and be restarted by its supervisor.
func (f *Forwarder) Run(ctx context.Context, interval time.Duration, maxEvents int) error {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		if err := f.DrainBatch(maxEvents); err != nil {
			return err
		}
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
		}
	}
}

// subjectFor derives the publish subject from the envelope's run_id. Events
// without a parseable run_id go to "<prefix>.unknown" rather than being dropped.
func (f *Forwarder) subjectFor(eventJSON []byte) string {
	var env struct {
		RunID string `json:"run_id"`
	}
	if err := json.Unmarshal(eventJSON, &env); err != nil || env.RunID == "" {
		return f.prefix + ".unknown"
	}
	return f.prefix + "." + env.RunID
}
