// Package envelope defines the NovaFabric Event Envelope v1 — the canonical
// wire format for all evidence events in the collector tier.
//
// The schema is defined at schemas/event-envelope-v1/envelope-v1.json.
// Every event that flows through the collector is wrapped in an EventEnvelope
// before being serialized to OTLP or CloudEvents.
package envelope

import (
	"math/rand"
	"time"

	"github.com/oklog/ulid/v2"
)

// EnvelopeVersion is the fixed string value for the envelope_version field.
const EnvelopeVersion = "1"

// SchemaVersion is the fixed string value for the schema_version field.
const SchemaVersion = "event-envelope/1"

// EventType enumerates the allowed values for the event_type field.
// See the JSON Schema enum at schemas/event-envelope-v1/envelope-v1.json.
type EventType string

const (
	EventTypeRunStart       EventType = "run.start"
	EventTypeRunEnd         EventType = "run.end"
	EventTypeModelCall      EventType = "model_call"
	EventTypeToolCall       EventType = "tool_call"
	EventTypeSpan           EventType = "span"
	EventTypeCapsuleFinalize EventType = "capsule.finalize"
)

// EventEnvelope is the top-level container for all NovaFabric evidence events.
// It maps 1:1 with the JSON Schema at schemas/event-envelope-v1/envelope-v1.json.
//
// Fields that are nullable in the schema use pointer types. Zero-value strings
// are treated as absent for optional fields that are not nullable (e.g.
// BatchSignature).
type EventEnvelope struct {
	// EnvelopeVersion is always "1". String type allows forward parsers to
	// detect unknown future versions.
	EnvelopeVersion string `json:"envelope_version"`

	// SchemaVersion is always "event-envelope/1".
	SchemaVersion string `json:"schema_version"`

	// EventID is a globally unique ULID generated at the time the event is
	// first assembled. Used as the content-addressable storage key.
	EventID string `json:"event_id"`

	// GlobalRunID is the logical distributed run identifier. All events in the
	// same distributed run share this value. For STANDALONE/PARENT capsules
	// this equals RunID.
	GlobalRunID string `json:"global_run_id"`

	// RunID is the per-process capsule identifier (ULID or UUID v7).
	RunID string `json:"run_id"`

	// ParentRunID is nil for STANDALONE/PARENT capsules and non-nil for
	// WORKER/COORDINATOR capsules.
	ParentRunID *string `json:"parent_run_id"`

	// EventType identifies the kind of event. Must be one of the EventType
	// constants.
	EventType string `json:"event_type"`

	// TraceID is the W3C Trace Context 16-byte trace ID encoded as 32
	// lowercase hex characters.
	TraceID string `json:"trace_id"`

	// SpanID is the W3C Trace Context 8-byte span ID encoded as 16 lowercase
	// hex characters.
	SpanID string `json:"span_id"`

	// AgentID is the stable identifier for the emitting agent or SDK component.
	AgentID string `json:"agent_id"`

	// ClusterID is nil in local/single-machine mode.
	ClusterID *string `json:"cluster_id"`

	// TenantID is nil in single-tenant/local mode.
	TenantID *string `json:"tenant_id"`

	// StartedAt is the event start time in UTC.
	StartedAt time.Time `json:"started_at"`

	// BatchSignature is the base64-encoded Ed25519 detached signature attached
	// by the NovaSeal Batch Processor. Empty string means unsigned.
	BatchSignature string `json:"nova.batch.signature,omitempty"`

	// BatchSigningKeyID is the UUID of the NovaSeal signing key. Empty string
	// means unsigned.
	BatchSigningKeyID string `json:"nova.batch.signing_key_id,omitempty"`

	// Payload is the JSON-encoded event-type-specific payload. The schema of
	// the payload is defined per EventType, not in the envelope.
	Payload []byte `json:"payload,omitempty"`

	// PayloadHash is the SHA-256 of the serialized payload bytes in
	// "sha256:<hex>" form, or empty if absent.
	PayloadHash string `json:"payload_hash,omitempty"`

	// EmitterNodeID is the hostname or node identifier of the emitting node.
	// Empty/nil when unavailable.
	EmitterNodeID string `json:"emitter_node_id,omitempty"`
}

// New constructs a minimal EventEnvelope with mandatory fields pre-filled.
// The caller is responsible for populating TraceID, SpanID, Payload,
// PayloadHash, and any optional fields before forwarding the envelope.
func New(eventType, agentID, runID, globalRunID string) *EventEnvelope {
	return &EventEnvelope{
		EnvelopeVersion: EnvelopeVersion,
		SchemaVersion:   SchemaVersion,
		EventID:         NewULID(),
		GlobalRunID:     globalRunID,
		RunID:           runID,
		EventType:       eventType,
		AgentID:         agentID,
		StartedAt:       time.Now().UTC(),
	}
}

// NewULID returns a new, monotonically increasing ULID string using a
// cryptographically seeded source. The ULID is encoded in Crockford Base32.
func NewULID() string {
	// Use a monotonic entropy source seeded from the default global rand.
	// For production, callers that need strict monotonicity should use their
	// own ulid.MonotonicEntropy with a mutex.
	ms := ulid.MustNew(ulid.Timestamp(time.Now()), rand.New(rand.NewSource(time.Now().UnixNano())))
	return ms.String()
}
