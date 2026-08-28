# `novafabric.envelope`

**EventEnvelope v1** — the canonical wire format for a single NovaFabric
evidence *event*. Spec: the private `design/architecture/event-envelope-v1.md`; schema is
SHA-256 pinned. Provides `EventEnvelope`, `EventType`, `validate_event`, and
CloudEvents interop.

**Not to be confused with [`novafabric.envelopes`](../envelopes/) — the
cryptographic attestation envelopes** (DSSE / in-toto / SLSA). `envelope` =
event wire format; `envelopes` = signed attestation wrappers.
