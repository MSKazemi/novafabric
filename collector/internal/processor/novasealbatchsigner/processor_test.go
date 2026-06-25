package novasealbatchsigner

import (
	"context"
	"encoding/base64"
	"errors"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/collector/consumer"
	"go.opentelemetry.io/collector/consumer/consumertest"
	"go.opentelemetry.io/collector/pdata/plog"

	"github.com/novafabric/collector/pkg/novaseal"
)

// staticSigner is a test-only novaseal.Signer that always returns a fixed
// signature and key ID, or a fixed error.
type staticSigner struct {
	sig   []byte
	keyID string
	err   error
}

func (s *staticSigner) Sign(_ context.Context, _ []byte) ([]byte, string, error) {
	return s.sig, s.keyID, s.err
}

func (s *staticSigner) Verify(_ context.Context, _ []byte, _ []byte, _ string) error {
	return s.err
}

// Ensure staticSigner satisfies the interface at compile time.
var _ novaseal.Signer = (*staticSigner)(nil)

// makeTestLogs builds a plog.Logs with n ResourceLogs, each containing one
// log record.
func makeTestLogs(n int) plog.Logs {
	ld := plog.NewLogs()
	for i := 0; i < n; i++ {
		rl := ld.ResourceLogs().AppendEmpty()
		rl.Resource().Attributes().PutStr("service.name", "test")
		sl := rl.ScopeLogs().AppendEmpty()
		lr := sl.LogRecords().AppendEmpty()
		lr.Body().SetStr("hello")
	}
	return ld
}

// buildProcessor creates a processor wired with the given signer and sink.
func buildProcessor(signer novaseal.Signer, failOpen bool, sink consumer.Logs) *novaSealBatchSignerProcessor {
	return &novaSealBatchSignerProcessor{
		cfg: &Config{
			FailOpen: failOpen,
			Timeout:  5 * time.Second,
		},
		nextConsumer: sink,
		signer:       signer,
		// signLatency is nil in tests — observeLatency is a no-op.
	}
}

// TestProcessor_SignsEachResourceLogs verifies that a processor with 2
// ResourceLogs in the input annotates both with nova.batch.signature.
func TestProcessor_SignsEachResourceLogs(t *testing.T) {
	signer := &staticSigner{sig: []byte("fakesig"), keyID: "key-1"}
	sink := &consumertest.LogsSink{}
	p := buildProcessor(signer, false, sink)

	ld := makeTestLogs(2)
	require.NoError(t, p.ConsumeLogs(context.Background(), ld))

	allLogs := sink.AllLogs()
	require.Len(t, allLogs, 1)
	outLD := allLogs[0]
	require.Equal(t, 2, outLD.ResourceLogs().Len())

	for i := 0; i < 2; i++ {
		attrs := outLD.ResourceLogs().At(i).Resource().Attributes()

		sig, ok := attrs.Get("nova.batch.signature")
		assert.True(t, ok, "ResourceLogs[%d] must have nova.batch.signature", i)
		assert.NotEmpty(t, sig.Str())

		keyID, ok2 := attrs.Get("nova.batch.signing_key_id")
		assert.True(t, ok2, "ResourceLogs[%d] must have nova.batch.signing_key_id", i)
		assert.Equal(t, "key-1", keyID.Str())
	}
}

// TestProcessor_FailClosedOnSignError verifies that when the signer returns an
// error and fail_open=false, ConsumeLogs returns an error and the next
// consumer is NOT called.
func TestProcessor_FailClosedOnSignError(t *testing.T) {
	signer := &staticSigner{err: errors.New("KMS unavailable")}
	sink := &consumertest.LogsSink{}
	p := buildProcessor(signer, false /* failOpen */, sink)

	ld := makeTestLogs(1)
	err := p.ConsumeLogs(context.Background(), ld)
	assert.Error(t, err, "ConsumeLogs must return an error when fail_open=false and signing fails")
	assert.Equal(t, 0, sink.LogRecordCount(), "nextConsumer must NOT be called on sign error with fail_open=false")
}

// TestProcessor_FailOpenContinues verifies that when the signer returns an
// error and fail_open=true, ConsumeLogs returns nil and the next consumer IS
// called with the (unsigned) batch.
func TestProcessor_FailOpenContinues(t *testing.T) {
	signer := &staticSigner{err: errors.New("KMS unavailable")}
	sink := &consumertest.LogsSink{}
	p := buildProcessor(signer, true /* failOpen */, sink)

	ld := makeTestLogs(1)
	err := p.ConsumeLogs(context.Background(), ld)
	assert.NoError(t, err, "ConsumeLogs must succeed when fail_open=true")
	assert.Greater(t, sink.LogRecordCount(), 0, "nextConsumer must be called with unsigned batch when fail_open=true")

	// Verify that no signature attribute was written (since signing failed).
	outLD := sink.AllLogs()[0]
	attrs := outLD.ResourceLogs().At(0).Resource().Attributes()
	_, hasSig := attrs.Get("nova.batch.signature")
	assert.False(t, hasSig, "no nova.batch.signature attribute expected when signing failed")
}

// TestProcessor_NoSignerPassthrough verifies that a processor with no signer
// forwards the batch unchanged.
func TestProcessor_NoSignerPassthrough(t *testing.T) {
	sink := &consumertest.LogsSink{}
	p := buildProcessor(nil, false, sink)

	ld := makeTestLogs(3)
	require.NoError(t, p.ConsumeLogs(context.Background(), ld))
	assert.Greater(t, sink.LogRecordCount(), 0, "nextConsumer must be called even without a signer")
}

// TestProcessor_Capabilities verifies that the processor declares it mutates data.
func TestProcessor_Capabilities(t *testing.T) {
	sink := &consumertest.LogsSink{}
	p := buildProcessor(nil, false, sink)
	caps := p.Capabilities()
	assert.True(t, caps.MutatesData)
}

// TestProcessor_StartShutdown exercises the component lifecycle.
func TestProcessor_StartShutdown(t *testing.T) {
	sink := &consumertest.LogsSink{}
	p := buildProcessor(nil, false, sink)
	require.NoError(t, p.Start(context.Background(), nil))
	require.NoError(t, p.Shutdown(context.Background()))
}

// TestProcessor_SignatureIsBase64 verifies that the written signature attribute
// is the base64-standard-encoding of the raw bytes returned by the signer.
func TestProcessor_SignatureIsBase64(t *testing.T) {
	rawSig := []byte{0xDE, 0xAD, 0xBE, 0xEF}
	signer := &staticSigner{sig: rawSig, keyID: "k1"}
	sink := &consumertest.LogsSink{}
	p := buildProcessor(signer, false, sink)

	ld := makeTestLogs(1)
	require.NoError(t, p.ConsumeLogs(context.Background(), ld))

	outLD := sink.AllLogs()[0]
	attrs := outLD.ResourceLogs().At(0).Resource().Attributes()
	sigVal, _ := attrs.Get("nova.batch.signature")

	expected := base64.StdEncoding.EncodeToString(rawSig)
	assert.Equal(t, expected, sigVal.Str())
}

// makeEnvelopeLog builds a plog.Logs with one log record whose body is a JSON
// envelope with the given envelope_version.
func makeEnvelopeLog(envelopeVersion string) plog.Logs {
	ld := plog.NewLogs()
	rl := ld.ResourceLogs().AppendEmpty()
	rl.Resource().Attributes().PutStr("service.name", "test")
	sl := rl.ScopeLogs().AppendEmpty()
	lr := sl.LogRecords().AppendEmpty()
	lr.Body().SetStr(`{"envelope_version":"` + envelopeVersion + `","event_type":"run.start"}`)
	return ld
}

// TestProcessor_ValidEnvelopeVersionPassesThrough verifies that a record with
// envelope_version "1" is not filtered and reaches the next consumer.
func TestProcessor_ValidEnvelopeVersionPassesThrough(t *testing.T) {
	signer := &staticSigner{sig: []byte("sig"), keyID: "k1"}
	sink := &consumertest.LogsSink{}
	p := buildProcessor(signer, false, sink)

	ld := makeEnvelopeLog("1")
	require.NoError(t, p.ConsumeLogs(context.Background(), ld))
	assert.Equal(t, 1, sink.LogRecordCount(), "valid envelope_version '1' must pass through")
}

// TestProcessor_UnknownEnvelopeVersionIsRejected verifies that a record with
// an unknown envelope_version is removed before signing and never forwarded.
func TestProcessor_UnknownEnvelopeVersionIsRejected(t *testing.T) {
	signer := &staticSigner{sig: []byte("sig"), keyID: "k1"}
	sink := &consumertest.LogsSink{}
	p := buildProcessor(signer, false, sink)

	ld := makeEnvelopeLog("99")
	require.NoError(t, p.ConsumeLogs(context.Background(), ld))
	assert.Equal(t, 0, sink.LogRecordCount(), "unknown envelope_version must be rejected before signing")
}

// TestProcessor_MixedVersionsFilteredCorrectly verifies that in a batch with
// both valid and invalid envelope_version records, only the valid ones are
// forwarded.
func TestProcessor_MixedVersionsFilteredCorrectly(t *testing.T) {
	signer := &staticSigner{sig: []byte("sig"), keyID: "k1"}
	sink := &consumertest.LogsSink{}
	p := buildProcessor(signer, false, sink)

	ld := plog.NewLogs()
	// ResourceLogs 0: one valid record
	rl0 := ld.ResourceLogs().AppendEmpty()
	sl0 := rl0.ScopeLogs().AppendEmpty()
	lr0 := sl0.LogRecords().AppendEmpty()
	lr0.Body().SetStr(`{"envelope_version":"1","event_type":"run.start"}`)
	// ResourceLogs 1: one invalid record
	rl1 := ld.ResourceLogs().AppendEmpty()
	sl1 := rl1.ScopeLogs().AppendEmpty()
	lr1 := sl1.LogRecords().AppendEmpty()
	lr1.Body().SetStr(`{"envelope_version":"future","event_type":"run.start"}`)

	require.NoError(t, p.ConsumeLogs(context.Background(), ld))
	assert.Equal(t, 1, sink.LogRecordCount(), "only the valid envelope_version record must be forwarded")
}

// TestProcessor_NonJSONBodyPassesThrough verifies that a log record with a
// non-JSON body (e.g. plain text) is passed through without rejection.
func TestProcessor_NonJSONBodyPassesThrough(t *testing.T) {
	signer := &staticSigner{sig: []byte("sig"), keyID: "k1"}
	sink := &consumertest.LogsSink{}
	p := buildProcessor(signer, false, sink)

	ld := makeTestLogs(1) // body is "hello" — not JSON
	require.NoError(t, p.ConsumeLogs(context.Background(), ld))
	assert.Equal(t, 1, sink.LogRecordCount(), "non-JSON body must not be rejected")
}

// TestEnvelopeVersionFromRecord_ValidVersion verifies version extraction from a
// standard envelope JSON body.
func TestEnvelopeVersionFromRecord_ValidVersion(t *testing.T) {
	lr := plog.NewLogRecord()
	lr.Body().SetStr(`{"envelope_version":"1","event_type":"run.start"}`)
	ver, ok := envelopeVersionFromRecord(lr)
	assert.True(t, ok)
	assert.Equal(t, "1", ver)
}

// TestEnvelopeVersionFromRecord_MissingKey returns false for bodies without
// envelope_version.
func TestEnvelopeVersionFromRecord_MissingKey(t *testing.T) {
	lr := plog.NewLogRecord()
	lr.Body().SetStr(`{"event_type":"run.start"}`)
	_, ok := envelopeVersionFromRecord(lr)
	assert.False(t, ok)
}

// TestEnvelopeVersionFromRecord_PlainText returns false for non-JSON bodies.
func TestEnvelopeVersionFromRecord_PlainText(t *testing.T) {
	lr := plog.NewLogRecord()
	lr.Body().SetStr("plain text log line")
	_, ok := envelopeVersionFromRecord(lr)
	assert.False(t, ok)
}
