package canonical

import (
	"fmt"
	"sort"

	otlpcommonpb "go.opentelemetry.io/proto/otlp/common/v1"
	otlplogspb "go.opentelemetry.io/proto/otlp/logs/v1"
	"google.golang.org/protobuf/proto"
)

// canonicalStripKeys lists the attribute keys that must be removed before
// computing the canonical encoding to avoid a circular dependency when
// verifying a signature.
var canonicalStripKeys = map[string]struct{}{
	"nova.batch.signature":      {},
	"nova.batch.signing_key_id": {},
}

// CanonicalEncode produces a stable, deterministic byte sequence from rl
// suitable for signing and verification.
//
// The algorithm (defined in ADR-001) is:
//  1. Strip nova.batch.signature and nova.batch.signing_key_id from
//     Resource.attributes.
//  2. Sort remaining Resource.attributes by key (lexicographic ascending).
//  3. Sort each LogRecord.attributes by key (lexicographic ascending).
//  4. Preserve LogRecord list order.
//  5. Preserve unknown fields.
//  6. Marshal with proto.MarshalOptions{Deterministic: true}.
//
// The returned bytes are passed directly to novaseal.Signer.Sign.
func CanonicalEncode(rl *otlplogspb.ResourceLogs) ([]byte, error) {
	if rl == nil {
		return nil, fmt.Errorf("canonical: nil ResourceLogs")
	}

	// Work on a shallow clone to avoid mutating the caller's data.
	clone := proto.Clone(rl).(*otlplogspb.ResourceLogs)

	// Step 1 & 2: strip signing attributes and sort Resource.attributes.
	if clone.Resource != nil {
		clone.Resource.Attributes = stripAndSort(clone.Resource.Attributes)
	}

	// Step 3: sort each LogRecord's attributes.
	for _, sl := range clone.ScopeLogs {
		for _, lr := range sl.LogRecords {
			lr.Attributes = sortAttributes(lr.Attributes)
		}
	}

	// Step 6: deterministic proto marshal.
	opts := proto.MarshalOptions{Deterministic: true}
	out, err := opts.Marshal(clone)
	if err != nil {
		return nil, fmt.Errorf("canonical: marshal ResourceLogs: %w", err)
	}
	return out, nil
}

// stripAndSort removes the two signing attribute keys and returns the
// remaining attributes sorted lexicographically by key.
func stripAndSort(attrs []*otlpcommonpb.KeyValue) []*otlpcommonpb.KeyValue {
	filtered := attrs[:0:0] // reuse backing array type, fresh slice
	for _, kv := range attrs {
		if _, skip := canonicalStripKeys[kv.Key]; !skip {
			filtered = append(filtered, kv)
		}
	}
	return sortAttributes(filtered)
}

// sortAttributes sorts a slice of KeyValue pairs in-place by key (ascending).
func sortAttributes(attrs []*otlpcommonpb.KeyValue) []*otlpcommonpb.KeyValue {
	sort.Slice(attrs, func(i, j int) bool {
		return attrs[i].Key < attrs[j].Key
	})
	return attrs
}
