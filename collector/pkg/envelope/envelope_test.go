package envelope_test

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/novafabric/collector/pkg/envelope"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// ulidPattern matches a valid Crockford Base32 ULID (26 chars, first char 0-7).
var ulidPattern = regexp.MustCompile(`^[0-7][0-9A-HJKMNP-TV-Z]{25}$`)

// fullEnvelope returns an EventEnvelope with all optional fields populated,
// suitable for round-trip tests.
func fullEnvelope() *envelope.EventEnvelope {
	parentRunID := "01JVXK0000000000000000000P"
	clusterID := "cluster-01"
	tenantID := "tenant-acme"
	payload := []byte(`{"model":"gpt-4","tokens":128}`)
	return &envelope.EventEnvelope{
		EnvelopeVersion:   envelope.EnvelopeVersion,
		SchemaVersion:     envelope.SchemaVersion,
		EventID:           envelope.NewULID(),
		GlobalRunID:       "01JVXK0000000000000000000G",
		RunID:             "01JVXK0000000000000000000R",
		ParentRunID:       &parentRunID,
		EventType:         string(envelope.EventTypeModelCall),
		TraceID:           "4bf92f3577b34da6a3ce929d0e0e4736",
		SpanID:            "00f067aa0ba902b7",
		AgentID:           "nova-agent-1",
		ClusterID:         &clusterID,
		TenantID:          &tenantID,
		StartedAt:         time.Date(2026, 5, 12, 10, 0, 0, 0, time.UTC),
		BatchSignature:    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
		BatchSigningKeyID: "00000000-0000-0000-0000-000000000001",
		Payload:           payload,
		PayloadHash:       "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
		EmitterNodeID:     "node-gpu-42",
	}
}

// --- New() and NewULID() ---

func TestNew_FillsRequiredFields(t *testing.T) {
	e := envelope.New("model_call", "agent-1", "run-01", "global-01")

	assert.Equal(t, envelope.EnvelopeVersion, e.EnvelopeVersion)
	assert.Equal(t, envelope.SchemaVersion, e.SchemaVersion)
	assert.NotEmpty(t, e.EventID)
	assert.Equal(t, "global-01", e.GlobalRunID)
	assert.Equal(t, "run-01", e.RunID)
	assert.Equal(t, "model_call", e.EventType)
	assert.Equal(t, "agent-1", e.AgentID)
	assert.False(t, e.StartedAt.IsZero())
}

func TestNew_EventIDIsValidULID(t *testing.T) {
	e := envelope.New("span", "a", "r", "g")
	assert.Regexp(t, ulidPattern, e.EventID, "EventID must be a valid ULID")
}

func TestNewULID_ProducesUniqueValues(t *testing.T) {
	seen := make(map[string]struct{}, 100)
	for i := 0; i < 100; i++ {
		u := envelope.NewULID()
		assert.Regexp(t, ulidPattern, u)
		_, dup := seen[u]
		assert.False(t, dup, "ULID %q was generated twice", u)
		seen[u] = struct{}{}
	}
}

// --- OTLP round-trip ---

func TestOTLPRoundTrip_AllFieldsPreserved(t *testing.T) {
	orig := fullEnvelope()

	lr := envelope.ToOTLPLogRecord(orig)
	require.NotNil(t, lr)

	recovered, err := envelope.FromOTLPLogRecord(lr)
	require.NoError(t, err)

	assert.Equal(t, orig.EnvelopeVersion, recovered.EnvelopeVersion)
	assert.Equal(t, orig.SchemaVersion, recovered.SchemaVersion)
	assert.Equal(t, orig.EventID, recovered.EventID)
	assert.Equal(t, orig.GlobalRunID, recovered.GlobalRunID)
	assert.Equal(t, orig.RunID, recovered.RunID)
	assert.Equal(t, orig.EventType, recovered.EventType)
	assert.Equal(t, orig.TraceID, recovered.TraceID)
	assert.Equal(t, orig.SpanID, recovered.SpanID)
	assert.Equal(t, orig.AgentID, recovered.AgentID)
	assert.Equal(t, orig.PayloadHash, recovered.PayloadHash)
	assert.Equal(t, orig.EmitterNodeID, recovered.EmitterNodeID)
	assert.Equal(t, orig.BatchSignature, recovered.BatchSignature)
	assert.Equal(t, orig.BatchSigningKeyID, recovered.BatchSigningKeyID)
	assert.Equal(t, orig.Payload, recovered.Payload)

	// Pointer fields.
	require.NotNil(t, recovered.ParentRunID)
	assert.Equal(t, *orig.ParentRunID, *recovered.ParentRunID)
	require.NotNil(t, recovered.ClusterID)
	assert.Equal(t, *orig.ClusterID, *recovered.ClusterID)
	require.NotNil(t, recovered.TenantID)
	assert.Equal(t, *orig.TenantID, *recovered.TenantID)

	// StartedAt is stored as nanoseconds; check within 1ms tolerance.
	assert.WithinDuration(t, orig.StartedAt, recovered.StartedAt, time.Millisecond)
}

func TestOTLPRoundTrip_NilPointerFieldsOmitted(t *testing.T) {
	e := envelope.New("span", "a", "r", "g")
	e.TraceID = "4bf92f3577b34da6a3ce929d0e0e4736"
	e.SpanID = "00f067aa0ba902b7"

	lr := envelope.ToOTLPLogRecord(e)
	recovered, err := envelope.FromOTLPLogRecord(lr)
	require.NoError(t, err)

	assert.Nil(t, recovered.ParentRunID)
	assert.Nil(t, recovered.ClusterID)
	assert.Nil(t, recovered.TenantID)
}

func TestOTLPFromLogRecord_NilInput(t *testing.T) {
	_, err := envelope.FromOTLPLogRecord(nil)
	assert.Error(t, err)
}

// --- CloudEvents round-trip ---

func TestCloudEventsRoundTrip_AllFieldsPreserved(t *testing.T) {
	orig := fullEnvelope()

	ce, err := envelope.ToCloudEvent(orig)
	require.NoError(t, err)

	recovered, err := envelope.FromCloudEvent(ce)
	require.NoError(t, err)

	assert.Equal(t, orig.EventID, recovered.EventID)
	assert.Equal(t, orig.EventType, recovered.EventType)
	assert.Equal(t, orig.AgentID, recovered.AgentID)
	assert.Equal(t, orig.GlobalRunID, recovered.GlobalRunID)
	assert.Equal(t, orig.RunID, recovered.RunID)
	assert.Equal(t, orig.TraceID, recovered.TraceID)
	assert.Equal(t, orig.SpanID, recovered.SpanID)
	assert.Equal(t, orig.BatchSignature, recovered.BatchSignature)
	assert.Equal(t, orig.BatchSigningKeyID, recovered.BatchSigningKeyID)
	assert.Equal(t, orig.PayloadHash, recovered.PayloadHash)
	assert.Equal(t, orig.EmitterNodeID, recovered.EmitterNodeID)

	// StartedAt: CloudEvents time has second precision by default; accept 1s tolerance.
	assert.WithinDuration(t, orig.StartedAt, recovered.StartedAt, time.Second)

	// Payload: compare JSON equivalence (the SDK may re-encode).
	require.NotNil(t, recovered.Payload)
	var origJSON, recoveredJSON interface{}
	require.NoError(t, json.Unmarshal(orig.Payload, &origJSON))
	require.NoError(t, json.Unmarshal(recovered.Payload, &recoveredJSON))
	assert.Equal(t, origJSON, recoveredJSON, "payload JSON content must be identical")

	// Pointer fields.
	require.NotNil(t, recovered.ParentRunID)
	assert.Equal(t, *orig.ParentRunID, *recovered.ParentRunID)
	require.NotNil(t, recovered.ClusterID)
	assert.Equal(t, *orig.ClusterID, *recovered.ClusterID)
	require.NotNil(t, recovered.TenantID)
	assert.Equal(t, *orig.TenantID, *recovered.TenantID)
}

func TestCloudEventsRoundTrip_NilPointerFieldsOmitted(t *testing.T) {
	e := envelope.New("run.start", "agent-x", "run-x", "global-x")
	e.TraceID = "4bf92f3577b34da6a3ce929d0e0e4736"
	e.SpanID = "00f067aa0ba902b7"
	e.Payload = []byte(`{}`)

	ce, err := envelope.ToCloudEvent(e)
	require.NoError(t, err)

	recovered, err := envelope.FromCloudEvent(ce)
	require.NoError(t, err)

	assert.Nil(t, recovered.ParentRunID)
	assert.Nil(t, recovered.ClusterID)
	assert.Nil(t, recovered.TenantID)
}

// TestTraceparentFormat verifies that the traceparent extension in the
// CloudEvent follows the W3C Trace Context format: "00-<32-hex>-<16-hex>-01"
func TestTraceparentFormat(t *testing.T) {
	e := envelope.New("model_call", "agent-1", "run-1", "global-1")
	e.TraceID = "4bf92f3577b34da6a3ce929d0e0e4736"
	e.SpanID = "00f067aa0ba902b7"
	e.Payload = []byte(`{}`)

	ce, err := envelope.ToCloudEvent(e)
	require.NoError(t, err)

	exts := ce.Extensions()
	tp, ok := exts["traceparent"]
	require.True(t, ok, "traceparent extension must be present")

	tpStr := strings.TrimSpace(strings.ToLower(fmt.Sprintf("%v", tp)))
	parts := strings.Split(tpStr, "-")
	require.Len(t, parts, 4, "traceparent must have 4 dash-separated components")
	assert.Equal(t, "00", parts[0], "version must be 00")
	assert.Equal(t, "4bf92f3577b34da6a3ce929d0e0e4736", parts[1], "trace-id must match")
	assert.Equal(t, "00f067aa0ba902b7", parts[2], "parent-id must match")
	assert.Equal(t, "01", parts[3], "trace-flags must be 01")
}

func TestToCloudEvent_InvalidPayloadReturnsError(t *testing.T) {
	e := envelope.New("span", "a", "r", "g")
	e.Payload = []byte(`{not valid json}`)

	_, err := envelope.ToCloudEvent(e)
	assert.Error(t, err, "invalid JSON payload must cause an error")
}
