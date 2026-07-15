# Feature schema fragments (`schemas/features/`)

**Status: planned / future design — nothing here is shipped.**

These are **additive, optional, v0 (not frozen)** JSON Schema fragments for the new
capsule/evidence blocks introduced by the 100-feature program. They are standalone
fragments, referenced by a capsule or Evidence Bundle through the existing
`extensions/` (or additive `facets`) surfaces. They are **not** wired into
`run-capsule.schema.json` or `environment.schema.json`, and they add **no required
fields** to any existing structure — a capsule produced before any of these features
exists must still validate unchanged.

All fragments follow the conventions of the shipped schemas
(`schemas/run-capsule.schema.json`, `schemas/environment.schema.json`): JSON Schema
draft 2020-12, `$id` under `https://novafabric.io/schemas/features/…`,
`additionalProperties: false` on defined objects plus a reverse-DNS `extensions`
object for forward-compatibility, `sha256:<hex>` content-hash pattern, and RFC 3339
date-times. Each carries an `x-novafabric` block pointing to its governing NF-id, ADR,
and spec, and states the additive/optional/v0-not-frozen contract.

## Files

| File | NF-id(s) | Governing ADR | Spec | Docs-honesty label |
|---|---|---|---|---|
| `eval-card-v0.schema.json` | NF-002 (with NF-010) | ADR-0099 | `design/spec/features/NF-002-010-signed-eval-cards.md` | **superseded** → implemented as `schemas/eval-card-v1.schema.json` (`experimental`); this v0 draft retained for history |
| `determinism-facet-v0.schema.json` | NF-012 / NF-013 / NF-014 | ADR-0100 | `design/spec/features/NF-012-014-determinism-attestation.md` | future design |
| `translog-checkpoint-v0.schema.json` | NF-042 (transparency log NF-041..050) | ADR-0097 | `design/spec/features/NF-041-047-verifiable-transparency-log.md` | planned |
| `delegation-chain-v0.schema.json` | NF-084 (identity layer NF-083..089) | ADR-0106 | `design/spec/features/NF-083-089-agent-identity-delegation.md` | future design |
| `dataset-provenance-card-v0.schema.json` | NF-058 (with NF-055 / NF-057) | ADR-0105 | `design/spec/features/NF-055-057-058-supply-chain.md` | planned |

## Contract (applies to every fragment)

- **Additive.** Referenced from `extensions/` or additive `facets`; never a new required field.
- **Optional.** Absence of the block leaves the capsule/bundle exactly as valid as today.
- **v0, not frozen.** Field names and shapes may change until the governing ADR's spikes
  pass and the block is promoted to a `v1` schema. Do not treat as a stable interface.
- **Secrets boundary.** The identity and dataset fragments record public identifiers,
  scopes, expiries, and digests only — never bearer tokens, signing keys, raw prompts,
  or cell values (ADR-0009 / ADR-0021).
- **Docs honesty.** Do not document any of these as implemented. See each spec's honesty
  label above and the project "Docs honesty rule".
