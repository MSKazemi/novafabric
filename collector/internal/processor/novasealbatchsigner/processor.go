package novasealbatchsigner

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"log/slog"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/consumer"
	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/plog"
	"go.opentelemetry.io/collector/pdata/plog/plogotlp"
	otlpcollogspb "go.opentelemetry.io/proto/otlp/collector/logs/v1"
	otlplogspb "go.opentelemetry.io/proto/otlp/logs/v1"
	"google.golang.org/protobuf/proto"

	"github.com/novafabric/collector/pkg/canonical"
	"github.com/novafabric/collector/pkg/envelope"
	"github.com/novafabric/collector/pkg/metrics"
	"github.com/novafabric/collector/pkg/novaseal"
)

// novaSealBatchSignerProcessor implements processor.Logs.  For each
// ResourceLogs in the incoming plog.Logs it:
//
//  1. Converts the pdata representation to a proto *otlplogspb.ResourceLogs.
//  2. Produces canonical bytes via canonical.CanonicalEncode.
//  3. Signs the canonical bytes with the injected novaseal.Signer.
//  4. Writes nova.batch.signature and nova.batch.signing_key_id back into the
//     pdata ResourceLogs.Resource attributes.
//  5. Forwards the annotated plog.Logs to the next consumer.
type novaSealBatchSignerProcessor struct {
	cfg          *Config
	nextConsumer consumer.Logs
	signer       novaseal.Signer
	telemetry    component.TelemetrySettings

	// signLatency is the histogram used to record the per-ResourceLogs signing
	// duration.  May be nil when running in tests without a metrics registry.
	signLatency prometheus.Histogram

	// invalidEnvelopeVersion counts log records rejected due to unknown
	// envelope_version.  May be nil when running in tests without a registry.
	invalidEnvelopeVersion *prometheus.CounterVec
}

// Capabilities implements processor.Logs.
func (p *novaSealBatchSignerProcessor) Capabilities() consumer.Capabilities {
	return consumer.Capabilities{MutatesData: true}
}

// Start implements component.Component.
func (p *novaSealBatchSignerProcessor) Start(_ context.Context, _ component.Host) error {
	if p.signer == nil {
		slog.Warn("novaseal_batch_signer: no signer configured; processor will operate in pass-through mode")
	}
	return nil
}

// Shutdown implements component.Component.
func (p *novaSealBatchSignerProcessor) Shutdown(_ context.Context) error {
	return nil
}

// ConsumeLogs validates envelope versions, signs each ResourceLogs batch, and
// forwards the annotated plog.Logs to the next consumer.
//
// Envelope validation (BQ-017):
//   - Log records whose JSON body contains envelope_version != "1" are removed
//     before signing and are never forwarded.  An observable error is emitted
//     via slog and the nova_invalid_envelope_version_total counter.
//
// If signing fails:
//   - fail_open=false → returns an error; the batch is dropped and the next
//     consumer is NOT called.
//   - fail_open=true  → logs a warning and forwards the unsigned batch.
func (p *novaSealBatchSignerProcessor) ConsumeLogs(ctx context.Context, ld plog.Logs) error {
	if p.signer == nil {
		// No signer configured — pass through (no version validation).
		return p.nextConsumer.ConsumeLogs(ctx, ld)
	}

	// Step 0: reject log records with unknown envelope_version before signing.
	// Records are removed in-place from ld; no valid records → skip signing.
	p.filterInvalidEnvelopeVersions(ld)
	if ld.LogRecordCount() == 0 {
		return nil
	}

	// Convert plog.Logs to OTLP proto for canonical encoding.
	req := plogotlp.NewExportRequestFromLogs(ld)
	rawBytes, err := req.MarshalProto()
	if err != nil {
		return fmt.Errorf("novaseal_batch_signer: marshal plog to proto: %w", err)
	}

	var exportReq otlpcollogspb.ExportLogsServiceRequest
	if err := proto.Unmarshal(rawBytes, &exportReq); err != nil {
		return fmt.Errorf("novaseal_batch_signer: unmarshal proto: %w", err)
	}

	// Sign each ResourceLogs independently.
	for i, rl := range exportReq.ResourceLogs {
		signErr := p.signResourceLogs(ctx, ld, i, rl)
		if signErr != nil {
			if !p.cfg.FailOpen {
				return fmt.Errorf("novaseal_batch_signer: signing failed (fail_open=false): %w", signErr)
			}
			slog.Warn("novaseal_batch_signer: signing failed, continuing (fail_open=true)",
				"error", signErr,
				"resource_logs_index", i)
		}
	}

	return p.nextConsumer.ConsumeLogs(ctx, ld)
}

// filterInvalidEnvelopeVersions removes log records whose JSON body contains
// envelope_version != envelope.EnvelopeVersion ("1").  Records are dropped
// in-place from ld.  Unknown versions are logged at Error level and counted.
//
// Records whose body is not JSON (e.g. plain text) are passed through: only
// records with a parseable envelope_version field that differs from "1" are
// rejected.
func (p *novaSealBatchSignerProcessor) filterInvalidEnvelopeVersions(ld plog.Logs) {
	for i := 0; i < ld.ResourceLogs().Len(); i++ {
		rl := ld.ResourceLogs().At(i)
		for j := 0; j < rl.ScopeLogs().Len(); j++ {
			sl := rl.ScopeLogs().At(j)
			sl.LogRecords().RemoveIf(func(lr plog.LogRecord) bool {
				ver, ok := envelopeVersionFromRecord(lr)
				if !ok {
					// Body is not JSON or has no envelope_version field — pass through.
					return false
				}
				if ver == envelope.EnvelopeVersion {
					return false
				}
				// Unknown version: emit observable error and reject.
				slog.Error("novaseal_batch_signer: rejecting record with unknown envelope_version",
					"observed_version", ver,
					"expected_version", envelope.EnvelopeVersion,
				)
				if p.invalidEnvelopeVersion != nil {
					p.invalidEnvelopeVersion.WithLabelValues(ver).Inc()
				} else {
					metrics.InvalidEnvelopeVersionTotal.WithLabelValues(ver).Inc()
				}
				return true // remove this record
			})
		}
	}
}

// envelopeVersionFromRecord extracts the envelope_version string from a log
// record's body.  Returns ("", false) if the body is not parseable JSON or
// does not contain the envelope_version key.
func envelopeVersionFromRecord(lr plog.LogRecord) (string, bool) {
	body := lr.Body()
	var raw []byte
	switch body.Type() {
	case pcommon.ValueTypeStr:
		raw = []byte(body.Str())
	case pcommon.ValueTypeBytes:
		raw = body.Bytes().AsRaw()
	default:
		return "", false
	}
	var m map[string]json.RawMessage
	if err := json.Unmarshal(raw, &m); err != nil {
		return "", false
	}
	verRaw, ok := m["envelope_version"]
	if !ok {
		return "", false
	}
	var ver string
	if err := json.Unmarshal(verRaw, &ver); err != nil {
		// envelope_version is present but not a string — still observable.
		return string(verRaw), true
	}
	return ver, true
}

// signResourceLogs computes the canonical bytes for rl, calls the signer, and
// writes the signature attributes back into the pdata ResourceLogs at index idx.
func (p *novaSealBatchSignerProcessor) signResourceLogs(
	ctx context.Context,
	ld plog.Logs,
	idx int,
	rl *otlplogspb.ResourceLogs,
) error {
	start := time.Now()

	signCtx, cancel := context.WithTimeout(ctx, p.cfg.Timeout)
	defer cancel()

	canonicalBytes, err := canonical.CanonicalEncode(rl)
	if err != nil {
		p.observeLatency(time.Since(start))
		return fmt.Errorf("canonical encode: %w", err)
	}

	sig, keyID, err := p.signer.Sign(signCtx, canonicalBytes)
	if err != nil {
		p.observeLatency(time.Since(start))
		return fmt.Errorf("signer.Sign: %w", err)
	}

	p.observeLatency(time.Since(start))

	// Write signature back into the pdata ResourceLogs at the same index.
	pdataRL := ld.ResourceLogs().At(idx)
	attrs := pdataRL.Resource().Attributes()
	attrs.PutStr("nova.batch.signature", base64.StdEncoding.EncodeToString(sig))
	attrs.PutStr("nova.batch.signing_key_id", keyID)
	return nil
}

// observeLatency records the latency histogram, ignoring a nil histogram.
func (p *novaSealBatchSignerProcessor) observeLatency(d time.Duration) {
	if p.signLatency == nil {
		return
	}
	p.signLatency.Observe(d.Seconds())
}

// defaultSignLatency returns the package-level BatchSignLatency histogram.
// It is used in production when no custom registry is injected.
func defaultSignLatency() prometheus.Histogram {
	return metrics.BatchSignLatency
}

// defaultInvalidEnvelopeVersion returns nil so the processor falls back to the
// package-level InvalidEnvelopeVersionTotal counter in production.
func defaultInvalidEnvelopeVersion() *prometheus.CounterVec {
	return nil
}
