# Drift Detection

`nova drift` is NovaFabric's offline drift-detection surface
([ADR-0147](../design/adr/0147-drift-continuous-assurance.md) — see
`nova drift --help`):
it computes distribution-shift statistics over samples drawn from **already
sealed** capsules, flags silent failures, and links an observed drift to the
input that changed. Everything runs offline over data you already captured —
**no model re-invocation, zero token cost**.

**Status: experimental** (v0.61, shipped 2026-07-16).

The load-bearing design decision, stated up front because it shapes every
output: **NovaFabric detects and evidences drift; it never rules on it.**
`drifted` is a threshold fact, a silent failure is a detector observation,
and a root cause is a correlation — none of them is a pass/fail verdict, and
nothing is auto-remediated. Accordingly, all three subcommands exit `0`
whether or not anything is flagged; `2` means bad input, nothing else.

In this first slice, each subcommand takes its samples directly in a JSON
document — you extract them from your capsules/scores. The collector that
reads baseline/window samples from sealed capsules over a
`--baseline`/`--window` range is a documented follow-on (**planned**).

---

## nova drift detect — two-sample drift

Computes a distribution-free two-sample statistic between a **baseline**
sample and a current **window** sample, and records whether it crossed your
threshold. Stdlib-only math; three statistics:

- **PSI** (Population Stability Index) — numeric samples; common bands:
  `<0.1` no shift, `0.1–0.25` moderate, `>0.25` significant;
- **KS** — two-sample Kolmogorov–Smirnov max-CDF distance, in `[0, 1]`;
- **Jensen–Shannon distance** — categorical distributions (e.g. a tool-call
  mix), in `[0, 1]`.

Two record kinds:

- `kind: "output"` — output drift (score distributions, response lengths):
  `{metric, statistic: psi|ks, baseline: [...], window: [...], threshold,
  window_meta: {from, to, run_ids}, baseline_id?}`;
- `kind: "behavioral"` — behavioral drift (cost/tokens per run, tool-call
  mix): `{dimension, distance, baseline, window, threshold}` — mappings use
  Jensen–Shannon, numeric sequences use `psi`/`ks`.

```bash
$ cat drift.json
{"kind": "output", "metric": "score-dist", "statistic": "psi",
 "baseline": [0.82, 0.85, 0.81, 0.86, 0.84, 0.83, 0.85, 0.82],
 "window":   [0.71, 0.69, 0.74, 0.70, 0.72, 0.68, 0.73, 0.70],
 "threshold": 0.25,
 "window_meta": {"from": "2026-07-01T00:00:00Z", "to": "2026-07-08T00:00:00Z",
                 "run_ids": ["run-101", "run-102"]},
 "baseline_id": "baseline-june"}

$ nova drift detect drift.json
Drift (output) — score-dist: DRIFTED
  psi = 39.0434   threshold = 0.25
```

**Reading it:** `DRIFTED` means `value >= threshold` — a fact about two
distributions, not a judgment about the runs. `--json` emits the full drift
record (including the window metadata and `baseline_id`) for downstream
tooling.

## nova drift silent-failure — success that wasn't

A *silent failure* is a run that reported a terminal **success** status while
its independent quality signal (a judge score, a pass rate — higher is
better; invert error rates first) fell below your threshold. The detector
reads two things sealed per run — `status` and a quality score — and flags
the disagreement. A run that already reported failure is never *silent*.

Input: `{"runs": [{run_id, status, quality_signal}], "threshold",
"success_statuses"?}` (default success status: `"success"`).

```bash
$ nova drift silent-failure runs.json
Silent-failure scan — 1 flagged of 2 reported-success run(s), threshold 0.6
  silent-failure run-102 (status=success, quality=0.42)
```

**Reading it:** a flag is surfaced for review — there is deliberately no
`failed`/`verdict` field, and the command exits `0` even when runs are
flagged.

## nova drift root-cause — what changed between the runs

Given a drift, diffs the lineage-provenance ancestors of a **baseline** run
against a **drifted** run — which `model`, `prompt`, `tool`, or `dataset`
ref changed between the two (the compared kinds are configurable via
`kinds`).

Input: `{"baseline": [{kind, ref}], "drifted": [{kind, ref}], "kinds"?}`.

```bash
$ nova drift root-cause rc.json
Drift root-cause — confidence (sole_change), correlation_only=True
  model: ['gpt-4o-2024-11-20'] -> ['gpt-4o-2025-03-01']
```

**Reading it:**

- `correlation_only` is **forced `true`** — a changed input that co-occurs
  with a drift is a hypothesis to investigate, never a proven cause. There
  is deliberately no `caused`/`blame` field.
- `confidence` is a descriptive **category, not a grade**: `no_change`
  (provenance identical over the compared kinds), `sole_change` (exactly one
  kind changed — the strongest hypothesis), `multiple_changes` (the signal
  is diffuse).

The follow-on that pulls both provenance lists straight from the lineage
store (`LineageStore.provenance(...)`) for two run refs is **planned**; today
you supply them (e.g. from `nova lineage provenance` output).

## Typical loop

1. Export a quality metric per run (eval score, judge score) as you already
   seal runs.
2. Weekly: `nova drift detect` current window vs your frozen baseline;
   `nova drift silent-failure` over the window's runs.
3. On a drift flag: `nova drift root-cause` between a representative baseline
   run and a drifted run; investigate the changed input.
4. Keep the `--json` records with your run evidence — they are observations
   over sealed data and reproducible from it.

---

## See also

- [User guide — lineage graph](user-guide.md) — `nova lineage provenance`,
  the source of root-cause input lists
- [Assurance cases](assurance-cases.md) — drift records also surface there as
  staleness/defeater evidence
- `nova drift --help` and per-subcommand `--help` for the exact document
  schemas
