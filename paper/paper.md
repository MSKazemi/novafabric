---
title: 'NovaFabric: portable, verifiable execution evidence for AI and HPC runs'
tags:
  - Python
  - reproducibility
  - provenance
  - AI agents
  - research software engineering
  - high-performance computing
  - audit
authors:
  - name: Mohsen Seyedkazemi Ardebili
    orcid: 0000-0000-0000-0000  # TODO(owner): JOSS requires a real ORCID before submission
    affiliation: 1
affiliations:
  - name: Independent researcher
    index: 1
date: 9 August 2026
bibliography: paper.bib
---

<!--
DRAFT — not submitted.

Before submitting to JOSS:
  1. Replace the placeholder ORCID above with the author's real identifier.
     JOSS will not accept the submission without it.
  2. Confirm the affiliation line.
  3. Audit every reference in paper.bib against the actual source. Do not
     submit citations that have not been opened and read — the /mska-scientific-
     reference-auditor skill exists for exactly this.
  4. Re-check the word count (JOSS expects roughly 250-1000 words in the body).
  5. Confirm the archived release DOI (Zenodo) is minted and quoted in the
     submission form.
-->

# Summary

Computational results are reproducible when the code, the inputs, and the environment can
be recovered and re-run. For workloads that call large language models (LLMs) or external
tools, that premise no longer holds: model weights are updated without notice, provider
behaviour drifts, tool responses vary between invocations, and the execution environment of
a scheduled HPC job is rebuilt on every allocation. A run that succeeded on one day can fail
on the next with no corresponding change in the source repository.

`NovaFabric` records the execution itself. Wrapping any command — a script, a notebook cell,
an agent, a batch training job — produces a **run capsule**: a schema-valid, secret-redacted
directory holding the command, the environment, every model call and tool invocation, the
inputs and outputs, and a cryptographic proof that no secrets were retained. Capsules can be
re-executed under controlled conditions, compared structurally against one another, linked
into a provenance graph, and exported as a signed bundle that a third party verifies offline
using only `sha256sum` and an `ed25519` verifier.

The system is local-first: it requires no server, no account, and no network access, and it
transmits no telemetry. The unit of value is a portable directory that the researcher owns,
rather than a record held in a hosted service.

# Statement of need

Existing observability platforms for LLM applications answer *what is happening now*. They
are effective for live debugging and alerting, and they are the wrong instrument for a
different question that researchers, reviewers, and auditors increasingly ask: *can this
past run be reconstructed, compared, and shown to be what it claims to be?* Answering it
requires the record to outlive the vendor, the network, and the model version — properties a
hosted trace store does not provide.

Three settings make this concrete.

**Reviewing and reproducing published results.** Artifact-evaluation committees, and readers
attempting to build on a result, need more than a repository and a requirements file when the
pipeline calls a hosted model. A capsule records which model version answered, with which
prompt, at which time, alongside the environment — and can be replayed with the recorded
responses so the pipeline runs deterministically and without incurring API cost.

**Regulated and data-restricted environments.** National laboratories, hospitals, public-sector
bodies, and firms operating under data-residency constraints frequently cannot transmit run
data to an external service at all. `NovaFabric` runs entirely within existing infrastructure,
including fully air-gapped deployments.

**High-performance computing.** On a shared compute node the user is a guest: privileged
access is unavailable, and opening a database connection from inside a training loop is not
acceptable. Capture therefore writes to a local append-only spool that is ingested elsewhere,
and requires no daemon and no root.

`NovaFabric` builds on established specifications rather than inventing formats: model calls
follow the OpenTelemetry GenAI semantic conventions [@opentelemetry], evidence is signed as
in-toto/DSSE attestations [@intoto] and timestamped under RFC 3161 [@rfc3161], provenance is
exportable as W3C PROV [@provo] and OpenLineage events [@openlineage], and digests are
computed over canonical JSON [@rfc8785]. This is a deliberate constraint: evidence that can
be verified only by the tool that produced it is not evidence.

The software also states its limits explicitly. It does **not** claim byte-exact replay of
remote LLM calls, which would require a deterministic environment and a per-call seed that
hosted endpoints do not offer; for models that drift it instead scores similarity of meaning.
It produces evidence supporting compliance workflows under instruments such as the EU AI Act
[@euaiact] and the NIST AI Risk Management Framework [@nistairmf], and does not determine
compliance, which remains a human judgement.

`NovaFabric` is implemented in Python, distributed under Apache-2.0, and installable from
PyPI. It provides a command-line interface, a Python API, a REST API for multi-user
deployments, and adapters for common agent frameworks.

# Acknowledgements

The author thanks the maintainers of the specifications this work depends on, whose open
formats made externally verifiable evidence achievable.

# References
