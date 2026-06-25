// Package metrics defines and registers the Prometheus metrics used by the
// NovaFabric collector tier.
//
// All metrics are registered against the global prometheus.DefaultRegisterer
// via promauto so they are automatically available to the default HTTP handler
// (prometheus/promhttp). For test isolation, use [NewRegistry] to obtain a
// fresh, independent registry and instantiate the metrics against it.
package metrics

import (
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

// batchSignLatencyBuckets are the histogram bucket boundaries for NovaSeal
// batch signing latency. The tightest bucket (0.1 ms) captures the fast path;
// the coarsest (10 ms) captures KMS round-trips under load.
var batchSignLatencyBuckets = []float64{0.0001, 0.0005, 0.001, 0.005, 0.01}

// Package-level metric singletons registered against the global default
// registry via promauto. Use [NewRegistry] in tests to avoid registration
// conflicts between test cases.
var (
	// SpoolSegments is the current number of open or sealed spool segments on
	// disk. Decreases as segments are forwarded and deleted.
	SpoolSegments = promauto.NewGauge(prometheus.GaugeOpts{
		Name: "nova_spool_segments",
		Help: "Current number of spool segments on disk (open + sealed).",
	})

	// SpoolBytes is the total size in bytes of all spool segment files.
	SpoolBytes = promauto.NewGauge(prometheus.GaugeOpts{
		Name: "nova_spool_bytes",
		Help: "Total size in bytes of all spool segment files on disk.",
	})

	// SpoolOldestEventAge is the age in seconds of the oldest event currently
	// held in the spool. Feeds the SLA alerting rule.
	SpoolOldestEventAge = promauto.NewGauge(prometheus.GaugeOpts{
		Name: "nova_spool_oldest_event_age_seconds",
		Help: "Age in seconds of the oldest event currently held in the spool.",
	})

	// SpoolDroppedSegments counts segments that were discarded due to storage
	// pressure, corruption, or TTL expiry.
	SpoolDroppedSegments = promauto.NewCounter(prometheus.CounterOpts{
		Name: "nova_spool_dropped_segments_total",
		Help: "Total number of spool segments dropped (storage pressure, corruption, or TTL).",
	})

	// BatchSignLatency tracks the end-to-end latency of the NovaSeal batch
	// signing operation, from canonical-encode to signature attachment.
	BatchSignLatency = promauto.NewHistogram(prometheus.HistogramOpts{
		Name:    "nova_batch_sign_latency_seconds",
		Help:    "End-to-end latency of the NovaSeal batch signing operation in seconds.",
		Buckets: batchSignLatencyBuckets,
	})

	// BatchSignErrors counts signing failures. The "reason" label carries the
	// short error category (e.g. "kms_unreachable", "timeout", "invalid_key").
	BatchSignErrors = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "nova_batch_sign_errors_total",
		Help: "Total number of NovaSeal batch signing errors, labelled by reason.",
	}, []string{"reason"})

	// ForwardedEvents counts events that the collector has successfully
	// forwarded to the upstream OTLP receiver or object store.
	ForwardedEvents = promauto.NewCounter(prometheus.CounterOpts{
		Name: "nova_collector_forwarded_events_total",
		Help: "Total number of events successfully forwarded by the collector.",
	})

	// EpilogFlushTimeouts counts cases where the epilog (end-of-run flush)
	// timed out before all pending events were drained from the spool.
	EpilogFlushTimeouts = promauto.NewCounter(prometheus.CounterOpts{
		Name: "nova_epilog_flush_timeout_total",
		Help: "Total number of epilog flush operations that timed out.",
	})

	// DLQEventsTotal counts events routed to the dead-letter queue (A-3).
	// Incremented when the spool is under backpressure and an event is
	// written to the DLQ instead of being silently dropped.
	DLQEventsTotal = promauto.NewCounter(prometheus.CounterOpts{
		Name: "nova_collector_dlq_events_total",
		Help: "Total number of events written to the dead-letter queue (A-3).",
	})

	// InvalidEnvelopeVersionTotal counts log records rejected because their
	// envelope_version is not the expected "1".  These records are dropped
	// before signing and must NOT reach downstream consumers unsigned.
	InvalidEnvelopeVersionTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "nova_invalid_envelope_version_total",
		Help: "Total log records rejected due to unknown envelope_version, labelled by observed version.",
	}, []string{"observed_version"})
)

// CollectorMetrics groups all collector metrics for injection into components
// that should not import package-level variables directly. This is the
// preferred pattern for testable components that receive their registry from
// the caller.
type CollectorMetrics struct {
	SpoolSegments               prometheus.Gauge
	SpoolBytes                  prometheus.Gauge
	SpoolOldestEventAge         prometheus.Gauge
	SpoolDroppedSegments        prometheus.Counter
	BatchSignLatency            prometheus.Histogram
	BatchSignErrors             *prometheus.CounterVec
	ForwardedEvents             prometheus.Counter
	EpilogFlushTimeouts         prometheus.Counter
	InvalidEnvelopeVersionTotal *prometheus.CounterVec
	DLQEventsTotal              prometheus.Counter
}

// NewRegistry creates a fresh, isolated Prometheus registry suitable for use
// in tests. All metrics are registered against the returned registry; the
// global default registry is not used. The returned [CollectorMetrics] mirrors
// the package-level singletons in behaviour.
//
// Example:
//
//	reg, m := metrics.NewRegistry()
//	m.ForwardedEvents.Inc()
//	// assert via promtest or manual gathering
func NewRegistry() (*prometheus.Registry, *CollectorMetrics) {
	reg := prometheus.NewRegistry()
	factory := promauto.With(reg)

	m := &CollectorMetrics{
		SpoolSegments: factory.NewGauge(prometheus.GaugeOpts{
			Name: "nova_spool_segments",
			Help: "Current number of spool segments on disk (open + sealed).",
		}),
		SpoolBytes: factory.NewGauge(prometheus.GaugeOpts{
			Name: "nova_spool_bytes",
			Help: "Total size in bytes of all spool segment files on disk.",
		}),
		SpoolOldestEventAge: factory.NewGauge(prometheus.GaugeOpts{
			Name: "nova_spool_oldest_event_age_seconds",
			Help: "Age in seconds of the oldest event currently held in the spool.",
		}),
		SpoolDroppedSegments: factory.NewCounter(prometheus.CounterOpts{
			Name: "nova_spool_dropped_segments_total",
			Help: "Total number of spool segments dropped (storage pressure, corruption, or TTL).",
		}),
		BatchSignLatency: factory.NewHistogram(prometheus.HistogramOpts{
			Name:    "nova_batch_sign_latency_seconds",
			Help:    "End-to-end latency of the NovaSeal batch signing operation in seconds.",
			Buckets: batchSignLatencyBuckets,
		}),
		BatchSignErrors: factory.NewCounterVec(prometheus.CounterOpts{
			Name: "nova_batch_sign_errors_total",
			Help: "Total number of NovaSeal batch signing errors, labelled by reason.",
		}, []string{"reason"}),
		ForwardedEvents: factory.NewCounter(prometheus.CounterOpts{
			Name: "nova_collector_forwarded_events_total",
			Help: "Total number of events successfully forwarded by the collector.",
		}),
		EpilogFlushTimeouts: factory.NewCounter(prometheus.CounterOpts{
			Name: "nova_epilog_flush_timeout_total",
			Help: "Total number of epilog flush operations that timed out.",
		}),
		InvalidEnvelopeVersionTotal: factory.NewCounterVec(prometheus.CounterOpts{
			Name: "nova_invalid_envelope_version_total",
			Help: "Total log records rejected due to unknown envelope_version, labelled by observed version.",
		}, []string{"observed_version"}),
		DLQEventsTotal: factory.NewCounter(prometheus.CounterOpts{
			Name: "nova_collector_dlq_events_total",
			Help: "Total number of events written to the dead-letter queue (A-3).",
		}),
	}

	return reg, m
}
