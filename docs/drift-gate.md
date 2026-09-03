# Drift Detection

`nova drift` is NovaFabric's offline drift-detection surface
([ADR-0147](./decisions.md) — see
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

Each detector takes its samples in a JSON document. **`nova drift collect`
now builds that document from your sealed capsules** — you no longer transcribe
numbers by hand for `detect` and `silent-failure`. It reads through the same
ADR-0129 scanner `nova query` uses, so both agree about what a capsule is and
what a window means. Collecting a *trajectory* for `fingerprint`, and the
provenance lists for `root-cause`, are still **planned**.

---

## The commands, and what each is for

`drift` answers *did something change?* The `assure` commands are the rest of
ADR-0147 — the reference to measure against, and what to do with an answer.

| Command | Question it answers | Exits non-zero? |
|---|---|---|
| `nova drift collect` | What do my sealed capsules and lineage graph actually say? | no — it collects, it does not judge |
| `nova drift detect` | Did the distribution shift past my threshold? | no — an observation |
| `nova drift silent-failure` | Did a run report success while its quality signal fell? | no — an observation |
| `nova drift root-cause` | What changed between a baseline run and a drifted one? | no — a correlation |
| `nova drift fingerprint` | Did this agent's *behaviour* shift, beyond benign non-determinism? | no — an observation |
| [`nova assure-baseline`](cli-reference.md) | Which sealed capsule *is* the reference, and is it still intact? | `1` if the pinned bytes changed |
| [`nova replay-equivalence`](cli-reference.md) | Did a replay behave equivalently to the baseline? | `1` if not equivalent |
| [`nova assure-canary`](cli-reference.md) | Did a canary replay match, and on the same stack? | `1` if it alarms |
| [`nova assure-alarm`](cli-reference.md) | Is this a *statistically significant* regression, not noise? | `1` if the alarm fires |
| [`nova assure-impact`](cli-reference.md) | What did a model swap do across a corpus? | no — it must not decide adoption |
| [`nova assure-run`](cli-reference.md) | Did the scheduled assurance run happen, and is one overdue? | `1` if overdue |

The exit codes are not arbitrary. A **detector** reports and exits `0`; a
**check** with a verdict exits non-zero; and `assure-impact` exits `0` even on
regressions because ADR-0147 forbids it from deciding whether to adopt a model —
a non-zero exit *would be* that decision.

> **The loop is one you drive.** NovaFabric schedules nothing — not here and not
> anywhere: retention is *"local-first — no daemon; run `nova retention apply`
> from cron/systemd"*, usage periods finalize lazily with *"no cron"*, and the
> warm capture daemon is opt-in. Verified against the tree 2026-09-02: there is no
> recurring scheduler in the product. That is a deliberate posture, not a gap —
> a tool that watches your production system should not also be a daemon you have
> to operate.
>
> So drive this loop from cron, CI, or by hand. `nova assure-run record` and
> `check` exist precisely so a loop *you* drive can prove it ran and a missed run
> is still detectable.
>
> ⚠ ADR-0147's title does say "a standing production loop", and the canary
> orchestration it describes (NF-153) is **not built** — only the evidence objects
> are. The record is honest about that; the title is the part that overreaches.

---

## nova drift collect — the sealed capsules, as a detector document

The detectors compare samples. This is where the samples come from.

```bash
# what is in my capsules over the last week?
$ nova drift collect --capsules ./capsules --window 7d.. --json

# what changed between a good run and a drifted one, from the lineage graph
$ nova drift collect --emit root-cause --run run-42 --baseline-run golden-1 --json > rc.json
$ nova drift root-cause rc.json

# fingerprint one run against a golden one, straight from the capsules
$ nova drift collect --capsules ./capsules --emit fingerprint \
    --run run-42 --baseline-run golden-1 --threshold 0.2 --json > fp.json
$ nova drift fingerprint fp.json

# a silent-failure document, ready to run
$ nova drift collect --capsules ./capsules --window 7d.. \
    --emit silent-failure --quality-metric pass-rate --threshold 0.8 --json > runs.json
$ nova drift silent-failure runs.json

# last week against the month before it
$ nova drift collect --capsules ./capsules --emit detect --dimension cost \
    --baseline 30d..2026-07-05T00:00:00Z --window 2026-07-05T00:00:00Z.. \
    --statistic psi --threshold 0.2 --json > drift.json
$ nova drift detect drift.json
```

**Reading it:**

- **It adds no new capsule reader.** `query/indexer.py` (ADR-0129) already defines what *is* a
  capsule — every immediate subdirectory carrying a `capsule.yaml`/`capsule.json` manifest — and
  the ADR-0225 cache sits in front of it. A second walker would be a second definition, and the
  two would eventually disagree about which directories count. `--no-cache` forces the
  authoritative full scan.
- **The window is half-open**, `since` inclusive and `until` exclusive, because that is what
  `nova query` means by it. `--window 7d..` is "everything since seven days ago".
  ⚠ `since` accepts a duration (`7d`) **or** a timestamp; `until` accepts a **timestamp only** —
  so a closed baseline window is written `30d..2026-07-05T00:00:00Z`, not `30d..7d`. That
  asymmetry is inherited from `nova query` on purpose: accepting a duration here that
  `nova query` rejects would make the two mean different things by the same words.
- **A missing value is not a zero.** A run that recorded no cost is left *out* of a cost sample
  rather than entered as `0.0`, and every document reports `contributing` and `missing` counts.
  Averaging absent runs in as zero drags the distribution toward a drift that did not happen.
- **Mixed currencies are refused, not summed** — EUR minor units added to JPY minor units is a
  number with no meaning.
- **An empty sample refuses to become a drift document.** A statistic over nothing is not "no
  drift"; it is no evidence, and the two must not serialise the same way. (An empty *window* is
  a legitimate `n: 0` from `--emit runs`.)
- **It collects; it does not judge.** No `drifted` flag is computed here, and `--threshold` /
  `--statistic` are your policy — never defaulted.
- **A malformed line in `tool-calls.jsonl` is refused, not skipped.** The conformance reader
  next door skips one, and is right to — a single bad line should not fail a summary. Here it
  would silently drop a step from the trajectory, and a fingerprint missing a step still looks
  like a perfectly good fingerprint.
- The `kind` is **derived** from the dimension: `score:<name>` is NF-151 output-drift, while
  cost, tokens, latency and `model-calls` are NF-152 behavioral-drift. Letting a caller label a
  cost distribution as output-drift would file the evidence under a claim it does not support.

Dimensions: `cost`, `prompt-tokens`, `completion-tokens`, `total-tokens`, `latency`,
`model-calls`, and `score:<name>` for any score in `scores.jsonl`.

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
NovaFabric records that drift occurred and its probable cause. It does not
remediate, retrain, roll back, or assert the drift is acceptable.
```

That last line is on **every** `drift` and `assure` command, by design: the
output is verdict-shaped (`DRIFTED` in red), and ADR-0147 exists partly because
a detector that looks like a gate gets treated as one. With `--json` it goes to
stderr, so `nova drift detect … --json | jq` still works.

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

`nova drift collect --emit root-cause --run <drifted> --baseline-run <baseline>`
pulls both provenance lists straight from the lineage store, so you no longer
supply them by hand.

⚠ **A run that is not in the lineage graph is refused, not collected as an empty
list.** `provenance()` returns `[]` both for a ref with no ancestors and for a ref
that is not there at all, and two empty lists diff to `no_change` — *"nothing
changed between these runs"*, a finding manufactured out of missing data. The walk
`--depth` travels with the document for the same reason: a walk too shallow to
reach the change that happened looks exactly like no change.

## nova drift fingerprint — did the behaviour itself shift?

The other detectors compare *numbers* about runs. This one compares what the
agent **did**: a deterministic signature over its canonicalized trajectory, its
tool mix, and its score profile.

Input: `{"run": {run_id, calls: [{name, arguments}], scores?}, "baseline"?: {…}, "threshold"?, "commutable"?, "idempotent"?}`.

```bash
$ nova drift fingerprint run.json
Behavioral fingerprint — run-2026-07-12-a
  signature = sha256:a68396…
  basis     = trajectory, tool-mix, score-profile
  vs baseline sha256:7b3ff8…: SHIFTED
  distance = 0.2822   threshold = 0.2
    trajectory: 0.3333
    tool-mix: 0.3333
    score-profile: 0.1800
```

**Reading it:**

- The trajectory is normalized by the **same ADR-0144 canonicalizer** the
  equivalence engine uses, not a second one. A collapsed idempotent retry, a
  reordered pair of *declared-commutable* calls, or a difference in argument key
  order does not move the signature; a different trajectory does. Nothing is
  assumed commutable — you declare it.
- **The signature is version-sensitive.** It covers the canonicalization
  `rules_version`, so a rules change cannot quietly read as "no shift", and
  comparing two fingerprints built under different rule versions is *refused*
  rather than scored.
- **`distance` is a metric, not a digest comparison.** It is the mean of the
  contributing components — normalized edit distance over the trajectory,
  total-variation distance over the tool mix, and the gap between mean scores —
  each bounded `[0, 1]`. Comparing two hex digests could only ever answer
  same/different.
- **A component missing on one side does not count as zero.** If no component is
  present on both sides, `distance` and `shifted` are `null` with a stated
  reason: *unknown* is not *unchanged*.
- **A run with no tool calls and no scores is refused.** A signature over nothing
  compares equal to every other nothing, which would read as "behaviour
  unchanged" when nothing was measured.
- A shift is an **observation, not a regression** — there is no
  `regressed`/`failed` field, and the command exits `0` either way.

`nova drift collect --emit fingerprint --run <id> [--baseline-run <id>]` builds this
document straight from the sealed capsules, so the trajectory need not be
transcribed. Scores are **opt-in** via `--quality-metric`: folding in whatever
scores happened to be present would change the basis, and therefore the
signature, without you asking.

## Typical loop

0. **Pin the reference.** `nova assure-baseline pin` designates a sealed golden
   capsule and binds it to that capsule's Merkle root. Until this existed there
   was no mechanism behind the phrase "your frozen baseline" — a baseline that
   can drift is not one. `nova assure-baseline verify` re-checks it offline.
1. Export a quality metric per run (eval score, judge score) as you already
   seal runs.
2. Weekly: `nova drift collect` builds both documents from the sealed capsules —
   `--emit detect` for the current window against your baseline window, and
   `--emit silent-failure` over the window's runs — then `nova drift detect` and
   `nova drift silent-failure` read them.
3. **Ask whether it is signal.** `nova assure-alarm check` runs the SPRT over the
   window and fires only on a statistically significant regression — a single-run
   dip does not alarm, which is why this is not a threshold gate.
4. On a drift flag: `nova drift root-cause` between a representative baseline
   run and a drifted run; investigate the changed input.
   `nova drift fingerprint` against the baseline run says whether the *behaviour*
   moved or only the numbers did.
5. **Before a model swap:** replay the corpus and feed the per-run verdicts from
   `nova replay-equivalence check` into `nova assure-impact report`. It reports
   what changed; deciding whether to adopt stays with you.
6. **Prove the loop ran.** `nova assure-run record` after each cycle, and
   `nova assure-run check` to catch a cycle that never happened — a missed run
   writes nothing, so it is detected against the previous run's `next_due`.
7. Keep the `--json` records with your run evidence — they are observations
   over sealed data and reproducible from it.

---

## See also

- [User guide — lineage graph](user-guide.md) — `nova lineage provenance`,
  the source of root-cause input lists
- [Assurance cases](assurance-cases.md) — drift records also surface there as
  staleness/defeater evidence
- [CLI reference](cli-reference.md) — full options for `assure-baseline`,
  `assure-alarm`, `assure-canary`, `assure-impact`, `assure-run` and
  `replay-equivalence`
- `nova drift --help` and per-subcommand `--help` for the exact document
  schemas
