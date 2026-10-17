# Safety, limitations and pre-deployment checks — novafabric

**Scope:** Capturing, storing, replaying and presenting evidence about AI-agent execution traces.  
**Issued:** 2026-10-17  
**Status:** Risk guidance, **not** a safety certification, compliance approval, or verified representation that technical controls have been implemented.

This document is meant to help users and maintainers identify foreseeable risks and the checks **they still need to perform**. It supplements [DISCLAIMER.md](DISCLAIMER.md) and the existing software license; it does not override either or remove non-waivable duties.

## Material risks to assess

- Run capsules, traces, prompts, artifacts and replay records may contain sensitive data, proprietary material or credentials.
- Replay or connected hooks can trigger external side effects or disclose data if isolation and permissions are weak.
- Hashes and cryptographic seals (where provided) attest to limited integrity properties, not automatically to truth, legality, completeness, privacy or independence.

## Required assessment before consequential deployment

- [ ] Document and test data classification, retention, redaction, encryption, access control, deletion and cross-tenant isolation where relevant.
- [ ] Ensure any replay that could call external tools is disabled or sandboxed unless explicitly and separately authorized.
- [ ] Test ingestion of hostile artifacts, prompt injection, invalid signatures and replay divergence.
- [ ] Validate threat model and provenance claims against independent evidence; keep records of limitations and incidents.

## Operational conditions

- Start with an isolated, disposable environment and the least privileges possible.
- Do not infer that a feature claim in project documentation proves that the safeguard is effectively enforced in every execution path. Perform threat modeling and independent testing.
- Independently validate material outcomes. Preserve audit evidence, tested version, configuration, dependencies, approver identity and incident response arrangements where appropriate.
- Before exposing a networked service, inspect authentication, authorization, logging, data handling, supply chain and applicable laws.
- Pause deployment when unresolved critical flaws, missing permissions, unexplained automation, or inaccurate claims are discovered. Route security reports privately rather than disclosing exploitable details in public issues.

**Important:** Project maintainers and users remain subject to applicable mandatory safety, privacy, security and product laws. A disclaimer does not substitute for these checks or guarantee immunity from claims.
