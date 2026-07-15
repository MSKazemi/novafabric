# `novafabric.evidence_fabric`

**Evidence Fabric v1.0 accumulator stack** (ADR-0066): the self-contained and
scale-tier primitives for accumulating evidence events — DuckDB / ClickHouse
accumulators, NATS-compatible queue consumers, a Parquet-backed PII table, Avro
serialization, and rebuild logic.

**Not to be confused with [`novafabric.evidence`](../evidence/) — single
Evidence Bundle assembly (ADR-0011).** `evidence_fabric` = the streaming
accumulator; `evidence` = one signed bundle.
