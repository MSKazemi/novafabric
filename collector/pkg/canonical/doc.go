// Package canonical implements the NovaFabric canonical encoding algorithm
// (ADR-001) for deterministic serialization of OTLP ResourceLogs prior to
// batch signing by NovaSeal.
//
// # Canonical Encoding Algorithm (ADR-001)
//
// The algorithm produces a stable byte sequence from an OTLP ResourceLogs
// message so that two logically identical payloads always hash and sign
// identically, regardless of attribute insertion order or protobuf field
// serialisation quirks.
//
// Steps, applied in order:
//
//  1. Strip "nova.batch.signature" and "nova.batch.signing_key_id" from
//     ResourceLogs.Resource.attributes. These fields are written by NovaSeal
//     itself and must be absent from the digest input to avoid a circular
//     dependency.
//
//  2. Sort the remaining Resource.attributes by key (lexicographic ascending,
//     UTF-8 byte order).
//
//  3. Sort each LogRecord.attributes by key (lexicographic ascending,
//     UTF-8 byte order).  All ScopeLog groups are visited.
//
//  4. Preserve LogRecord list order within each ScopeLog. Do NOT re-sort
//     log records — temporal ordering carries semantic meaning for replayers
//     and auditors.
//
//  5. Preserve unknown fields in all messages so that future OTLP versions
//     can be signed without a collector upgrade.
//
//  6. Marshal the modified proto with proto.MarshalOptions{Deterministic: true}.
//     This eliminates map-key nondeterminism in protobuf itself.
//
// The canonical bytes are passed to [novaseal.Signer.Sign]. The signature
// and key ID are written back into Resource.attributes under
// "nova.batch.signature" and "nova.batch.signing_key_id". Verifiers strip
// those two attributes and re-run the algorithm before verifying the signature.
package canonical
