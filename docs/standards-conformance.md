# Standards and specifications

What NovaFabric implements, how to verify each claim yourself, and — kept deliberately
prominent — **what is not claimed**.

This page exists because "supports EU AI Act" is not a checkable statement. A standards
engineer, an auditor, or a reviewer needs to know *which artefact*, *produced by which
command*, *against which version of which specification*, and *how mature that
implementation is*. Everything below is stated in those terms.

> **The distinction this whole page rests on.** NovaFabric produces **evidence** and
> **conformance receipts**. It does **not** issue verdicts, certificates, or attestations
> of compliance, and no software can. A receipt records that a stated check ran against
> stated inputs and what it observed. Deciding whether that satisfies an obligation is a
> judgement made by a qualified human — an auditor, a notified body, a regulator, a DPO.
>
> The codebase enforces this distinction rather than merely promising it; see
> [Assurance cases §4, "Conformance receipts — never verdicts"](assurance-cases.md).

**Maturity labels** are the project's standard ones and are not rounded up:
**works today** (in `main`, tests pass) · **experimental** (implemented, interface may
change) · **planned** · **future design**. See [Project status](https://github.com/MSKazemi/novafabric/wiki/Project-Status).

---

## 1. Evidence, signing, and provenance formats

These are the load-bearing ones: they define the artefact a third party verifies, and they
are the part of the system that must keep working offline in five years.

| Specification | Version / profile | What NovaFabric does | Verify it yourself | Maturity |
|---|---|---|---|---|
| **in-toto Attestation / DSSE** | DSSE envelope, ed25519 | Signs Evidence Bundles and capsule digests as DSSE envelopes | `nova seal verify <run-id>` · `nova verify-envelope <file>` | works today |
| **RFC 3161** | Time-Stamp Protocol | Anchors each seal with a trusted timestamp; CRL caching so verification works air-gapped | `nova seal verify <run-id>` | works today |
| **RFC 8785** | JSON Canonicalization Scheme | Canonical JSON for digest computation, so a digest is reproducible across implementations | inspect `proposal_digest` in a sealed capsule | works today |
| **JSON Schema** | draft 2020-12 | `schemas/run-capsule.schema.json` and siblings define every artefact the system emits | `nova validate <run-id>` — or validate the schema with any 2020-12 validator | works today |
| **Merkle inclusion proofs** | RFC 6962-style tree | Evidence Provenance tree over capsule hashes | `nova merkle-tree <run-id>` | works today |
| **SLSA** | provenance for released artefacts | Build provenance and signed release artefacts in the publish pipeline | release attestations on the GitHub release | works today |
| **CycloneDX** | 1.7, AI/ML profile | AI Bill of Materials generated from capsule facts | `nova export-aibom <run-id>` | experimental |
| **C2PA** | content credentials | Provenance manifest for generated media | `nova export-c2pa <run-id>` | experimental |

**Why this section matters most.** An Evidence Bundle is designed to be verifiable with
`sha256sum` and an ed25519 verifier — **no NovaFabric installation required**. If you can
only check our evidence with our tool, it is not evidence. That constraint is what forced
the choices above toward boring, widely-implemented formats.

## 2. Telemetry and lineage interoperability

NovaFabric records against open conventions rather than a proprietary shape, so capsule
data can leave the system.

| Specification | Version / profile | What NovaFabric does | Verify it yourself | Maturity |
|---|---|---|---|---|
| **OpenTelemetry** | traces, GenAI semantic conventions | Model calls and spans are recorded against the GenAI semconv; `trace.jsonl` is OTel-shaped | inspect `model-calls.jsonl` / `trace.jsonl` in any capsule | works today |
| **OpenLineage** | run/job/dataset events | Emits lineage as OpenLineage events for ingestion by Marquez and others | `nova lineage emit-openlineage` | works today |
| **W3C PROV** | PROV-O (RDF) | Exports the provenance graph as PROV-O triples | `nova lineage export-prov` | experimental |
| **SHACL** | shape validation | Validates provenance triples on ingest into the knowledge graph | `novafabric[spkg]` extra | experimental |
| **openCypher** | query | Graph queries over the capsule knowledge graph | `nova kg query` | experimental |
| **Model Context Protocol** | MCP tool exchanges | Transparent MCP proxy records tool calls; a conformance suite runs in CI | `nova mcp conformance` · `.github/workflows/mcp-conformance.yml` | experimental |

## 3. Regulatory and management-system frameworks

**Read the caveat before the table.** Everything in this section produces a *document or
receipt derived from captured run facts*. It is an input to a compliance process, never an
output of one. NovaFabric cannot and does not determine whether you comply with any of
these instruments.

| Framework | What NovaFabric produces | Command | Maturity |
|---|---|---|---|
| **EU AI Act** | Annex IV technical-documentation export; risk-tier classification | `nova export-annex-iv` · `nova classify run` | experimental |
| **NIST AI RMF 1.0** | Risk report mapped from capsule facts | `nova export-nist-rmf` | experimental |
| **ISO/IEC 42001** | AI management-system evidence export; examiner package | `nova export-compliance iso42001` · `nova export-examiner iso42001` | experimental |
| **GDPR** | Art. 30 Records of Processing; Art. 17 erasure via DEK crypto-shredding; DSAR assembly | `nova export-ropa` · `nova pii erase` · `nova dsar assemble` | experimental |
| **NIS2** | Incident report export | `nova export-nis2` | experimental |
| **HIPAA** | Safe Harbor de-identification proof | `nova export-hipaa-proof` | experimental |
| **OMB M-24-10** | Risk-tier vocabulary in classification | `nova classify list-vocabularies` | experimental |

### Records-retention (WORM) conformance

A separate, installable behavioural test suite that verifies an **object-storage backend**
meets write-once-read-many requirements. It tests the storage, not your compliance.

Ten mandatory cases covering immutability, retention locks, legal holds, and lifecycle
exemptions, referenced against **SEC 17a-4**, **MiFID II**, **CFTC 1.31** and
**FINRA 4370**. Backends: S3-compatible, Azure Blob, GCS.

```bash
pip install "git+https://github.com/MSKazemi/novafabric#subdirectory=packages/nova_worm_conformance"
nova-worm-conformance run --backend s3 --bucket my-capsule-store
```

Also reachable in-tree as `nova storage validate`. **Maturity: experimental.**

## 4. Identity and access (server mode only)

| Specification | What NovaFabric does | Maturity |
|---|---|---|
| **OIDC** | Token verification for the multi-user API | experimental |
| **SAML 2.0** | SSO assertion verification, XML-DSIG with XML-Signature-Wrapping resistance (`novafabric[saml]`) | experimental |
| **SCIM 2.0** | User/group provisioning events | experimental |

None of these are required for local mode, which has no accounts at all.

---

## What is explicitly **not** claimed

Stated plainly, because a standards page that only lists strengths is not useful to anyone
evaluating the project.

1. **No certification, of anything, by anyone.** NovaFabric is not certified, accredited,
   or endorsed under any framework listed here. Nothing on this page has been assessed by a
   notified body, an accredited certification body, or a regulator.

2. **Producing an Annex IV export is not EU AI Act compliance.** The export is a document
   assembled from what a run actually did. Whether your system is high-risk, whether the
   documentation is adequate, and whether your obligations are met are determinations for
   qualified people. The same applies to every row in §3.

3. **Conformance receipts are observations, not verdicts.** A receipt says "this check ran,
   against these inputs, and observed this". It does not say "you pass". This is a
   deliberate architectural constraint, not a disclaimer bolted on afterwards.

4. **No byte-exact replay of remote LLM calls.** `exact` mode requires a deterministic
   environment and a per-call seed — realistic for a local or on-prem model, not for a
   hosted endpoint that can change under you. For drifting remote models, `semantic` mode
   scores similarity of meaning on a 0.0–1.0 scale. Any claim of "deterministic replay" for
   hosted models should be read carefully, including ours.

5. **Capsule and Evidence Bundle formats are not frozen.** They change until the v1.0
   freeze — additively, with old capsules staying readable, but they move. If you are
   building a long-lived integration against the schema, that is the milestone to watch.

6. **Most of §3 is `experimental`.** Implemented and tested, interface may change. Do not
   build a submission pipeline on an experimental exporter without pinning a version.

7. **Standards coverage is not uniform.** Implementing a *format* (DSSE, CycloneDX) is a
   much stronger claim than mapping to a *framework* (ISO 42001). §1 is where the rigour
   is; §3 is best understood as structured evidence assembly.

---

## For standards bodies and working groups

If you maintain or contribute to any specification above, three things may be useful, and
all three are things we would rather hear about than guess at:

**1. Implementation feedback.** NovaFabric is a real implementation of DSSE + RFC 3161 +
in-toto in an AI-workload context, of the OTel GenAI conventions, and of OpenLineage. Where
a specification was ambiguous, under-specified for AI workloads, or expensive to implement
correctly, that is recorded in the [ADR ledger](decisions.md) — those are exactly the
findings an editor usually has to extract from implementers by hand.

**2. Conformance suites you can run.** `nova-worm-conformance` (object-storage WORM) and
`nova mcp conformance` are executable, not prose. If your group wants an independent
implementation to test a draft against, these are available under Apache-2.0.

**3. Corrections.** If any row above misstates your specification — wrong version, wrong
clause, an overstated claim, a profile we do not actually meet — that is a **bug**, and one
we want reported. Open an issue with the `documentation` label, or start a
[Discussion](https://github.com/MSKazemi/novafabric/discussions). A correction from a
specification's own maintainers is the most valuable kind of contribution this page can
receive, and it will be credited in the release notes.

**Contact:** [open a Discussion](https://github.com/MSKazemi/novafabric/discussions) or see
[SUPPORT.md](../SUPPORT.md). For anything security-sensitive use the private reporting path
in [SECURITY.md](../SECURITY.md).

## See also

- [Assurance cases](assurance-cases.md) — the receipts-not-verdicts model in detail
- [Concepts](concepts.md) — what a capsule contains and why
- [Benchmarks](benchmarks.md) — measured overhead, every number with its command and hardware
- [Architecture decisions](decisions.md) — the reasoning ledger
- [Trust surfaces](trust-surfaces.md) — what is signed, by whom, and what that proves
