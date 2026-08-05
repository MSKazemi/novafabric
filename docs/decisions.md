<!-- GENERATED FILE — do not edit by hand.
     Regenerate with: uv run python scripts/gen_decisions_index.py -->

# Architecture decisions

NovaFabric records every architectural decision as a numbered **ADR**
(Architecture Decision Record). This page is the canonical index: whenever the
documentation cites `ADR-0123`, this table is what it refers to.

**Why only an index?** ADR bodies are internal design records — they carry
in-progress research, competitive analysis, and commercial reasoning alongside
the technical decision, so the project publishes the decision *ledger* rather
than the deliberation. Everything an ADR decides that affects you as a user or
contributor is reflected in the code, the [CHANGELOG](../CHANGELOG.md), the
[ROADMAP](../ROADMAP.md), and the docs in this directory. If a decision here
matters to something you are building and the public docs do not explain it,
[open a Discussion](https://github.com/novafabric/novafabric/discussions) and
ask — we will write it up.

**Proposing a change to a decision** is the [RFC
process](governance/rfc-process.md), not an ADR. RFCs are public and live in
[`docs/rfcs/`](rfcs/).

| Status | Meaning |
|---|---|
| **accepted** | Decided and in force. |
| **proposed** | Written, not yet decided. |
| **superseded** | Replaced by a later ADR. |
| **rejected** | Considered and declined; kept as provenance. |


**225 decisions recorded** — **215** accepted · **7** proposed · **3** superseded.

| ADR | Title | Status | Date |
|---|---|---|---|
| `ADR-0001` | Local first registry | accepted | 2026-05-06 |
| `ADR-0002` | Ai asset specification | accepted | 2026-05-06 |
| `ADR-0003` | Eval gated agent promotion | accepted | 2026-05-06 |
| `ADR-0004` | Capsule layout | accepted | 2026-05-07 |
| `ADR-0005` | Replay modes | accepted | 2026-05-07 |
| `ADR-0006` | Capture mechanism | accepted | 2026-05-07 |
| `ADR-0007` | Environment lock | accepted | 2026-05-07 |
| `ADR-0008` | Tool call serialization | accepted | 2026-05-07 |
| `ADR-0009` | Secret scanning | accepted | 2026-05-07 |
| `ADR-0010` | External resource handling | accepted | 2026-05-07 |
| `ADR-0011` | Evidence bundle | accepted | 2026-05-07 |
| `ADR-0012` | Replay safety | accepted | 2026-05-07 |
| `ADR-0013` | Lineage graph storage | accepted | 2026-05-07 |
| `ADR-0014` | Otel genai semconv | accepted | 2026-05-07 |
| `ADR-0015` | Mcp integration | accepted | 2026-05-07 |
| `ADR-0016` | Storage backend evolution | accepted | 2026-05-07 |
| `ADR-0017` | Server api protocol | accepted | 2026-05-07 |
| `ADR-0018` | Auth model | accepted | 2026-05-07 |
| `ADR-0019` | Policy engine | accepted | 2026-05-07 |
| `ADR-0020` | Cluster scale low overhead capture | accepted | 2026-05-08 |
| `ADR-0021` | Ai factory design intent | proposed | 2026-05-08 |
| `ADR-0022` | Polyglot persistence and object storage | accepted | 2026-05-08 |
| `ADR-0023` | Cache architecture | proposed | 2026-05-08 |
| `ADR-0024` | Dependency license policy | accepted | 2026-05-08 |
| `ADR-0025` | Runner spec interface | accepted | — |
| `ADR-0026` | Api proxy promotion | accepted | — |
| `ADR-0027` | Nova serve experimental dashboard | accepted | 2026-05-10 |
| `ADR-0028` | Itwinai interlink integration | proposed | — |
| `ADR-0029` | Server config schema | accepted | — |
| `ADR-0030` | Rfc3161 trusted timestamps | accepted | 2026-05-10 |
| `ADR-0031` | Worm retention policy | accepted | 2026-05-10 |
| `ADR-0032` | Parent child capsule | superseded | 2026-05-10 |
| `ADR-0033` | Eval runner design | accepted | 2026-05-11 |
| `ADR-0034` | V1 spec stability policy | accepted | 2026-05-11 |
| `ADR-0035` | Agent Identity Attribution in Capsule Records | accepted | 2026-05-11 |
| `ADR-0036` | Cross-Run Comparison UX in the Experimental Dashboard | accepted | 2026-05-12 |
| `ADR-0037` | Evidence Tab Native Dashboard Implementation | accepted | 2026-05-12 |
| `ADR-0038` | Eval Trend Chart in the Registry Dashboard Tab | accepted | 2026-05-12 |
| `ADR-0039` | Parent/Child Capsule Hierarchy v2 | superseded | 2026-05-12 |
| `ADR-0040` | Production MetadataStore Interface | superseded | 2026-05-12 |
| `ADR-0041` | NovaSeal Cryptographic Core Adoption | accepted | 2026-05-12 |
| `ADR-0042` | Deployment tier invariants | accepted | 2026-05-12 |
| `ADR-0043` | Collector v01 implementation | accepted | 2026-05-12 |
| `ADR-0044` | Typed lineage edge vocabulary | accepted | 2026-05-12 |
| `ADR-0045` | Fail open out of order arrival | accepted | 2026-05-12 |
| `ADR-0046` | Two phase capsule lifecycle | accepted | 2026-05-12 |
| `ADR-0047` | Capsule manifest chain | accepted | 2026-05-12 |
| `ADR-0048` | Novaseal sync write contract | accepted | 2026-05-12 |
| `ADR-0049` | Worm conformance suite | accepted | 2026-05-12 |
| `ADR-0050` | MetadataStore ABC with SET LOCAL tenant isolation | accepted | 2026-05-13 |
| `ADR-0051` | Partition key for the `runs` table | accepted | 2026-05-13 |
| `ADR-0052` | pgBouncer transaction mode + two-role split for MetadataStore | accepted | 2026-05-13 |
| `ADR-0053` | LineageStore v2 Tiering: Benchmark-Gated Backend Progression | accepted | 2026-05-13 |
| `ADR-0054` | DSSE Signing Envelope | accepted | 2026-05-14 |
| `ADR-0055` | Dual-Mode Signing Identity | accepted | 2026-05-14 |
| `ADR-0056` | Rules-Based (Not ML) Risk-Tier Classifier | accepted | 2026-05-14 |
| `ADR-0057` | Per-Tenant Append-Only Merkle Log | accepted | 2026-05-14 |
| `ADR-0058` | Maker-Checker Dual-Approval for Asset Promotion | accepted | 2026-05-15 |
| `ADR-0059` | NovaSeal Linked-Envelope Chain Maker-Checker Signing | accepted | 2026-05-15 |
| `ADR-0060` | Role-management HTTP surface | accepted | 2026-05-15 |
| `ADR-0061` | NATS JetStream as Cluster-Tier Event Bus | accepted | 2026-05-17 |
| `ADR-0062` | Dual-Object GDPR/WORM Capsule Storage | accepted | 2026-05-17 |
| `ADR-0063` | Microsoft Presidio as Default PII Detector | accepted | 2026-05-17 |
| `ADR-0064` | JSON-LD as Primary Evidence Export Format | accepted | 2026-05-17 |
| `ADR-0065` | tool-permission-event as First-Class Capsule Entity | accepted | 2026-05-17 |
| `ADR-0066` | Evidence Fabric v1.0 Core Pipeline | accepted | 2026-05-17 |
| `ADR-0067` | Capsule Knowledge Graph v1.2 | accepted | 2026-05-17 |
| `ADR-0068` | TV-5 3D Topology View | accepted | 2026-05-17 |
| `ADR-0069` | GDPR Art.17 Erasure via AES-256-GCM Crypto-Shredding for Immutable Capsule Logs | accepted | 2026-05-19 |
| `ADR-0070` | RFC 3161 TSA Trust Chain Validation with CRL Caching for Air-Gapped HPC Environments | accepted | 2026-05-19 |
| `ADR-0071` | Sigstore Keyless Signing Integration via sigstore Python SDK 4.x | accepted | 2026-05-19 |
| `ADR-0072` | Post-Quantum Cryptography Migration Roadmap for NovaSeal (2026–2035) | accepted | 2026-05-19 |
| `ADR-0073` | AI Bill of Materials Export Using CycloneDX ML-BOM v1.7 (cap-008) | accepted | 2026-05-19 |
| `ADR-0074` | C2PA Content Credentials for AI-Generated Artifact Provenance (EU AI Act Art.50) | accepted | 2026-05-19 |
| `ADR-0075` | W3C DID + Verifiable Credentials for Agentic AI Identity and Authorization Chains | accepted | 2026-05-19 |
| `ADR-0076` | EU AI Act Article 12 Compliance Mode for High-Risk AI Logging | accepted | 2026-05-19 |
| `ADR-0077` | Multi-Region Log Sovereignty for Jurisdictional Data Residency | accepted | 2026-05-19 |
| `ADR-0078` | Ecosystem Framework Adapters: OpenAI Agents SDK, Google ADK, Bedrock AgentCore, A2A | accepted | 2026-05-19 |
| `ADR-0079` | Production Capsule Storage: Hybrid Three-Tier Model (Filesystem → OCS → MetadataStore) | accepted | 2026-05-20 |
| `ADR-0080` | Statistical significance eval gate | accepted | 2026-06-11 |
| `ADR-0081` | CloudEvents v1.0 Envelope Interop (structured-mode round-trip) | accepted | 2026-06-11 |
| `ADR-0082` | Extended Span Taxonomy (gap-011) | accepted | 2026-06-11 |
| `ADR-0083` | Hot lineage impact index | accepted | 2026-06-11 |
| `ADR-0084` | Failure attribution | accepted | — |
| `ADR-0085` | Sealed system card and eval pinning | accepted | — |
| `ADR-0086` | Intervention replay | accepted | 2026-06-12 |
| `ADR-0087` | Evidence completeness binding attestation | accepted | 2026-06-12 |
| `ADR-0088` | Incident deadline clock | accepted | 2026-06-12 |
| `ADR-0089` | Forward-Secure Per-Node Signing Key Ratchet | accepted | 2026-06-12 |
| `ADR-0090` | Fine grained verifiable lineage | accepted | 2026-06-12 |
| `ADR-0091` | Ebpf agentless capture | proposed | 2026-06-12 |
| `ADR-0092` | Warm capture daemon | accepted | 2026-06-14 |
| `ADR-0093` | Energy-Anchored Action Receipts | accepted | 2026-06-19 |
| `ADR-0094` | Adversary-Anchored Accountability Ledger + Deterministic Replay Attestation | accepted | 2026-06-19 |
| `ADR-0095` | Evidence-Grounded Safety-Case Compiler + Court-Admissible Evidence Binding | accepted | 2026-06-19 |
| `ADR-0096` | Standard outer envelopes — DSSE / in-toto / SLSA / CloudEvents wrapping for NovaFabric evidence | accepted | 2026-07-01 |
| `ADR-0097` | Verifiable, witness-cosigned transparency log for capsules, evidence and policy decisions | accepted | 2026-07-01 |
| `ADR-0098` | OTel GenAI as canonical span vocabulary — agent-span emitter, content-capture bridge, OTLP/OpenInference ingestion | accepted | 2026-07-01 |
| `ADR-0099` | Evidence-grade evaluation — signed, replayable scores bound to content-addressed spans | accepted | 2026-07-01 |
| `ADR-0100` | Determinism replay attestation — batch-invariant, bitwise-provable exact replay | accepted | 2026-07-01 |
| `ADR-0101` | Intervention-verified counterfactual attribution — replay-confirmed, not log-only, root cause | accepted | 2026-07-01 |
| `ADR-0102` | Cluster-scale collector v2 — OTAP-native, two-tier, JetStream-durable ingestion plane | proposed | 2026-07-01 |
| `ADR-0103` | Object capsule store — Iceberg-v3-style object-store-as-source-of-truth with DuckLake/DuckDB index and Lance payload tier | proposed | 2026-07-01 |
| `ADR-0104` | eBPF agentless capture tier v2 — promote ADR-0091 to proposed with gating spikes (OBI/Beyla, cross-process propagation) | proposed | 2026-07-01 |
| `ADR-0105` | AI supply-chain bill-of-materials: model signing, CycloneDX AI-BOM, SLSA-for-ML, dataset provenance | accepted | 2026-07-01 |
| `ADR-0106` | Agent identity binding and provable delegated authority | accepted | 2026-07-01 |
| `ADR-0107` | EU AI Act and standards evidence exporters | accepted | 2026-07-01 |
| `ADR-0108` | Eval-harness interop — be the provenance/seal layer under Inspect AI, HAL, τ²-bench, and Terminal-Bench 2.0 | accepted | 2026-07-01 |
| `ADR-0109` | Fine-grained lineage v2 — cell/row facets, transformation provenance, hot index | accepted | 2026-07-01 |
| `ADR-0110` | Cross-node interaction proofs and Slurm-native provenance consolidation | accepted | 2026-07-01 |
| `ADR-0111` | Security & Provenance Knowledge Graph (SPKG) — graph-based anomaly detection and threat reasoning over run capsules, lineage, evidence, identity, and AI-BOM | accepted | 2026-07-02 |
| `ADR-0112` | Prompt as a first-class versioned, content-addressed registry asset | accepted | 2026-07-12 |
| `ADR-0113` | Asset deployment labels — movable named pointers to immutable asset versions, resolution-frozen into the capsule | accepted | 2026-07-12 |
| `ADR-0114` | Protected labels — critical deployment-label moves require maker-checker approval | accepted | 2026-07-12 |
| `ADR-0115` | Prompt composability with resolved-at-capture snapshot — content-addressed composition manifests for deterministic replay | accepted | 2026-07-12 |
| `ADR-0116` | Variant attribution — record which A/B experiment variant was active for a run (record-only, never allocate) | accepted | 2026-07-12 |
| `ADR-0117` | Score configuration catalog — named, reusable, content-addressed score definitions that constrain and make evidence-grade scores comparable across capsules | accepted | 2026-07-12 |
| `ADR-0118` | Human annotation queues — a maker-checker workflow layer that routes subjects to reviewers and emits each completed annotation as a signed HUMAN-source Score | accepted | 2026-07-12 |
| `ADR-0119` | External score-submission API/SDK — a documented, validated write surface for ingesting externally-computed evaluation scores into a capsule's append-only scores.jsonl | accepted | 2026-07-12 |
| `ADR-0120` | Dataset-experiment harness — per-item dataset runs, immutable comparison records, and A/B experiment diffs feeding the existing regression gate | accepted | 2026-07-12 |
| `ADR-0121` | Append-only comments/notes on capsules & assets — portable evidence annotations, not live chat | accepted | 2026-07-12 |
| `ADR-0122` | Session capsule — a content-addressed manifest grouping N independent runs into one multi-turn conversation or workflow session | accepted | 2026-07-12 |
| `ADR-0123` | Session replay — deterministic end-to-end re-execution of a multi-turn session over its member capsules | accepted | 2026-07-12 |
| `ADR-0124` | Agent execution-graph reconstruction — a deterministic within-run DAG projection over one capsule's spans, model calls, and tool calls | accepted | 2026-07-12 |
| `ADR-0125` | Multi-modal capture — content-addressed image/audio/video/document content on model calls | accepted | 2026-07-12 |
| `ADR-0126` | Deployment-environment field on run capsules — a first-class, queryable production/staging/dev tag distinct from the environment lock | accepted | 2026-07-12 |
| `ADR-0127` | Log levels on observations — a stored, filterable severity dimension for forensic inspection of captured runs | accepted | 2026-07-12 |
| `ADR-0128` | Tool-call & structured-output schema validation — an enforcing validation pass over declared schema_refs at capture and replay | accepted | 2026-07-12 |
| `ADR-0129` | Offline metrics query DSL over the local capsule store — a local-first, CLI-native way to filter/group/aggregate metrics across many capsules without a server | accepted | 2026-07-12 |
| `ADR-0130` | Saved views / saved queries — named, versioned, portable query definitions over capsule sets | accepted | 2026-07-12 |
| `ADR-0131` | Offline CLI score/cost/latency trend reports (nova trend) — JSON series + optional self-contained static HTML | accepted | 2026-07-12 |
| `ADR-0132` | Rich token usage-type accounting — an additive, extensible usage breakdown (cached / reasoning / audio / image tokens) on the model-call record | accepted | 2026-07-12 |
| `ADR-0133` | Local, user-extensible model-pricing catalog for offline cost accounting (self-hosted / fine-tuned / private models) | accepted | 2026-07-12 |
| `ADR-0134` | Data-retention policy scheduler — a WORM-aware, crypto-shred-integrated, audited sweep that applies retention windows over time | accepted | 2026-07-12 |
| `ADR-0135` | Pluggable PII masking pipeline at capture ingestion (operator-registered maskers, recorded in the redaction proof) | accepted | 2026-07-12 |
| `ADR-0136` | Cost/energy budget policy gate — a Rego promotion gate that blocks acceptance of a capsule or asset whose recorded cost/energy exceeds a declared budget ceiling | accepted | 2026-07-12 |
| `ADR-0137` | Lifecycle event emitter & webhooks — a structured, append-only event surface that lets external CI/automation react to capsule/promotion/policy/retention transitions | accepted | 2026-07-12 |
| `ADR-0138` | SAML 2.0 SSO for server mode — an optional enterprise auth backend behind the existing auth abstraction (never a local-mode requirement) | accepted | 2026-07-12 |
| `ADR-0139` | SCIM 2.0 provisioning for server-mode user lifecycle (RFC 7643/7644) — automated create/update/deactivate + group→role mapping | accepted | 2026-07-12 |
| `ADR-0140` | Self-contained shareable capsule viewer — single-file offline HTML export (nova export --html) | accepted | 2026-07-12 |
| `ADR-0141` | Batch capsule export to blob storage with a signed, verifiable completeness manifest | accepted | 2026-07-12 |
| `ADR-0142` | Multi-agent trajectory & A2A message/handoff evidence — captured, signed, causally-ordered records of the wire between agents | accepted | 2026-07-12 |
| `ADR-0143` | Memory & context provenance — sealed lineage for what entered the model's context and where it came from | accepted | 2026-07-12 |
| `ADR-0144` | Behavioral-equivalence replay — score whether a replay *passed* behaviorally (goal/trajectory equivalence, drift, non-determinism budget), not whether it re-emitted the same tokens | accepted | 2026-07-12 |
| `ADR-0145` | Runtime guardrail & safety evidence — sealed, verifiable, replayable runtime-safety decision objects | accepted | 2026-07-12 |
| `ADR-0146` | Agent-granularity cost / energy / SLA accountability — attribute the sealed run-total across the agent team and the acted-as chain, and witness quota / breaker / SLA breaches as evidence | accepted | 2026-07-12 |
| `ADR-0147` | Drift & continuous assurance — a standing production loop over sealed capsules that detects output/behavior drift, canary-replays pinned baselines, analyzes model-update impact, and attests it ran | accepted | 2026-07-12 |
| `ADR-0148` | Content-provenance binding, tool-schema-change evidence & computer-use capture — per-artifact C2PA binding, schema-drift replay-impact, and GUI action provenance | accepted | 2026-07-12 |
| `ADR-0149` | 2026 agent-standard interoperability — portable capture & object mapping of A2A / AGNTCY / WebMCP / ACP / OCI / SCITT into the capsule | accepted | 2026-07-12 |
| `ADR-0150` | Human-agent collaboration & accountability — conversation-granular HITL decision-context, consent, dispute, and right-to-explanation evidence | accepted | 2026-07-12 |
| `ADR-0151` | Verifiability frontier — post-quantum, zero-knowledge, TEE-attestation, and selective-disclosure evidence layered on NovaSeal | accepted | 2026-07-12 |
| `ADR-0152` | Training & model-provenance lineage — binding a model's training ancestry into the run capsule | accepted | 2026-07-13 |
| `ADR-0153` | Retrieval-source authority & knowledge provenance — sealed authority/freshness/license/integrity of retrieved sources and citation-to-source verification | accepted | 2026-07-13 |
| `ADR-0154` | Evaluation & benchmark integrity — cryptographic contamination proofs, harness attestation, judge-bias evidence, and leaderboard reproducibility receipts sealed into the capsule | accepted | 2026-07-13 |
| `ADR-0155` | Agent DFIR — forensic timeline, tamper-evident chain-of-custody & investigator-grade export on top of the shipped Incident object | accepted | 2026-07-13 |
| `ADR-0156` | Third-party verifier & evidence-consumer ecosystem — verify, compare, badge, receipt, and share NovaFabric evidence with no NovaFabric install | accepted | 2026-07-13 |
| `ADR-0157` | Edge / on-device / offline agent evidence — constrained-device capture, deferred-seal, and bounded-clock attestation for intermittently-connected agents | accepted | 2026-07-13 |
| `ADR-0158` | Responsible-AI & sustainability reporting — system cards, RAI scorecards, ESG/CSRD packs, and water/e-waste/embodied-carbon provenance over sealed capsules | accepted | 2026-07-13 |
| `ADR-0159` | Finance-sector agentic evidence pack — SR 26-2/SR 11-7 model-risk, MiFID best-execution, trade-decision, suitability, market-abuse, DORA ICT-incident, SEC 17a-4 retention, ECOA adverse-action, and CAT-style event exporters over sealed capsule evidence | accepted | 2026-07-13 |
| `ADR-0160` | Healthcare / life-sciences agentic evidence pack — GxP, SaMD, HIPAA-AoD, CDS-provenance, ALCOA+ exporters over existing capsule evidence | accepted | 2026-07-13 |
| `ADR-0161` | Data governance, privacy & subject-rights at fleet scale — cross-capsule DSAR assembly, erasure orchestration, purpose/minimization/retention/transfer evidence & fleet-wide RoPA | accepted | 2026-07-13 |
| `ADR-0162` | Embodied & cyber-physical agent evidence — binding physical-world action provenance into the run capsule | accepted | 2026-07-13 |
| `ADR-0163` | Agentic commerce & settlement evidence — binding the provenance of the money an agent moved into the run capsule | accepted | 2026-07-13 |
| `ADR-0164` | Scientific reproducibility & research-integrity evidence — binding an agentic-science run's hypothesis→result provenance into the capsule | accepted | 2026-07-13 |
| `ADR-0165` | Evidence longevity & long-term preservation — keeping a sealed capsule verifiable across decades of crypto, schema, format, and storage migration | accepted | 2026-07-13 |
| `ADR-0166` | Assurance cases & continuous-certification evidence — binding sealed capsules into a machine-checkable argument that stays current | accepted | 2026-07-13 |
| `ADR-0167` | Runtime safety & alignment evidence — recording frontier-safety-framework, AI-control, and dangerous-capability governance events as sealed evidence | accepted | 2026-07-13 |
| `ADR-0168` | Federation & cross-org trust-transitivity evidence — recording how trust composes across organization boundaries | accepted | 2026-07-13 |
| `ADR-0169` | Public transparency & societal accountability evidence — exporting the public/civic disclosure layer from sealed capsules | accepted | 2026-07-13 |
| `ADR-0170` | Insurance, liability & actuarial evidence — binding the risk-transfer layer around an agent run into the capsule | accepted | 2026-07-13 |
| `ADR-0171` | Persistent knowledge & organizational-memory governance evidence — recording governance about the long-lived, cross-run, at-rest shared store | accepted | 2026-07-13 |
| `ADR-0172` | Evidence Provenance Merkle Tree visualization — an interactive, verify-on-click proof tree that makes a sealed capsule's cryptographic chain of custody legible | accepted | 2026-07-13 |
| `ADR-0173` | Trust Attestation Radar visualization — a per-capsule radial gauge that summarizes every Trust-Layer guarantee in one glance-readable glyph | accepted | 2026-07-13 |
| `ADR-0174` | Redaction / Secret-Scan X-Ray visualization — a field-structure heat overlay showing what was captured, redacted, scrubbed, or never captured in a capsule | accepted | 2026-07-13 |
| `ADR-0175` | Streaming object listing for the Object Capsule Store (iter_objects) — bounded-memory rebuild over large namespaces | accepted | 2026-07-16 |
| `ADR-0176` | PROV-N text export for lineage — a second W3C serialization of the same provenance graph | accepted | 2026-07-16 |
| `ADR-0177` | OTLP/protobuf trace ingest — binary encoding support that reuses the JSON ingest path | accepted | 2026-07-16 |
| `ADR-0178` | Workspace/organization model and first-class service accounts above flat tenant isolation | accepted | 2026-07-16 |
| `ADR-0179` | API rate limiting and quotas — in-process token bucket, standard 429 contract | accepted | 2026-07-16 |
| `ADR-0180` | High-availability and upgrade posture — single-writer active-passive through v1.0, expand-contract migrations | accepted | 2026-07-16 |
| `ADR-0181` | Backup, restore, and DR tooling — evidence-grade backup sets with keys excluded | accepted | 2026-07-16 |
| `ADR-0182` | Self-observability surface — /metrics, /livez, /readyz, /v0/version, and opt-in self-tracing | accepted | 2026-07-16 |
| `ADR-0183` | HTTP server consolidation — server/ is the strategic surface; serve/ is frozen and migrates incrementally | accepted | 2026-07-16 |
| `ADR-0184` | Secure-by-default local server auth — no more anonymous admin | accepted | 2026-07-16 |
| `ADR-0185` | Optional application-layer envelope encryption at rest — KMS-wrapped DEKs, not the default | accepted | 2026-07-16 |
| `ADR-0186` | Dependency and vulnerability management — scanning gates and CVE response SLAs | accepted | 2026-07-16 |
| `ADR-0187` | Support bundle — nova support-bundle diagnostics tarball with normative redaction | accepted | 2026-07-16 |
| `ADR-0188` | API deprecation and sunset policy — RFC 8594 headers and a published register | accepted | 2026-07-16 |
| `ADR-0189` | Entitlement stance — no license keys, no runtime enforcement, no phone-home | accepted | 2026-07-16 |
| `ADR-0190` | SCIM group→role assignment provenance — SCIM revokes only what SCIM granted | accepted | 2026-07-16 |
| `ADR-0191` | Audit-log SIEM egress — nova audit-log export/tail in OCSF/CEF/JSONL; the site's shipper does transport | accepted | 2026-07-16 |
| `ADR-0192` | Operational alerting — an ops.* event family and notification adapters layered on the ADR-0137 lifecycle emitter | accepted | 2026-07-16 |
| `ADR-0193` | API keys and key-rotation lifecycle — hashed scoped keys shown once, plus KEK re-wrap tooling | accepted | 2026-07-16 |
| `ADR-0194` | Official TypeScript SDK — thin generated-plus-handwritten client for the /v0 REST surface | accepted | 2026-07-16 |
| `ADR-0195` | FIPS 140-3 posture statement — crypto inventory, validated-module dependence, no product claim | accepted | 2026-07-16 |
| `ADR-0196` | Additive first-party `facets` container on the Run Capsule schema | accepted | 2026-07-20 |
| `ADR-0197` | `evidence_source` — marking operator assertion apart from capsule-verified evidence in compliance exports | accepted | 2026-07-20 |
| `ADR-0198` | Device-grant demo flow is opt-in; JWT verification is pinned to asymmetric algorithms | accepted | 2026-07-24 |
| `ADR-0199` | Dashboard scale posture — bounded queries, keyset cursors, watermark caching, honest truncation | accepted | 2026-07-24 |
| `ADR-0200` | CLI→dashboard parity classification guard + generic compliance-export registry | accepted | 2026-07-24 |
| `ADR-0201` | Server-side chart rendering — stdlib SVG end-to-end, WeasyPrint PDF, no raster dependency | accepted | 2026-07-24 |
| `ADR-0202` | Python client SDK — a typed, sync-first httpx client for the /v0 REST API (`novafabric.client`) | accepted | 2026-07-24 |
| `ADR-0203` | Server ingest hardening — upload size cap, streaming spool, zip-bomb guards | accepted | 2026-07-24 |
| `ADR-0204` | Capsule content full-text search — SQLite FTS5 over redacted capsule text, on-ingest, with CLI and dashboard surface | accepted | 2026-07-24 |
| `ADR-0205` | Server-side webhook subscription registry — /v0/webhooks CRUD, signed delivery, persisted delivery log | accepted | 2026-07-24 |
| `ADR-0206` | Bulk capsule operations and keyset pagination for the /v0 API | accepted | 2026-07-24 |
| `ADR-0207` | Batch import / instance interchange — `nova import`, the verified inverse of the ADR-0141 blob export | accepted | 2026-07-24 |
| `ADR-0208` | Per-org/workspace usage metering and quota scoping — ingest-time accounting, /v0/usage, per-workspace warn-then-reject | accepted | 2026-07-24 |
| `ADR-0209` | Default-path wiring for the seven unwired extended capture events — a public recorder façade plus honest adapter wiring, no heuristics | accepted | 2026-07-24 |
| `ADR-0210` | REST erasure execution — wire the compliance erasure endpoints to the real crypto-shred machinery | accepted | 2026-07-24 |
| `ADR-0211` | Automated Postgres restore and a startup schema-skew guard | accepted | 2026-07-24 |
| `ADR-0212` | Read-only graph-analytics layer over the lineage store (centrality, hubs, articulation points) | accepted | 2026-07-24 |
| `ADR-0213` | Node-level root-cause ranking over the lineage graph (`nova lineage root-cause`) | accepted | 2026-07-24 |
| `ADR-0214` | Lineage graph export interop: GraphML, GEXF, and Cypher (`nova lineage export-graph`) | accepted | 2026-07-24 |
| `ADR-0215` | Synthesized graph-intelligence report (`nova insights`) | accepted | 2026-07-24 |
| `ADR-0216` | Backup coverage expansion — full local state, sensitive members, manifest-only profile | accepted | 2026-07-24 |
| `ADR-0217` | Automated pg-dump restore — safety semantics, DSN hygiene, RLS re-application | accepted | 2026-07-24 |
| `ADR-0218` | Two Merkle constructions: authoritative domains and non-interoperability | accepted | 2026-07-24 |
| `ADR-0219` | LineageConsumer bulk-COPY: real KuzuDB schema + idempotent node upsert | accepted | 2026-07-30 |
| `ADR-0220` | Reconcile the Go collector's event envelope with the canonical CapsuleEventType taxonomy | accepted | 2026-07-30 |
| `ADR-0221` | Opt-in psycopg connection pool for the Postgres metadata store | accepted | 2026-07-30 |
| `ADR-0222` | Default-install dependency tier reclassification: duckdb, pyarrow, python-louvain and clickhouse-connect move to extras | accepted | 2026-07-30 |
| `ADR-0223` | Schema identity: nine schema pairs share one $id and disagree; separate the v1 target from the in-force v0 | accepted | 2026-08-02 |
| `ADR-0224` | Concurrent in-process captures: single-owner hooks now, task-scoped recorder later | accepted | 2026-08-02 |
| `ADR-0225` | Persistent nova query index: mtime-validated cache outside the capsule directory | accepted | 2026-08-02 |
