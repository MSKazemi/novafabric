package envelope

import (
	"encoding/json"
	"fmt"

	cloudevents "github.com/cloudevents/sdk-go/v2"
)

// traceparentFromEnvelope builds a W3C Trace Context traceparent header value
// from the envelope's TraceID and SpanID.
// Format: "00-<32-hex-trace-id>-<16-hex-span-id>-01"
func traceparentFromEnvelope(e *EventEnvelope) string {
	return fmt.Sprintf("00-%s-%s-01", e.TraceID, e.SpanID)
}

// ToCloudEvent converts an EventEnvelope to a CloudEvents v1.0 event.
//
// Field mapping:
//   - id         ← EventID
//   - source     ← "nova://agent/{AgentID}"
//   - type       ← EventType
//   - time       ← StartedAt
//   - specversion = "1.0"
//   - Extension "traceparent" ← W3C traceparent: "00-{TraceID}-{SpanID}-01"
//   - data       ← Payload (raw JSON bytes)
//
// Returns an error if the envelope payload is not valid JSON and is non-empty.
func ToCloudEvent(e *EventEnvelope) (cloudevents.Event, error) {
	ce := cloudevents.NewEvent()
	ce.SetID(e.EventID)
	ce.SetSource(fmt.Sprintf("nova://agent/%s", e.AgentID))
	ce.SetType(e.EventType)
	ce.SetTime(e.StartedAt)
	ce.SetSpecVersion("1.0")

	if e.TraceID != "" && e.SpanID != "" {
		ce.SetExtension("traceparent", traceparentFromEnvelope(e))
	}

	// Optional extensions for run identifiers.
	if e.GlobalRunID != "" {
		ce.SetExtension("novarunglobal", e.GlobalRunID)
	}
	if e.RunID != "" {
		ce.SetExtension("novarun", e.RunID)
	}
	if e.ParentRunID != nil {
		ce.SetExtension("novaparentrun", *e.ParentRunID)
	}
	if e.BatchSignature != "" {
		ce.SetExtension("novabatchsig", e.BatchSignature)
	}
	if e.BatchSigningKeyID != "" {
		ce.SetExtension("novabatchkid", e.BatchSigningKeyID)
	}
	if e.PayloadHash != "" {
		ce.SetExtension("novapayloadhash", e.PayloadHash)
	}
	if e.EmitterNodeID != "" {
		ce.SetExtension("novaemitter", e.EmitterNodeID)
	}
	if e.ClusterID != nil {
		ce.SetExtension("novacluster", *e.ClusterID)
	}
	if e.TenantID != nil {
		ce.SetExtension("novatenant", *e.TenantID)
	}

	// Set data content type and payload.
	ce.SetDataContentType("application/json")
	if len(e.Payload) > 0 {
		// Validate that payload is JSON before embedding.
		if !json.Valid(e.Payload) {
			return cloudevents.Event{}, fmt.Errorf("envelope: payload is not valid JSON")
		}
		if err := ce.SetData("application/json", e.Payload); err != nil {
			return cloudevents.Event{}, fmt.Errorf("envelope: set CloudEvent data: %w", err)
		}
	}

	return ce, nil
}

// FromCloudEvent reconstructs an EventEnvelope from a CloudEvents event
// produced by [ToCloudEvent]. Returns an error if required fields are absent.
func FromCloudEvent(ce cloudevents.Event) (*EventEnvelope, error) {
	e := &EventEnvelope{
		EnvelopeVersion: EnvelopeVersion,
		SchemaVersion:   SchemaVersion,
		EventID:         ce.ID(),
		EventType:       ce.Type(),
		StartedAt:       ce.Time().UTC(),
	}

	// Parse source: "nova://agent/{AgentID}"
	src := ce.Source()
	const prefix = "nova://agent/"
	if len(src) > len(prefix) && src[:len(prefix)] == prefix {
		e.AgentID = src[len(prefix):]
	} else {
		return nil, fmt.Errorf("envelope: unexpected CloudEvent source %q (want nova://agent/<id>)", src)
	}

	// Reconstruct TraceID and SpanID from traceparent extension.
	// Format: "00-<32hex>-<16hex>-<flags>"
	if tp, ok := ce.Extensions()["traceparent"]; ok && tp != "" {
		tpStr := fmt.Sprintf("%v", tp)
		parts := splitTraceparent(tpStr)
		if len(parts) == 4 {
			e.TraceID = parts[1]
			e.SpanID = parts[2]
		}
	}

	// Optional run identifiers from extensions.
	exts := ce.Extensions()

	if v, ok := stringExt(exts, "novarunglobal"); ok {
		e.GlobalRunID = v
	}
	if v, ok := stringExt(exts, "novarun"); ok {
		e.RunID = v
	}
	if v, ok := stringExt(exts, "novaparentrun"); ok {
		e.ParentRunID = &v
	}
	if v, ok := stringExt(exts, "novabatchsig"); ok {
		e.BatchSignature = v
	}
	if v, ok := stringExt(exts, "novabatchkid"); ok {
		e.BatchSigningKeyID = v
	}
	if v, ok := stringExt(exts, "novapayloadhash"); ok {
		e.PayloadHash = v
	}
	if v, ok := stringExt(exts, "novaemitter"); ok {
		e.EmitterNodeID = v
	}
	if v, ok := stringExt(exts, "novacluster"); ok {
		e.ClusterID = &v
	}
	if v, ok := stringExt(exts, "novatenant"); ok {
		e.TenantID = &v
	}

	// Payload from data.
	if ce.Data() != nil && len(ce.Data()) > 0 {
		e.Payload = ce.Data()
	}

	return e, nil
}

// splitTraceparent splits a traceparent string by "-". Returns nil if the
// string has fewer than 4 segments.
func splitTraceparent(tp string) []string {
	var parts []string
	start := 0
	for i := 0; i < len(tp); i++ {
		if tp[i] == '-' {
			parts = append(parts, tp[start:i])
			start = i + 1
		}
	}
	parts = append(parts, tp[start:])
	return parts
}

// stringExt extracts an extension value as a string from a CloudEvents
// extensions map. Returns ("", false) if absent.
func stringExt(exts map[string]interface{}, key string) (string, bool) {
	v, ok := exts[key]
	if !ok || v == nil {
		return "", false
	}
	s := fmt.Sprintf("%v", v)
	if s == "" {
		return "", false
	}
	return s, true
}
