# For researchers

Using NovaFabric to make a computational result reproducible, reviewable, and citable — and
an honest account of what it does not solve.

---

## The problem this addresses

Reproducibility in conventional software rests on recovering the code, the inputs, and the
environment. Once a pipeline calls a hosted model or an external tool, that premise fails:
weights are updated without notice, provider behaviour drifts, tool responses vary between
invocations, and a scheduled HPC job rebuilds its environment on every allocation. Pinning
`requirements.txt` does not pin the model that answered.

A reviewer six months later cannot re-run your pipeline and obtain your numbers, and neither
can you. NovaFabric's response is to record the execution itself, as an artefact you keep.

## What a capsule gives a reviewer

Wrapping the command produces a directory holding the command line, the environment lock,
the model calls capture can see (model identifier, parameters, token counts, latency),
tool invocations, the inputs and outputs, and a secret-scan record (rule-based scanning
for known key and token patterns).

```console
$ nova capture python experiments/run_benchmark.py --config configs/main.yaml
✓ Capsule written: ~/.novafabric/capsules/01HXAY7M5JZ8R7K4P9DPBYK2WX
```

A reviewer can then, without your API keys and without network access:

```console
$ nova validate <run-id>                      # schema-valid, secret-scan record present
$ nova replay  <run-id> --mode forensic       # inspect, execute nothing
$ nova replay  <run-id> --mode mocked         # re-run with the recorded responses
$ nova diff    <run-a> <run-b>                # what actually differed between two runs
```

`mocked` replay is the one that matters most for review: it re-runs the pipeline against
the recorded model responses from the capsule, at no API cost. Since v0.105.0 that covers
OpenAI chat completions and Responses API calls and Anthropic messages, sync or async,
streamed or not, and recorded MCP tool results; every other tool runs live (v0.104.0
served only synchronous, non-streaming chat calls). Re-running is not a determinism guarantee: anything the capsule does not
serve — tools, the clock, the filesystem — can still differ. A
reviewer without a budget or an account can still run your experiment.

## A workflow for a paper artifact

**1. Capture the runs that produce every reported number.** One capsule per experiment.
Failed runs produce complete capsules too, with `status: failure` — keep them; the negative
results are part of the record.

**2. Register the assets you depend on** so the capsule references stable identities rather
than free text:

```console
$ nova register model-spec.yaml       # name@version, pinned to a git SHA
$ nova list
```

**3. Seal and export.** An Evidence Bundle verifies **offline, with no NovaFabric
installed** — only `sha256sum` and an `ed25519` verifier:

```console
$ nova export-evidence ~/.novafabric/capsules/<run-id> \
    --key ~/.novafabric/keys/signing_key.pem --output evidence.zip
```

This property is deliberate. Evidence that can be checked only by the tool that produced it
is not evidence, and an artifact-evaluation committee should not have to install your stack
to believe your numbers.

**4. Publish the capsules alongside the paper.** They are plain directories: archive them in
Zenodo, figshare, or your institutional repository next to the code.

**5. Record the lineage** if outputs feed each other, so the dependency graph between runs
and artifacts is explicit rather than implied by filenames:

```console
$ nova lineage provenance <artifact>     # what produced this
$ nova lineage replay-chain <artifact>   # what must be re-run to regenerate it
```

**6. Pin what a re-run must match, and export a FAIR research object** (**experimental**,
ADR-0164 P2). A *reproducibility receipt* binds the environment digest, seed(s), input-data
digest, code digest and optional workflow digest under one `bound_root`, plus your declared
`determinism_class`. Anything you do not supply is listed in `receipt_incomplete` — it is
never filled in for you:

```console
$ nova science receipt build --capsule <run-id> --env sha256:<lockfile> \
    --data sha256:<inputs> --code sha256:<commit-tree> --seed 1337 \
    --determinism statistical --write
$ nova science receipt verify --capsule <run-id>
$ nova export-rocrate-science --capsule <run-id> --out ./crates --orcid 0000-0002-1825-0097
```

`export-rocrate-science` needs a capsule carrying the science-provenance facet (the
hypothesis→claim lineage, NF-321). It writes a Workflow Run Crate 0.5 profile over the
RO-Crate 1.1 carrier `nova export-rocrate` already produces, plus a `<run-id>.fair-binding.json`
record with the crate's metadata digest. The same capsule always produces the same bytes.

The receipt says *what would have to match*; it never re-runs anything and never claims the
run **is** reproducible (`reproducible_in_fact: null`). The workflow and code are referenced by
digest, not included, so a strict Workflow RO-Crate validator will flag the workflow entity as
contextual. The Provenance Run Crate profile (per-step records) is **planned**.

**7. Bind a lab experiment and its instruments** (**experimental**, ADR-0164 P3). When the
experiment ran in a self-driving lab, a cloud lab, by hand, or in simulation, record its
*declared* provenance next to the lineage: the protocol digest, the lab's job id, whether it was
`sim`, `real` or `hybrid`, the outcome digest, and one record per instrument (firmware digest,
calibration-record digest and timestamp, manufacturer reference). The experiment names its
instruments by their record digests, so swapping a firmware digest afterwards breaks the link:

```python
from novafabric.science.lab import attach_lab, build_instrument_record, build_lab_experiment

hplc = build_instrument_record(
    instrument_id="hplc-7", instrument_class="hplc",
    firmware_digest="sha256:<fw>", calibration_ref="sha256:<calibration record>",
    calibration_timestamp="2026-06-30T09:00:00Z", manufacturer_ref="https://ror.org/<id>",
)
experiment = build_lab_experiment(
    lab_kind="self_driving", protocol_ref="sha256:<protocol>", run_id="sdl-job-001",
    sim_to_real="real", outcome_digest="sha256:<outcome table>",
    instrument_refs=[hplc.record_digest], started_at="2026-07-15T09:30:00Z",
)
capsule = attach_lab(capsule, experiment, [hplc])
```

```console
$ nova science lab show --capsule <run-id>
$ nova science lab verify --capsule <run-id>
$ nova science instrument show --capsule <run-id>
```

`lab verify` fails when an instrument reference resolves to no record, an instrument was
calibrated after the experiment started, a digest no longer re-derives, or the block names an
unknown `lab_kind`. It checks that your declarations are *coherent*, not that the experiment
was sound (`verdict: null`). NovaFabric never talks to the lab or the instrument, and the
blocks refuse telemetry, raw readings, or credentials outright — record the digest of a
calibration record or data stream, never its contents.

## Artifact-evaluation badges

Most committees assess roughly the axes below (ACM's terminology; other venues differ in
wording, less so in substance). NovaFabric helps with some and not others — the third column
is the honest part.

| Axis | What is asked | Where NovaFabric helps |
|---|---|---|
| **Available** | Artifact is archived with a DOI | Not its job — use Zenodo/figshare. Capsules are ordinary directories and archive cleanly. |
| **Functional** | Documented, consistent, complete, exercisable | Strong. A capsule *is* the documented execution, and `nova validate` makes "complete" checkable rather than asserted. |
| **Reusable** | Others can repurpose it | Helps. The environment lock and registered assets state what a reuser must reproduce. |
| **Results Reproduced** | An independent party obtains the results | Partial, and this is the honest limit. `mocked` replay reproduces *your recorded run* exactly, which demonstrates the pipeline is deterministic given those responses. It does **not** demonstrate that a fresh call to the live model would produce them again — see below. |

## What this does not solve

Stated first rather than last, because a reproducibility tool that oversells is worse than
none.

**Byte-exact replay against a hosted model is not offered.** It would require a deterministic
environment and a per-call seed that hosted endpoints do not provide. `exact` mode is
realistic for a local or on-prem model. For models that drift, `semantic` mode re-executes
and scores similarity of meaning on a 0.0–1.0 scale. If you have seen "deterministic replay"
advertised for hosted models, read the fine print — including ours.

**A capsule does not make a result correct.** It records what happened. A faithfully captured
run of a flawed experiment is a faithful record of a flawed experiment.

**Capturing is not controlling.** NovaFabric does not fix your seeds, your data splits, or
your evaluation protocol. It records what they were.

**The formats are not frozen.** Capsule and Evidence Bundle schemas change until the v1.0
freeze — additively, with old capsules remaining readable, but they move. Pin a version for a
long-lived artifact and state it in the paper.

**No certification of anything.** See [Standards and specifications](standards-conformance.md)
for the full list of what is and is not claimed.

## Citing NovaFabric

The system and its evaluation are described in the paper
[*NovaFabric: Tamper-Evident, Replayable Evidence for Autonomous AI Agent Runs*](https://arxiv.org/abs/2609.12582)
(arXiv:2609.12582, 2026) — cite it for the design and the measured results.

The repository carries a [`CITATION.cff`](../CITATION.cff), so GitHub's "Cite this
repository" button produces BibTeX and APA directly. When you used the software, also cite the
**version you used** —
behaviour changes between releases, and a citation without a version is not reproducible
either.

A DOI is being minted via Zenodo; until it appears in `CITATION.cff`, cite the repository URL
and the exact version, e.g. `novafabric 0.105.0`.

## Working with us

Research use is the case this project was built for, and the feedback loop is short:

- **A capsule that fails to validate, or a replay that diverges unexpectedly, is a bug** —
  and a valuable report. [Open an issue](https://github.com/MSKazemi/novafabric/issues/new/choose).
- **If a field you need for your discipline is missing from the schema**, say so before the
  v1.0 freeze. The schema is additive and optional-first specifically so domain fields can be
  accommodated, and the [v1.0 discussion](https://github.com/MSKazemi/novafabric/discussions)
  is open now. Arguments made there carry real weight; after the freeze they carry much less.
- **If you publish using NovaFabric**, we would like to know — both to link the work and
  because how it is actually used in a discipline is the best available guide to what to
  build next.

## See also

- [Getting started](getting-started.md) · [Concepts](concepts.md)
- [Benchmarks](benchmarks.md) — measured overhead, each number with its command and hardware
- [Standards and specifications](standards-conformance.md) — what is implemented and what is not claimed
- [Assurance cases](assurance-cases.md) — conformance receipts, never verdicts
- [Prove a run to an auditor](tutorials/prove-a-run-to-an-auditor.md)
