# `novafabric.envelopes`

**Cryptographic attestation envelopes**: DSSE, in-toto, and SLSA provenance
(`dsse.py`, `intoto.py`, `slsa.py`, `schema.py`). These wrap and sign
attestations about builds/runs.

**Not to be confused with [`novafabric.envelope`](../envelope/) (singular) —
the EventEnvelope v1 event wire format.** `envelopes` = signed attestations;
`envelope` = the per-event message format.
