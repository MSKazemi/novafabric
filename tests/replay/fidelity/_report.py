"""Per-surface E1-E7 report from retained fidelity baselines (issue #59).

Input is one or more ``NOVA_FIDELITY_BASELINE`` JSON files written by a corpus session
(``conftest.py``), each the oracle observations of every scenario and control that ran.
Output is a machine-readable dict and a markdown rendering. Everything here is a pure
function of those retained files, so a number in a document can be regenerated and
checked::

    PYTHONPATH=tests python -m replay.fidelity._report run-*.json --json out.json --md out.md

What the report refuses to do:

* count a run that exercised nothing (``unmeasured``) in a denominator, or print a rate
  or an interval for a surface with no measured run;
* score from ``replay_result.yaml`` -- its counters are compared with the oracles (E7),
  never used as the measurement;
* accept a scenario it does not know, or hide a verdict outside its pre-registration.

The pass criterion of a surface is its **pre-registered verdict set**
(``_harness.SCENARIOS``), not "faithful": a surface registered as ``refused`` passes by
being refused. E1-E6 are descriptive observations of the replay phase.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import stat
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from replay.fidelity._harness import REPLAY_BUDGET_S, SCENARIOS

Z95 = 1.959963984540054

ORACLE_SCOPE = (
    "Network oracle: TCP connections accepted by two harness-owned loopback servers (a sink "
    "and a fake Ollama). It is not exhaustive network observation: traffic to any other "
    "address, and any non-TCP traffic, is not seen.",
    "Side-effect oracle: files created or changed under one designated directory outside "
    "the work dir. Writes anywhere else are not seen.",
    "Process oracle: a child process is observed only through its effect on those servers; "
    "no process accounting, no network namespace, no strace.",
    "Model oracle: requests reaching a fake OpenAI transport (httpx.MockTransport) or a fake "
    "Ollama server, plus a canary string in the workload output. Other providers and "
    "frameworks (Anthropic, MCP, LangGraph, ADK, OpenAI Agents) are not scored here.",
    "The corpus is synthetic: fake transports, no real model, no cloud account, no paid API. "
    "It is not a realistic-workload evaluation and says nothing about one.",
    "replay_result.yaml counters are compared with the oracles (E7); they are never the "
    "measurement.",
    "A workload that recorded no model or tool call is reported by the engine as faithful and "
    "replayable: full -- vacuously true, and silent about any call capture did not recognise "
    "and so did not record. The harness scores that case unmeasured and never counts it.",
    "'N surfaces met their pre-registered verdict' is not 'N faithful replays': a surface "
    "pre-registered as refused or live-reported meets it by being refused or reported.",
)

#: Control name -> the verdicts that prove the control did its job. ``faithful`` is in none of
#: them: a negative control that comes back faithful means the harness cannot fail.
#: ``test_required_controls_are_exactly_the_ones_the_controls_module_records`` keeps this in
#: step with ``test_fidelity_controls.py``.
_NOT_FAITHFUL_LIVE = frozenset({"live-reported", "live-unreported"})
REQUIRED_CONTROLS: dict[str, frozenset[str]] = {
    "drop-response": frozenset({"refused", "diverged"}),
    "force-live": _NOT_FAITHFUL_LIVE,
    "tool-not-served": _NOT_FAITHFUL_LIVE,
    "mutated-output": frozenset({"diverged"}),
    "calls-nothing": frozenset({"unmeasured"}),
    "report-only:S6b": frozenset({"live-reported", "live-unreported", "diverged", "refused"}),
}
for _sid in ("S10", "S11", "S12"):
    REQUIRED_CONTROLS[f"drop-response:{_sid}"] = REQUIRED_CONTROLS["drop-response"]
    REQUIRED_CONTROLS[f"force-live:{_sid}"] = REQUIRED_CONTROLS["force-live"]
    REQUIRED_CONTROLS[f"mutated-output:{_sid}"] = REQUIRED_CONTROLS["mutated-output"]

#: Provenance fields two sessions must share to be combined into one report.
PINNED_PROVENANCE = (
    "novafabric",
    "git_sha",
    "git_dirty",
    "python",
    "implementation",
    "platform",
    "machine",
    "dependencies",
    "fixture_sha256",
    "replay_budget_s",
)

MAX_BASELINE_BYTES = 32 * 1024 * 1024
MAX_EVIDENCE_FILE_BYTES = 8 * 1024 * 1024
MAX_EVIDENCE_TOTAL_BYTES = 64 * 1024 * 1024
MAX_EVIDENCE_FILES = 2000
_CHUNK = 64 * 1024

_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_SHA = re.compile(r"[0-9a-f]{40}")


class MixedProvenanceError(ValueError):
    """Sessions with different source, runtime or fixture provenance were offered together."""


class EvidenceError(Exception):
    """Raw evidence could not be collected or read safely."""


_SEG = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_TOP_FILE = re.compile(r"(?:markers|model-hits|sentinel)\.[A-Za-z0-9._:-]+")
#: Directory name -> the file names kept from each run directory beneath it.
_RUN_FILES = {
    "runs": frozenset({"model-calls.jsonl", "tool-calls.jsonl", "replayability.json"}),
    "runs-mutated": frozenset({"model-calls.jsonl", "tool-calls.jsonl"}),
    "replays": frozenset({"replay_result.yaml"}),
}
#: The only shape a manifest path may have: <scenario>/<file> or <scenario>/<dir>/<run>/<file>.
_EVIDENCE = re.compile(
    rf"{_SEG}/(?:"
    r"(?:markers|model-hits|sentinel)\.[A-Za-z0-9._:-]+"
    rf"|runs(?:-mutated)?/{_SEG}/(?:model-calls\.jsonl|tool-calls\.jsonl|replayability\.json)"
    rf"|replays/{_SEG}/replay_result\.yaml"
    r")"
)


# ── statistics ───────────────────────────────────────────────────────────────


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    """Wilson score interval for ``k`` successes in ``n`` trials; ``None`` when ``n == 0``."""
    if n < 0 or k < 0 or k > n:
        raise ValueError(f"impossible count {k}/{n}")
    if n == 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    lo = 0.0 if k == 0 else max(0.0, centre - half)
    hi = 1.0 if k == n else min(1.0, centre + half)
    return lo, hi


# ── one run of one scenario ──────────────────────────────────────────────────


def _live(replay: dict[str, Any]) -> dict[str, int]:
    return {
        "model": max(int(replay["model_hits"]), int(bool(replay["canary_in_output"]))),
        "tool": int(replay["tool_bodies"]),
        "network": int(replay["connections"]),
        "side_effects": len(replay["outside_writes"]),
    }


def metrics(sid: str, summary: dict[str, Any]) -> dict[str, Any]:
    """E1-E6 as the oracles saw the replay phase, plus the non-vacuity accounting."""
    claims = SCENARIOS[sid].claims
    cap, rep = summary["capture"], summary["replay"]
    live = _live(rep)
    executed = bool(rep["started"])
    finished = bool(rep["finished"])

    def applies(kind: str, ok: bool) -> bool | None:
        # None: not claimed by this scenario, or the replay never ran so there is no replay
        # activity to judge -- neither is a pass.
        return ok if kind in claims and executed else None

    return {
        "exercised": _live(cap),  # what the CAPTURE oracles saw: the non-vacuity evidence
        "live": live,
        "e1": {"executed": executed, "finished": finished},
        "e2": {
            "live_model_calls": live["model"],
            "pass": applies("model", finished and live["model"] == 0),
        },
        "e3": {
            "live_tool_executions": live["tool"],
            "pass": applies("tool", finished and live["tool"] == 0),
        },
        "e4": {
            "declared_network_coverage": (summary["report"] or {}).get(
                "network_observation_coverage"
            ),
            "declared_network_scope": (summary["report"] or {}).get("network_observation_scope"),
            "live_connections": live["network"],
            "writes_outside_work_dir": live["side_effects"],
            "pass": (live["network"] == 0 and live["side_effects"] == 0) if executed else None,
        },
        "e5": {
            "outcome_equal": bool(summary["outcome_equal"]),
            "exit_code": {"capture": cap["exit_code"], "replay": rep["exit_code"]},
            "output_digest_equal": cap["output_digest"] == rep["output_digest"],
        },
        "e6": {
            "wall_s": rep["wall_s"],
            "budget_s": REPLAY_BUDGET_S,
            "pass": (finished and rep["wall_s"] <= REPLAY_BUDGET_S) if executed else None,
        },
    }


def _e7_disagreements(sid: str, summary: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for kind, entry in sorted(summary["e7"].items()):
        silent = entry["agree"] is None and entry["oracle"] > 0 and not entry["acknowledged"]
        if entry["agree"] is False or silent:
            found.append(
                {
                    "sid": sid,
                    "kind": kind,
                    "oracle": entry["oracle"],
                    "reported": entry["reported"],
                    "acknowledged": bool(entry["acknowledged"]),
                    # The engine itself said it could not see everything: a disclosed limit.
                    "declared_limitation": kind == "network"
                    and (summary["report"] or {}).get("network_observation_coverage") == "partial",
                }
            )
    claimed = (summary["report"] or {}).get("fidelity_verdict")
    if claimed == "faithful" and summary["verdict"] != "faithful":
        found.append(
            {
                "sid": sid,
                "kind": "verdict",
                "oracle": summary["verdict"],
                "reported": claimed,
                "acknowledged": False,
                "declared_limitation": False,
            }
        )
    return found


# ── a whole report ───────────────────────────────────────────────────────────


def effective_verdict(sid: str, summary: dict[str, Any]) -> str:
    """The verdict the report stands behind, re-derived from the capture oracles.

    A recorded ``faithful`` is kept only if the scenario claims something and the CAPTURE
    oracles saw every claimed kind. Otherwise ``0 of 0`` would be a pass, whatever wrote
    the baseline (an older harness, a hand edit).
    """
    verdict = str(summary["verdict"])
    if verdict != "faithful":
        return verdict
    claims = SCENARIOS[sid].claims
    exercised = _live(summary["capture"])
    return "faithful" if claims and all(exercised[k] for k in claims) else "unmeasured"


def _surface_status(row: dict[str, Any]) -> str:
    if row["measured"] == 0:
        return "UNMEASURED"
    return "UNEXPECTED" if row["unexpected"] else "OK"


def build(
    baselines: list[dict[str, Any]], sources: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Aggregate *baselines* (parsed baseline JSON, one per corpus session) by surface."""
    runs: dict[str, list[dict[str, Any]]] = {}
    mismatched: list[str] = []
    confusion: dict[str, Counter[str]] = {}
    disagreements: Counter[tuple[Any, ...]] = Counter()
    first: dict[tuple[Any, ...], dict[str, Any]] = {}
    for index, baseline in enumerate(baselines):
        for sid, summary in sorted(baseline["scenarios"].items()):
            if sid not in SCENARIOS:
                raise ValueError(f"unknown scenario {sid!r} in baseline {index}")
            if sorted(summary.get("expected") or SCENARIOS[sid].expected) != sorted(
                SCENARIOS[sid].expected
            ):
                mismatched.append(sid)
            verdict = effective_verdict(sid, summary)
            summary = {**summary, "verdict": verdict}
            runs.setdefault(sid, []).append(
                {"run": index, "verdict": verdict, "metrics": metrics(sid, summary)}
            )
            for kind, entry in summary["e7"].items():
                _tally(confusion, kind, entry["oracle"], entry["reported"])
            for d in _e7_disagreements(sid, summary):
                key = (
                    d["sid"],
                    d["kind"],
                    str(d["oracle"]),
                    str(d["reported"]),
                    d["acknowledged"],
                    d["declared_limitation"],
                )
                disagreements[key] += 1
                first.setdefault(key, d)

    surfaces: dict[str, dict[str, Any]] = {}
    scenarios: dict[str, dict[str, Any]] = {}
    for sid, scenario_runs in sorted(runs.items(), key=lambda kv: _sid_order(kv[0])):
        scenario = SCENARIOS[sid]
        verdicts = Counter(r["verdict"] for r in scenario_runs)
        scenarios[sid] = {
            "surface": scenario.surface,
            "title": scenario.title,
            "preregistered": sorted(scenario.expected),
            "claims": list(scenario.claims),
            "verdicts": dict(sorted(verdicts.items())),
            "runs": scenario_runs,
        }
        row = surfaces.setdefault(
            scenario.surface,
            {
                "scenarios": [],
                "preregistered": set(),
                "attempted": 0,
                "executed": 0,
                "measured": 0,
                "unmeasured": 0,
                "faithful": 0,
                "correctly_refused": 0,
                "live_reported": 0,
                "diverged": 0,
                "unexpected": 0,
                "met_preregistration": 0,
                "verdicts": Counter(),
                "_wall": [],
            },
        )
        row["scenarios"].append(sid)
        row["preregistered"] |= set(scenario.expected)
        for r in scenario_runs:
            verdict = r["verdict"]
            row["attempted"] += 1
            row["executed"] += int(r["metrics"]["e1"]["executed"])
            if r["metrics"]["e1"]["executed"]:
                row["_wall"].append(r["metrics"]["e6"]["wall_s"])
            row["verdicts"][verdict] += 1
            if verdict == "unmeasured":
                row["unmeasured"] += 1
                continue
            row["measured"] += 1
            if verdict not in scenario.expected:
                row["unexpected"] += 1
                continue
            row["met_preregistration"] += 1
            row["faithful"] += verdict == "faithful"
            row["correctly_refused"] += verdict == "refused"
            row["live_reported"] += verdict == "live-reported"
        row["diverged"] += sum(r["verdict"] == "diverged" for r in scenario_runs)

    for row in surfaces.values():
        wall = row.pop("_wall")
        row["replay_wall_s"] = {
            "n": len(wall),
            "mean": round(sum(wall) / len(wall), 3) if wall else None,
            "max": max(wall) if wall else None,
        }
        row["preregistered"] = sorted(row["preregistered"])
        row["verdicts"] = dict(sorted(row["verdicts"].items()))
        interval = wilson(row["met_preregistration"], row["measured"])
        row["wilson95"] = None if interval is None else list(interval)
        row["status"] = _surface_status(row)

    inconsistent = sorted(sid for sid, s in scenarios.items() if len(s["verdicts"]) > 1)
    prov = _common_provenance(baselines)
    controls = _controls(baselines)
    missing_controls = sorted(n for n, c in controls.items() if c["missing_runs"])
    problems: list[str] = []
    if not surfaces:
        problems.append("no scenarios in the baselines")
    for name, row in surfaces.items():
        if row["status"] != "OK":
            problems.append(f"surface {name}: {row['status']}")
        if row["unmeasured"]:
            problems.append(f"surface {name}: {row['unmeasured']} unmeasured run(s)")
    problems += [f"scenario {sid}: verdict differs between runs" for sid in inconsistent]
    problems += [
        f"scenario {sid}: pre-registration differs from the baseline's"
        for sid in sorted(set(mismatched))
    ]
    for name, c in controls.items():
        if c["status"] != "OK":
            problems.append(f"control {name}: {c['status']}")
    problems += _provenance_problems(prov)
    return {
        "issue": "#59",
        "schema": 2,
        "runs": len(baselines),
        "sources": sources or [],
        "provenance": prov if isinstance(prov, dict) else None,
        "oracle_scope": list(ORACLE_SCOPE),
        "headline": _headline(surfaces),
        "surfaces": surfaces,
        "scenarios": scenarios,
        "e7_confusion": _matrix(confusion),
        "e7_confusion_controls": _control_confusion(baselines),
        "e7_disagreements": [
            {**first[key], "runs": count} for key, count in sorted(disagreements.items())
        ],
        "inconsistent_across_runs": inconsistent,
        "preregistration_mismatch": sorted(set(mismatched)),
        "controls": controls,
        "required_controls_missing": missing_controls,
        "controls_ok": all(c["status"] == "OK" for c in controls.values()),
        "problems": problems,
        "ok": not problems,
    }


def _headline(surfaces: dict[str, dict[str, Any]]) -> dict[str, int]:
    """How many surfaces met their pre-registration, and HOW (a refusal is not a replay)."""
    classes: Counter[str] = Counter()
    for row in surfaces.values():
        seen = set(row["verdicts"]) - {"unmeasured"}
        if row["measured"] == 0:
            classes["unmeasured"] += 1
        elif row["unexpected"]:
            classes["unexpected"] += 1
        elif seen == {"faithful"}:
            classes["faithful"] += 1
        elif seen == {"refused"}:
            classes["correctly_refused"] += 1
        elif seen == {"live-reported"}:
            classes["live_reported"] += 1
        else:
            classes["mixed"] += 1
    keys = ("faithful", "correctly_refused", "live_reported", "mixed", "unexpected", "unmeasured")
    return {
        "surfaces": len(surfaces),
        "met_preregistration": sum(r["status"] == "OK" for r in surfaces.values()),
        **{k: classes[k] for k in keys},
    }


def _provenance_problems(prov: Any) -> list[str]:
    if not isinstance(prov, dict):
        return ["provenance missing: the result is not pinned to a source and runtime"]
    out: list[str] = []
    if not (isinstance(prov.get("git_sha"), str) and _GIT_SHA.fullmatch(prov["git_sha"])):
        out.append("provenance: git_sha is not a 40-hex commit")
    if prov.get("git_dirty") is not False:
        out.append("provenance: the source tree was dirty or its state unknown")
    for key in ("novafabric", "python", "implementation", "platform", "machine"):
        if not (isinstance(prov.get(key), str) and prov[key]):
            out.append(f"provenance: {key} missing")
    deps = prov.get("dependencies")
    if not (isinstance(deps, dict) and deps and all(isinstance(v, str) for v in deps.values())):
        out.append("provenance: dependency versions missing")
    fixtures = prov.get("fixture_sha256")
    if not (
        isinstance(fixtures, dict)
        and fixtures
        and all(isinstance(v, str) and _SHA256.fullmatch(v) for v in fixtures.values())
    ):
        out.append("provenance: fixture hashes missing or malformed")
    if not isinstance(prov.get("replay_budget_s"), (int, float)):
        out.append("provenance: replay budget missing")
    return out


def _pinned(prov: Any) -> str:
    if not isinstance(prov, dict):
        return "null"
    return json.dumps({k: prov.get(k) for k in PINNED_PROVENANCE}, sort_keys=True)


def _common_provenance(baselines: list[dict[str, Any]]) -> Any:
    """The provenance every session shares; sessions that differ are never merged."""
    provs = [b.get("provenance") for b in baselines]
    if len({_pinned(p) for p in provs}) > 1:
        differing = [
            k
            for k in PINNED_PROVENANCE
            if len(
                {
                    json.dumps(p.get(k) if isinstance(p, dict) else None, sort_keys=True)
                    for p in provs
                }
            )
            > 1
        ]
        raise MixedProvenanceError(
            "baselines carry different provenance (" + ", ".join(differing or ["missing"]) + "); "
            "report them separately (partition_by_provenance)"
        )
    return provs[0] if provs else None


def partition_by_provenance(baselines: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group *baselines* into sets that may be combined, in order of first appearance."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for baseline in baselines:
        groups.setdefault(_pinned(baseline.get("provenance")), []).append(baseline)
    return list(groups.values())


_CELLS = ("tp", "fn", "fp", "tn", "no_counter", "no_counter_oracle_positive")

#: The ``replay_result.yaml`` counter that answers each oracle kind (none for side effects).
_COUNTER_KEY = {
    "model": "model_calls_live",
    "tool": "tool_calls_live",
    "network": "network_connections_live",
}


def _tally(matrix: dict[str, Counter[str]], kind: str, oracle: int, reported: int | None) -> None:
    """One oracle-vs-counter observation into *matrix*; ``reported=None``: no such counter."""
    cell = matrix.setdefault(kind, Counter())
    oracle_hit = oracle > 0
    if reported is None:
        cell["no_counter"] += 1
        cell["no_counter_oracle_positive"] += oracle_hit
        return
    counter_hit = reported > 0
    cell["tp"] += oracle_hit and counter_hit
    cell["fn"] += oracle_hit and not counter_hit
    cell["fp"] += counter_hit and not oracle_hit
    cell["tn"] += not oracle_hit and not counter_hit


def _matrix(matrix: dict[str, Counter[str]]) -> dict[str, dict[str, int]]:
    return {kind: {k: int(cell[k]) for k in _CELLS} for kind, cell in sorted(matrix.items())}


def _control_confusion(baselines: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """E7 over the perturbation controls: the oracle's live count vs the report's counter.

    Controls are built to make the oracle see something the report may not, so they are kept
    apart from the corpus matrix; a control with no oracle data (the report-only mutation) is
    skipped.
    """
    matrix: dict[str, Counter[str]] = {}
    for baseline in baselines:
        for control in (baseline.get("controls") or {}).values():
            live, report = control.get("live"), control.get("report")
            if not live or not report:
                continue
            for kind, key in _COUNTER_KEY.items():
                _tally(matrix, kind, live[kind], report.get(key))
    return _matrix(matrix)


def _sid_order(sid: str) -> tuple[int, str]:
    digits = "".join(c for c in sid if c.isdigit())
    return (int(digits) if digits else 0, sid)


def _control_run_ok(name: str, record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    verdict = record.get("verdict", record.get("oracle"))
    expected = REQUIRED_CONTROLS.get(name)
    if expected is None:  # not pre-registered: at least it must not have come back faithful
        return verdict is not None and verdict != "faithful"
    if verdict not in expected:
        return False
    # the "replace the oracle with the report's counters" mutation must actually flip it
    return not (name == "report-only:S6b" and record.get("report_only") == verdict)


def _controls(baselines: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Every required control, in every session: present, executed, and not faithful.

    A control passes when its verdict is one its pre-registration accepts. A required control
    absent from a session is ``MISSING``; one outside its set (``faithful`` above all) is
    ``FAILED``. Nothing here is skipped silently.
    """
    names = sorted(
        set(REQUIRED_CONTROLS) | {n for b in baselines for n in (b.get("controls") or {})}
    )
    out: dict[str, dict[str, Any]] = {}
    for name in names:
        verdicts: Counter[str] = Counter()
        runs = bad = 0
        for baseline in baselines:
            record = (baseline.get("controls") or {}).get(name)
            if record is None:
                continue
            runs += 1
            raw = record.get("verdict", record.get("oracle")) if isinstance(record, dict) else None
            verdicts[str(raw)] += 1
            bad += not _control_run_ok(name, record)
        missing = len(baselines) - runs
        status = "FAILED" if bad else ("MISSING" if missing else "OK")
        out[name] = {
            "runs": runs,
            "verdicts": dict(sorted(verdicts.items())),
            "expected": sorted(REQUIRED_CONTROLS.get(name, {"anything but faithful"})),
            "bad_runs": bad,
            "missing_runs": missing,
            "status": status,
            "all_runs_flagged_not_faithful": "faithful" not in verdicts,
        }
    return out


# ── retained evidence ────────────────────────────────────────────────────────


def sha256_file(path: Path, *, max_bytes: int = MAX_BASELINE_BYTES) -> str:
    """sha256 of a regular file, streamed; refuses more than *max_bytes*."""
    digest, total = hashlib.sha256(), 0
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            total += len(chunk)
            if total > max_bytes:
                raise EvidenceError(f"{path.name}: more than {max_bytes} bytes")
            digest.update(chunk)
    return digest.hexdigest()


def _need_posix() -> None:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise EvidenceError("evidence handling needs O_NOFOLLOW and directory descriptors (POSIX)")


_BASE = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)


def _open_dir(name: str | Path, dir_fd: int | None = None) -> int:
    """Open a directory without following a symlink in its last component."""
    flags = _BASE | os.O_NOFOLLOW | os.O_DIRECTORY
    return os.open(name, flags, dir_fd=dir_fd) if dir_fd is not None else os.open(name, flags)


def _lstat(name: str, dir_fd: int) -> os.stat_result:
    return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)


def _open_regular(name: str, dir_fd: int, max_bytes: int) -> int:
    """Open *name* for reading only if it is a bounded regular file, never via a symlink.

    ``O_NOFOLLOW`` makes a swapped-in symlink fail at open time (not merely at an earlier
    check); ``O_NONBLOCK`` keeps a FIFO from blocking; the post-open ``fstat`` is the check
    that counts, because it describes the object actually opened.
    """
    flags = _BASE | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(name, flags, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise EvidenceError(f"{name}: not a regular file")
        if st.st_size > max_bytes:
            raise EvidenceError(f"{name}: {st.st_size} bytes exceeds the {max_bytes}-byte limit")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _stream(fd: int, max_bytes: int, sink: Any = None) -> tuple[str, int]:
    """Hash (and optionally copy via *sink*) what *fd* holds, never reading past *max_bytes*."""
    digest, total = hashlib.sha256(), 0
    while True:
        chunk = os.read(fd, _CHUNK)
        if not chunk:
            return digest.hexdigest(), total
        total += len(chunk)
        if total > max_bytes:  # grew while we read it
            raise EvidenceError(f"more than {max_bytes} bytes")
        digest.update(chunk)
        if sink is not None:
            sink(chunk)


def _walk(root_fd: int, unsafe: Any) -> Any:
    """Yield ``(rel, parent_fd, name)`` for every evidence candidate under *root_fd*.

    Only the known layout is descended (scenario / run-kind / run-id), and only through
    non-symlink directories. Anything with an evidence name that is a symlink or not a
    regular file goes to ``unsafe(rel, reason)``; unrelated entries (home, xdg, work, the
    audit log, ``.env``) are never opened.
    """

    def regular_or_unsafe(rel: str, fd: int, name: str) -> bool:
        mode = _lstat(name, fd).st_mode
        if stat.S_ISLNK(mode):
            unsafe(rel, "symlink")
            return False
        if not stat.S_ISREG(mode):
            unsafe(rel, "not a regular file")
            return False
        return True

    def directory_or_unsafe(rel: str, fd: int, name: str) -> int | None:
        mode = _lstat(name, fd).st_mode
        if stat.S_ISLNK(mode):
            unsafe(rel, "symlink")
            return None
        if not stat.S_ISDIR(mode):
            unsafe(rel, "not a directory")
            return None
        return _open_dir(name, fd)

    for scenario in sorted(os.listdir(root_fd)):
        mode = _lstat(scenario, root_fd).st_mode
        if stat.S_ISLNK(mode):
            unsafe(scenario, "symlink")
            continue
        if not stat.S_ISDIR(mode) or not re.fullmatch(_SEG, scenario):
            continue
        sfd = _open_dir(scenario, root_fd)
        try:
            for name in sorted(os.listdir(sfd)):
                rel = f"{scenario}/{name}"
                if _TOP_FILE.fullmatch(name):
                    if regular_or_unsafe(rel, sfd, name):
                        yield rel, sfd, name
                elif name in _RUN_FILES:
                    kfd = directory_or_unsafe(rel, sfd, name)
                    if kfd is None:
                        continue
                    try:
                        for run in sorted(os.listdir(kfd)):
                            run_rel = f"{rel}/{run}"
                            if not re.fullmatch(_SEG, run):
                                continue
                            rfd = directory_or_unsafe(run_rel, kfd, run)
                            if rfd is None:
                                continue
                            try:
                                for fname in sorted(os.listdir(rfd)):
                                    if fname in _RUN_FILES[name] and regular_or_unsafe(
                                        f"{run_rel}/{fname}", rfd, fname
                                    ):
                                        yield f"{run_rel}/{fname}", rfd, fname
                            finally:
                                os.close(rfd)
                    finally:
                        os.close(kfd)
        finally:
            os.close(sfd)


def collect_evidence(
    root: Path,
    dest: Path,
    *,
    max_file_bytes: int = MAX_EVIDENCE_FILE_BYTES,
    max_total_bytes: int = MAX_EVIDENCE_TOTAL_BYTES,
    max_files: int = MAX_EVIDENCE_FILES,
) -> dict[str, str]:
    """Copy the raw oracle files under *root* into *dest*; return ``{relative path: sha256}``.

    *root* and *dest* are the caller's trusted paths; everything below *root* is not.
    Raises ``EvidenceError`` (and removes what it already copied) on a symlink or special
    file with an evidence name, a file or total over its limit, too many files, or an
    existing destination file -- evidence is never overwritten.
    """
    _need_posix()
    manifest: dict[str, str] = {}
    created: list[Path] = []
    total = 0

    def unsafe(rel: str, why: str) -> None:
        raise EvidenceError(f"{rel}: {why}; refusing to collect it")

    root_fd = _open_dir(os.path.realpath(root))
    try:
        for rel, parent_fd, name in _walk(root_fd, unsafe):
            if len(manifest) >= max_files:
                raise EvidenceError(f"more than {max_files} evidence files")
            src = _open_regular(name, parent_fd, max_file_bytes)
            try:
                target = Path(dest) / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _BASE
                try:
                    out = os.open(target, flags, 0o640)
                except FileExistsError as exc:
                    raise EvidenceError(
                        f"{rel}: destination exists; evidence is not overwritten"
                    ) from exc
                created.append(target)
                with os.fdopen(out, "wb", closefd=True) as fh:
                    digest, size = _stream(src, max_file_bytes, fh.write)
            finally:
                os.close(src)
            total += size
            if total > max_total_bytes:
                raise EvidenceError(f"total evidence exceeds {max_total_bytes} bytes")
            manifest[rel] = digest
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    finally:
        os.close(root_fd)
    return manifest


def _canonical(rel: object) -> bool:
    return (
        isinstance(rel, str)
        and 0 < len(rel) <= 256
        and _EVIDENCE.fullmatch(rel) is not None
        and not any(part in ("", ".", "..") for part in rel.split("/"))
    )


def _hash_confined(root_fd: int, rel: str, max_bytes: int) -> str | None:
    """sha256 of *rel* under *root_fd*, opened one non-symlink component at a time."""
    parts = rel.split("/")
    fds: list[int] = []
    try:
        fd = root_fd
        for part in parts[:-1]:
            fd = _open_dir(part, fd)
            fds.append(fd)
        src = _open_regular(parts[-1], fd, max_bytes)
        try:
            return _stream(src, max_bytes)[0]
        finally:
            os.close(src)
    except (OSError, EvidenceError):
        return None
    finally:
        for fd in reversed(fds):
            os.close(fd)


def verify_evidence(
    dest: Path,
    manifest: dict[str, str],
    *,
    strict: bool = True,
    max_file_bytes: int = MAX_EVIDENCE_FILE_BYTES,
) -> list[str]:
    """Manifest entries whose retained file is missing, unsafe, oversized or changed.

    Every manifest path must be canonical and have the evidence layout; it is opened
    relative to *dest* one non-symlink component at a time, so a crafted entry cannot name
    a file outside it. With *strict*, evidence files in *dest* that the manifest does not
    list are reported as ``unlisted:<path>``.
    """
    if not isinstance(manifest, dict):
        return ["<manifest>"]
    _need_posix()
    bad: list[str] = []
    try:
        root_fd = _open_dir(dest)
    except OSError:
        return sorted(str(rel) for rel in manifest)
    try:
        for rel, digest in manifest.items():
            if (
                not _canonical(rel)
                or not isinstance(digest, str)
                or not _SHA256.fullmatch(digest)
                or _hash_confined(root_fd, rel, max_file_bytes) != digest
            ):
                bad.append(str(rel))
        if strict:
            listed = {rel for rel in manifest if isinstance(rel, str)}

            def flag(rel: str, why: str) -> None:
                bad.append(f"unsafe:{rel}")

            bad += [f"unlisted:{rel}" for rel, _fd, _n in _walk(root_fd, flag) if rel not in listed]
    finally:
        os.close(root_fd)
    return sorted(set(bad))


def provenance(repo: Path | None = None) -> dict[str, Any]:
    """Exact version, commit, runtime, platform and fixture revision of this session."""
    here = Path(__file__).resolve().parent
    repo = repo or here.parents[2]

    def git(*args: str) -> str | None:
        try:
            done = subprocess.run(  # noqa: S603 -- fixed argv, no shell
                ["git", "-C", str(repo), *args],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    def version(dist: str) -> str | None:
        try:
            return importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            return None

    fixtures = {
        p.name: sha256_file(p)
        for p in sorted(here.glob("*.py"))
        if p.name != "__init__.py" and not p.name.startswith("test_")
    }
    status = git("status", "--porcelain", "--untracked-files=no")
    return {
        "novafabric": version("novafabric"),
        "git_sha": git("rev-parse", "HEAD"),
        "git_dirty": None if status is None else bool(status),
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "dependencies": {d: version(d) for d in ("openai", "httpx", "pytest", "pyyaml")},
        "fixture_sha256": fixtures,
        "replay_budget_s": REPLAY_BUDGET_S,
    }


# ── rendering ────────────────────────────────────────────────────────────────


def _wall(w: dict[str, Any]) -> str:
    return "—" if not w["n"] else f"{w['mean']:.2f} / {w['max']:.2f}"


def _pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def render_markdown(report: dict[str, Any]) -> str:
    h = report["headline"]
    lines = [
        "# Replay fidelity — per-surface E1–E7 report (issue #59)",
        "",
        f"Overall: **{'OK' if report['ok'] else 'NOT OK'}** — {report['runs']} corpus session(s).",
        "",
    ]
    if report["problems"]:
        lines += ["Why not ok:", ""] + [f"- {p}" for p in report["problems"]] + [""]
    lines += [
        f"**Read this first.** {h['met_preregistration']} of {h['surfaces']} surfaces met their "
        f"pre-registered verdict: {h['faithful']} as faithful replay, {h['correctly_refused']} "
        f"as correctly refused, {h['live_reported']} as live-reported (activity the engine "
        f"disclosed). That is not {h['surfaces']}/{h['surfaces']} faithful replays, and not "
        "production validation.",
        "",
    ]
    prov = report["provenance"]
    if prov is None:
        lines += ["> Provenance: **missing** from the baseline — this report is not pinned.", ""]
    else:
        dirty = " (dirty tree)" if prov.get("git_dirty") else ""
        lines += [
            f"Version {prov.get('novafabric')} · commit `{prov.get('git_sha')}`{dirty} · "
            f"Python {prov.get('python')} ({prov.get('implementation')}) · {prov.get('platform')}",
            "",
        ]
    lines += [
        "A surface passes when every measured run's verdict is in its pre-registered set. "
        "`measured` excludes runs whose capture oracles saw nothing to replay (`unmeasured`); "
        "the interval is Wilson 95% on `met pre-registration / measured`.",
        "",
        "| Surface | Pre-registered | Attempted | Executed | Measured | Faithful | Refused | "
        "Live-reported | Diverged | Unmeasured | Met pre-reg. | Wilson 95% | Replay wall s "
        "(mean / max) | Status |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, row in report["surfaces"].items():
        if row["wilson95"] is None:
            stat = "—"
        else:
            lo, hi = row["wilson95"]
            stat = f"{row['met_preregistration']}/{row['measured']} [{_pct(lo)}, {_pct(hi)}]"
        lines.append(
            f"| {name} | {', '.join(row['preregistered'])} | {row['attempted']} | "
            f"{row['executed']} | {row['measured']} | {row['faithful']} | "
            f"{row['correctly_refused']} | {row['live_reported']} | {row['diverged']} | "
            f"{row['unmeasured']} | {row['met_preregistration']} | {stat} | "
            f"{_wall(row['replay_wall_s'])} | **{row['status']}** |"
        )
    lines += [
        "",
        "## Scenarios",
        "",
        "| Id | Surface | Verdicts across runs | Pre-registered |",
        "|---|---|---|---|",
    ]
    for sid, s in report["scenarios"].items():
        verdicts = ", ".join(f"{v}×{n}" for v, n in s["verdicts"].items())
        lines.append(f"| {sid} | {s['surface']} | {verdicts} | {', '.join(s['preregistered'])} |")
    lines += ["", "## E7 — report vs oracle disagreements", ""]
    if report["e7_disagreements"]:
        lines += [
            "| Scenario | Kind | Oracle | replay_result.yaml | Acknowledged | "
            "Declared by the engine as a limitation | Runs |",
            "|---|---|---|---|---|---|---|",
        ]
        lines += [
            f"| {d['sid']} | {d['kind']} | {d['oracle']} | {d['reported']} | "
            f"{d['acknowledged']} | {d['declared_limitation']} | {d['runs']} |"
            for d in report["e7_disagreements"]
        ]
    else:
        lines.append("None observed.")
    lines += [
        "",
        "## E7 — confusion matrix per counter (oracle vs `replay_result.yaml`, all runs)",
        "",
        "`fn` is the dangerous cell: the oracle saw the activity and the counter says 0. "
        "`no counter` means the report has no counter for that kind (side effects today).",
        "",
        "| Kind | tp | fn | fp | tn | no counter | of which oracle saw activity |",
        "|---|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {kind} | {c['tp']} | {c['fn']} | {c['fp']} | {c['tn']} | {c['no_counter']} | "
        f"{c['no_counter_oracle_positive']} |"
        for kind, c in report["e7_confusion"].items()
    ]
    lines += [
        "",
        "### The same, over the perturbation controls",
        "",
        "Each control makes the oracle see something the report may miss (a forced live "
        "model call, an unserved tool). A forced call goes to an unregistered host, so the "
        "engine cannot know it is a model call: that is the documented limit on calls the "
        "capture did not record, shown here as a cell, not argued away.",
        "",
        "| Kind | tp | fn | fp | tn | no counter |",
        "|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {kind} | {c['tp']} | {c['fn']} | {c['fp']} | {c['tn']} | {c['no_counter']} |"
        for kind, c in report["e7_confusion_controls"].items()
    ]
    lines += [
        "",
        "## Controls (every required control must run, in every session, with an accepted verdict)",
        "",
    ]
    for name, c in report["controls"].items():
        verdicts = ", ".join(f"{v}×{n}" for v, n in c["verdicts"].items()) or "no run"
        mark = "ok" if c["status"] == "OK" else f"**{c['status']}**"
        lines.append(f"- `{name}`: {verdicts} (accepted: {', '.join(c['expected'])}) — {mark}")
    if report["inconsistent_across_runs"]:
        lines += [
            "",
            f"Verdict differs between runs: {', '.join(report['inconsistent_across_runs'])}.",
        ]
    lines += ["", "## Scope of the oracles", ""]
    lines += [f"- {s}" for s in report["oracle_scope"]]
    if report["sources"]:
        lines += ["", "## Raw evidence", "", "| File | SHA-256 |", "|---|---|"]
        lines += [f"| `{s['path']}` | `{s['sha256']}` |" for s in report["sources"]]
    return "\n".join(lines) + "\n"


# ── command line ─────────────────────────────────────────────────────────────


def _read_baseline(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_BASELINE_BYTES:
        raise EvidenceError(f"{path.name}: larger than {MAX_BASELINE_BYTES} bytes")
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get("scenarios"), dict):
        raise EvidenceError(f"{path.name}: not a fidelity baseline")
    return data


def _numbered(path: Path, index: int, total: int) -> Path:
    return path if total == 1 else path.with_name(f"{path.stem}.part{index}{path.suffix}")


def main(argv: list[str] | None = None) -> int:
    """Exit 0 only for an evidence-grade OK report; 1 when not ok; 2 for unusable input."""
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("baselines", nargs="+", type=Path)
    ap.add_argument("--json", type=Path, dest="json_out")
    ap.add_argument("--md", type=Path, dest="md_out")
    ap.add_argument(
        "--verify",
        action="store_true",
        help="re-hash every retained evidence file next to each baseline "
        "(<baseline>.evidence/) and fail on any change",
    )
    ap.add_argument(
        "--partition",
        action="store_true",
        help="baselines with different provenance become independent reports "
        "(<name>.partN.<ext>) instead of an error",
    )
    args = ap.parse_args(argv)
    parsed: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    failures: list[str] = []
    try:
        for path in args.baselines:
            parsed.append(_read_baseline(path))
            sources.append(
                {"path": path.name, "sha256": sha256_file(path), "bytes": path.stat().st_size}
            )
            if args.verify:
                manifest = (parsed[-1].get("evidence") or {}).get("files") or {}
                evidence = path.with_name(path.stem + ".evidence")
                failures += [f"{path.name}: {r}" for r in verify_evidence(evidence, manifest)]
                if not manifest:
                    failures.append(f"{path.name}: no evidence manifest")
        index_of = {id(b): i for i, b in enumerate(parsed)}
        groups = partition_by_provenance(parsed) if args.partition else [parsed]
        reports = [build(g, [sources[index_of[id(b)]] for b in g]) for g in groups]
    except (EvidenceError, MixedProvenanceError, OSError, ValueError) as exc:
        print(f"fidelity report: {exc}", file=sys.stderr)
        return 2
    for i, report in enumerate(reports, 1):
        if args.json_out:
            _numbered(args.json_out, i, len(reports)).write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n"
            )
        if args.md_out:
            _numbered(args.md_out, i, len(reports)).write_text(render_markdown(report))
    for failure in failures:
        print(f"EVIDENCE MISMATCH {failure}", file=sys.stderr)
    return 0 if all(r["ok"] for r in reports) and not failures else 1


if __name__ == "__main__":
    sys.exit(main())
