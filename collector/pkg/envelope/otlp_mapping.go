package envelope

import (
	"encoding/hex"
	"fmt"
	"time"

	otlpcommonv1 "go.opentelemetry.io/proto/otlp/common/v1"
	otlplogspb "go.opentelemetry.io/proto/otlp/logs/v1"
)

// OTLP attribute key constants for the nova.* namespace.
const (
	attrEventID          = "nova.event_id"
	attrGlobalRunID      = "nova.global_run_id"
	attrRunID            = "nova.run_id"
	attrParentRunID      = "nova.parent_run_id"
	attrEventType        = "nova.event_type"
	attrAgentID          = "nova.agent_id"
	attrClusterID        = "nova.cluster_id"
	attrTenantID         = "nova.tenant_id"
	attrEnvelopeVersion  = "nova.envelope_version"
	attrSchemaVersion    = "nova.schema_version"
	attrPayloadHash      = "nova.payload_hash"
	attrEmitterNodeID    = "nova.emitter_node_id"
	attrBatchSignature   = "nova.batch.signature"
	attrBatchSigningKeyID = "nova.batch.signing_key_id"
)

// ToOTLPLogRecord converts an EventEnvelope into an OTLP LogRecord.
//
// Mapping rules:
//   - TraceID and SpanID are set via the native OTLP LogRecord fields
//     (TraceId / SpanId as byte slices).
//   - All other envelope fields are encoded as LogRecord attributes using the
//     nova.* namespace.
//   - nova.batch.signature and nova.batch.signing_key_id are intended as
//     Resource.attributes (added by NovaSeal); they are included here as log
//     attributes for single-record transport and will be promoted by the
//     collector's batch processor.
//   - Nil pointer fields are omitted (attribute not added).
func ToOTLPLogRecord(e *EventEnvelope) *otlplogspb.LogRecord {
	lr := &otlplogspb.LogRecord{
		TimeUnixNano: uint64(e.StartedAt.UnixNano()),
		ObservedTimeUnixNano: uint64(time.Now().UTC().UnixNano()),
	}

	// Native OTLP trace context fields.
	if e.TraceID != "" {
		b, err := hex.DecodeString(e.TraceID)
		if err == nil && len(b) == 16 {
			lr.TraceId = b
		}
	}
	if e.SpanID != "" {
		b, err := hex.DecodeString(e.SpanID)
		if err == nil && len(b) == 8 {
			lr.SpanId = b
		}
	}

	// Encode payload as the log body.
	if len(e.Payload) > 0 {
		lr.Body = &otlpcommonv1.AnyValue{
			Value: &otlpcommonv1.AnyValue_BytesValue{BytesValue: e.Payload},
		}
	}

	// nova.* attributes.
	attrs := []*otlpcommonv1.KeyValue{
		strAttr(attrEnvelopeVersion, e.EnvelopeVersion),
		strAttr(attrSchemaVersion, e.SchemaVersion),
		strAttr(attrEventID, e.EventID),
		strAttr(attrGlobalRunID, e.GlobalRunID),
		strAttr(attrRunID, e.RunID),
		strAttr(attrEventType, e.EventType),
		strAttr(attrAgentID, e.AgentID),
	}

	if e.ParentRunID != nil {
		attrs = append(attrs, strAttr(attrParentRunID, *e.ParentRunID))
	}
	if e.ClusterID != nil {
		attrs = append(attrs, strAttr(attrClusterID, *e.ClusterID))
	}
	if e.TenantID != nil {
		attrs = append(attrs, strAttr(attrTenantID, *e.TenantID))
	}
	if e.PayloadHash != "" {
		attrs = append(attrs, strAttr(attrPayloadHash, e.PayloadHash))
	}
	if e.EmitterNodeID != "" {
		attrs = append(attrs, strAttr(attrEmitterNodeID, e.EmitterNodeID))
	}
	if e.BatchSignature != "" {
		attrs = append(attrs, strAttr(attrBatchSignature, e.BatchSignature))
	}
	if e.BatchSigningKeyID != "" {
		attrs = append(attrs, strAttr(attrBatchSigningKeyID, e.BatchSigningKeyID))
	}

	lr.Attributes = attrs
	return lr
}

// FromOTLPLogRecord reconstructs an EventEnvelope from an OTLP LogRecord
// produced by [ToOTLPLogRecord]. Returns an error if any required attribute
// is missing.
func FromOTLPLogRecord(lr *otlplogspb.LogRecord) (*EventEnvelope, error) {
	if lr == nil {
		return nil, fmt.Errorf("envelope: LogRecord must not be nil")
	}

	attrMap := make(map[string]string, len(lr.Attributes))
	for _, kv := range lr.Attributes {
		if sv, ok := kv.Value.GetValue().(*otlpcommonv1.AnyValue_StringValue); ok {
			attrMap[kv.Key] = sv.StringValue
		}
	}

	required := []string{
		attrEnvelopeVersion, attrSchemaVersion, attrEventID,
		attrGlobalRunID, attrRunID, attrEventType, attrAgentID,
	}
	for _, k := range required {
		if _, ok := attrMap[k]; !ok {
			return nil, fmt.Errorf("envelope: missing required attribute %q", k)
		}
	}

	e := &EventEnvelope{
		EnvelopeVersion:   attrMap[attrEnvelopeVersion],
		SchemaVersion:     attrMap[attrSchemaVersion],
		EventID:           attrMap[attrEventID],
		GlobalRunID:       attrMap[attrGlobalRunID],
		RunID:             attrMap[attrRunID],
		EventType:         attrMap[attrEventType],
		AgentID:           attrMap[attrAgentID],
		PayloadHash:       attrMap[attrPayloadHash],
		EmitterNodeID:     attrMap[attrEmitterNodeID],
		BatchSignature:    attrMap[attrBatchSignature],
		BatchSigningKeyID: attrMap[attrBatchSigningKeyID],
	}

	if v, ok := attrMap[attrParentRunID]; ok {
		e.ParentRunID = &v
	}
	if v, ok := attrMap[attrClusterID]; ok {
		e.ClusterID = &v
	}
	if v, ok := attrMap[attrTenantID]; ok {
		e.TenantID = &v
	}

	// Reconstruct TraceID and SpanID from native OTLP fields.
	if len(lr.TraceId) == 16 {
		e.TraceID = hex.EncodeToString(lr.TraceId)
	}
	if len(lr.SpanId) == 8 {
		e.SpanID = hex.EncodeToString(lr.SpanId)
	}

	// Reconstruct payload from log body.
	if lr.Body != nil {
		if bv, ok := lr.Body.GetValue().(*otlpcommonv1.AnyValue_BytesValue); ok {
			e.Payload = bv.BytesValue
		}
	}

	// Reconstruct StartedAt from TimeUnixNano.
	if lr.TimeUnixNano > 0 {
		e.StartedAt = time.Unix(0, int64(lr.TimeUnixNano)).UTC()
	}

	return e, nil
}

// strAttr is a helper that builds a string-valued OTLP KeyValue.
func strAttr(key, value string) *otlpcommonv1.KeyValue {
	return &otlpcommonv1.KeyValue{
		Key: key,
		Value: &otlpcommonv1.AnyValue{
			Value: &otlpcommonv1.AnyValue_StringValue{StringValue: value},
		},
	}
}
