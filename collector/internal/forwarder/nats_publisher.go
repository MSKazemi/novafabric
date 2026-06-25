package forwarder

import (
	"context"
	"fmt"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

// NATSPublisher publishes events to a NATS JetStream stream. It is the C0.3b
// adapter behind the Publisher interface; the resident drain (C0.3c) constructs
// one pointed at the local leaf node, and leafnodes replication carries the
// events to the hub where novaseal_batch_signer signs them (OQ-C-1 hub-sign).
//
// Edge-keyless: this publisher holds no NovaSeal keystore.
type NATSPublisher struct {
	nc *nats.Conn
	js jetstream.JetStream
}

// NewNATSPublisher connects to url and ensures a JetStream stream named
// streamName covering "<subjectPrefix>.>" exists (idempotent). The drain
// publishes per-run subjects "<subjectPrefix>.<run_id>" under that stream.
func NewNATSPublisher(url, streamName, subjectPrefix string) (*NATSPublisher, error) {
	nc, err := nats.Connect(url, nats.Name("novafabric-spool-forwarder"))
	if err != nil {
		return nil, fmt.Errorf("forwarder: nats connect %q: %w", url, err)
	}
	js, err := jetstream.New(nc)
	if err != nil {
		nc.Close()
		return nil, fmt.Errorf("forwarder: jetstream init: %w", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if _, err := js.CreateOrUpdateStream(ctx, jetstream.StreamConfig{
		Name:     streamName,
		Subjects: []string{subjectPrefix + ".>"},
	}); err != nil {
		nc.Close()
		return nil, fmt.Errorf("forwarder: ensure stream %q: %w", streamName, err)
	}
	return &NATSPublisher{nc: nc, js: js}, nil
}

// Publish sends one event to subject via JetStream, blocking until the server
// acknowledges persistence (so a publish "success" means the event is durable).
func (p *NATSPublisher) Publish(subject string, eventJSON []byte) error {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if _, err := p.js.Publish(ctx, subject, eventJSON); err != nil {
		return fmt.Errorf("forwarder: jetstream publish %q: %w", subject, err)
	}
	return nil
}

// Close drains and closes the NATS connection.
func (p *NATSPublisher) Close() error {
	p.nc.Close()
	return nil
}
