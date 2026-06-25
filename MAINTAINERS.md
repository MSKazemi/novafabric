# Maintainers

This file lists the people responsible for NovaFabric. The path to becoming
a maintainer is documented in
[`design/governance/MAINTAINER_CRITERIA.md`](design/governance/MAINTAINER_CRITERIA.md).

The decision-making process is documented in [`GOVERNANCE.md`](GOVERNANCE.md).

---

## BDFL / Project lead

**[Mohsen Seyedkazemi Ardebili](https://github.com/MSKazemi)** —
AI systems engineer, platform architect, HPC researcher. Founder and
current BDFL of NovaFabric.

Areas of competence: all (founder).

The BDFL phase ends when at least 2 external co-maintainers have demonstrated
sustained merge-quality work. See
[`design/strategy/foundation-ready-governance.md`](design/strategy/foundation-ready-governance.md).

---

## Maintainers

*None yet.* External maintainer recruitment begins in v0.3 per the roadmap.

When the first external maintainer joins, they are listed here with:

```
**Name** — github-handle
Areas of competence: <area>, <area>
Joined: YYYY-MM-DD
```

---

## Stewards

*None yet.* Stewards are recognized contributors without merge rights but
with vote weight in RFCs as community sponsors.

When the first steward is recognized, they are listed here with:

```
**Name** — github-handle
Areas: <area>
Recognized: YYYY-MM-DD
```

---

## Emeritus

*None yet.* Maintainers and stewards who step back retain credit here.

---

## Areas of competence

Each maintainer self-declares one or more areas. Reviewing PRs outside one's
declared areas requires a co-review from someone in that area.

| Area | Code paths | Responsibilities |
|---|---|---|
| **Asset Registry** | `src/novafabric/spec/`, `src/novafabric/registry/` | Schemas, validators, store, lifecycle state machine |
| **Capture & Replay** | `src/novafabric/capture/`, `src/novafabric/replay/` (when added) | Run capsule capture, replay engine, diff |
| **Lineage & Evidence** | `src/novafabric/lineage/`, `src/novafabric/evidence/` (when added) | Graph storage, signing, attestation, redaction |
| **CLI** | `src/novafabric/cli/` | Command surface, output formatting, error messages |
| **Adapters & Integrations** | `src/novafabric/adapters/`, integration packages | OpenLineage, OTel, MCP, Sigstore, MLflow, Langfuse, etc. |
| **Documentation & Spec** | `docs/`, `schemas/`, RFCs | Vision, governance, open specs, JSON schemas |

---

## Maintainer count targets

| Phase | Target |
|---|---|
| v0.2 → v0.3 (BDFL) | 1 maintainer + 1-2 stewards |
| v0.3 → v0.5 (recruiting) | 1 + 2 external = 3 maintainers; 3-5 stewards |
| v0.5 → v0.7 (foundation-ready) | 3-4 maintainers; 5-7 stewards |
| v0.7+ (TSC) | TSC of 3-5 + maintainer pool of 5-10; 10+ stewards |

These are targets, not quotas. Quality bar does not move.

---

## Contact

For project decisions, open an issue or RFC.

For Code of Conduct concerns, see [`SECURITY.md`](SECURITY.md) /
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) for the relevant contact path.

For Design Partner inquiries, see [`design/governance/DESIGN_PARTNERS.md`](design/governance/DESIGN_PARTNERS.md).
