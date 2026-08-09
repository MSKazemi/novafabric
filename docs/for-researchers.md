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
every model call (model identifier, parameters, token counts, latency), every tool
invocation, the inputs and outputs, and a proof that no secrets were retained.

```console
$ nova capture python experiments/run_benchmark.py --config configs/main.yaml
✓ Capsule written: ~/.novafabric/capsules/01HXAY7M5JZ8R7K4P9DPBYK2WX
```

A reviewer can then, without your API keys and without network access:

```console
$ nova validate <run-id>                      # schema-valid, redaction proof intact
$ nova replay  <run-id> --mode forensic       # inspect, execute nothing
$ nova replay  <run-id> --mode mocked         # re-run with the recorded responses
$ nova diff    <run-a> <run-b>                # what actually differed between two runs
```

`mocked` replay is the one that matters most for review: the pipeline re-executes
deterministically, serving recorded model responses from the capsule, at no API cost. A
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
$ nova export-evidence <run-id>
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

The repository carries a [`CITATION.cff`](../CITATION.cff), so GitHub's "Cite this
repository" button produces BibTeX and APA directly. Please cite the **version you used** —
behaviour changes between releases, and a citation without a version is not reproducible
either.

A DOI is being minted via Zenodo; until it appears in `CITATION.cff`, cite the repository URL
and the exact version, e.g. `novafabric 0.101.0`.

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
