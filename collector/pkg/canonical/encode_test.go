package canonical_test

import (
	"bytes"
	"testing"

	"github.com/novafabric/collector/pkg/canonical"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	otlpcommonv1 "go.opentelemetry.io/proto/otlp/common/v1"
	otlplogspb "go.opentelemetry.io/proto/otlp/logs/v1"
	otlpresourcepb "go.opentelemetry.io/proto/otlp/resource/v1"
)

// makeAttr is a convenience helper that produces a string KeyValue.
func makeAttr(key, val string) *otlpcommonv1.KeyValue {
	return &otlpcommonv1.KeyValue{
		Key:   key,
		Value: &otlpcommonv1.AnyValue{Value: &otlpcommonv1.AnyValue_StringValue{StringValue: val}},
	}
}

// makeRL builds a minimal ResourceLogs with the given resource attributes and
// a single ScopeLog that contains the provided log records.
func makeRL(resourceAttrs []*otlpcommonv1.KeyValue, records ...*otlplogspb.LogRecord) *otlplogspb.ResourceLogs {
	return &otlplogspb.ResourceLogs{
		Resource: &otlpresourcepb.Resource{Attributes: resourceAttrs},
		ScopeLogs: []*otlplogspb.ScopeLogs{
			{LogRecords: records},
		},
	}
}

// TestCanonicalEncode_Deterministic verifies that encoding the same
// ResourceLogs 100 times always produces identical bytes.
func TestCanonicalEncode_Deterministic(t *testing.T) {
	rl := makeRL(
		[]*otlpcommonv1.KeyValue{makeAttr("b", "2"), makeAttr("a", "1")},
		&otlplogspb.LogRecord{Attributes: []*otlpcommonv1.KeyValue{makeAttr("z", "z"), makeAttr("m", "m")}},
	)

	first, err := canonical.CanonicalEncode(rl)
	require.NoError(t, err)
	require.NotEmpty(t, first)

	for i := 0; i < 99; i++ {
		got, err := canonical.CanonicalEncode(rl)
		require.NoError(t, err, "iteration %d", i)
		assert.True(t, bytes.Equal(first, got), "encoding differs on iteration %d", i)
	}
}

// TestCanonicalEncode_AttributeSort verifies that two ResourceLogs with the
// same resource and log-record attributes in different orders produce identical
// canonical bytes.
func TestCanonicalEncode_AttributeSort(t *testing.T) {
	lr1 := &otlplogspb.LogRecord{
		Attributes: []*otlpcommonv1.KeyValue{makeAttr("z", "z"), makeAttr("a", "a"), makeAttr("m", "m")},
	}
	lr2 := &otlplogspb.LogRecord{
		Attributes: []*otlpcommonv1.KeyValue{makeAttr("m", "m"), makeAttr("z", "z"), makeAttr("a", "a")},
	}

	rl1 := makeRL(
		[]*otlpcommonv1.KeyValue{makeAttr("beta", "2"), makeAttr("alpha", "1")},
		lr1,
	)
	rl2 := makeRL(
		[]*otlpcommonv1.KeyValue{makeAttr("alpha", "1"), makeAttr("beta", "2")},
		lr2,
	)

	b1, err := canonical.CanonicalEncode(rl1)
	require.NoError(t, err)

	b2, err := canonical.CanonicalEncode(rl2)
	require.NoError(t, err)

	assert.True(t, bytes.Equal(b1, b2), "different attribute order should produce identical canonical bytes")
}

// TestCanonicalEncode_ExcludesSignatureAttrs verifies that the signature
// fields are stripped from Resource.attributes before encoding.
func TestCanonicalEncode_ExcludesSignatureAttrs(t *testing.T) {
	rl := makeRL(
		[]*otlpcommonv1.KeyValue{
			makeAttr("nova.batch.signature", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
			makeAttr("nova.batch.signing_key_id", "00000000-0000-0000-0000-000000000001"),
			makeAttr("nova.agent_id", "agent-1"),
		},
	)

	out, err := canonical.CanonicalEncode(rl)
	require.NoError(t, err)

	// The output bytes must not contain the signature or key-id values.
	assert.NotContains(t, string(out), "nova.batch.signature",
		"canonical output must not contain nova.batch.signature key")
	assert.NotContains(t, string(out), "nova.batch.signing_key_id",
		"canonical output must not contain nova.batch.signing_key_id key")
	// The retained attribute should still be present.
	assert.Contains(t, string(out), "nova.agent_id",
		"canonical output must retain non-excluded attributes")
}

// TestCanonicalEncode_PreservesLogRecordOrder verifies that five log records
// inserted in order A,B,C,D,E appear in that order in the canonical bytes.
func TestCanonicalEncode_PreservesLogRecordOrder(t *testing.T) {
	labels := []string{"alpha", "bravo", "charlie", "delta", "echo"}
	records := make([]*otlplogspb.LogRecord, len(labels))
	for i, l := range labels {
		records[i] = &otlplogspb.LogRecord{
			Attributes: []*otlpcommonv1.KeyValue{makeAttr("seq", l)},
		}
	}

	rl := makeRL(nil, records...)
	out, err := canonical.CanonicalEncode(rl)
	require.NoError(t, err)

	s := string(out)
	prevIdx := -1
	for _, l := range labels {
		idx := bytes.Index([]byte(s), []byte(l))
		assert.Greater(t, idx, prevIdx,
			"log record %q must appear after preceding record in canonical bytes", l)
		if idx > prevIdx {
			prevIdx = idx
		}
	}
}

// TestCanonicalEncode_NilInput verifies that a nil input returns an error
// rather than panicking.
func TestCanonicalEncode_NilInput(t *testing.T) {
	_, err := canonical.CanonicalEncode(nil)
	assert.Error(t, err)
}

// TestCanonicalEncode_DoesNotMutateInput verifies that the caller's
// ResourceLogs is not modified by encoding.
func TestCanonicalEncode_DoesNotMutateInput(t *testing.T) {
	attrs := []*otlpcommonv1.KeyValue{
		makeAttr("nova.batch.signature", "sig"),
		makeAttr("z", "z"),
		makeAttr("a", "a"),
	}
	rl := makeRL(attrs)

	originalOrder := make([]string, len(attrs))
	for i, kv := range attrs {
		originalOrder[i] = kv.Key
	}

	_, err := canonical.CanonicalEncode(rl)
	require.NoError(t, err)

	// Check that the original slice was not modified.
	for i, kv := range rl.Resource.Attributes {
		assert.Equal(t, originalOrder[i], kv.Key,
			"CanonicalEncode must not mutate the caller's attribute slice")
	}
}
