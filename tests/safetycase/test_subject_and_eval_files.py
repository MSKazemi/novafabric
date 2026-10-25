"""The safety case names the capsule's real run and weighs every eval file (review misc-1-05).

``_run_id`` minted a fresh ULID for any capsule directory not named by a ULID (and
``/nonexistent`` hashed ``b""``), so the signed case attributed its claims to a run
that never existed and every rebuild named a different one. ``_resolve_eval`` read
only the alphabetically first ``eval-results/*.json``, so whoever chose the file names
chose the verdict.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novafabric.safetycase.compiler import NotACapsuleError, SafetyCaseCompiler
from novafabric.safetycase.models import BackingState

RUN = "01HXAY7M5JZ8R7K4P9DPBYK2WX"
TEMPLATE = "clymer-generic-v0"


def _capsule(root: Path, name: str, *, run_id: str | None = RUN) -> Path:
    cap = root / name
    cap.mkdir()
    if run_id is not None:
        (cap / "capsule.yaml").write_text(f"run_id: {run_id}\n")
    return cap


def _eval(cap: Path, name: str, *, successes: int, verdict: str) -> None:
    path = cap / "eval-results" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({
        "judges": ["a", "b"], "inter_judge_agreement": 0.9, "consensus_verdict": verdict,
        "sample_size": 100, "successes": successes,
    }))


def test_the_run_id_comes_from_capsule_yaml_not_the_directory_name(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, "my-capsule")
    first = SafetyCaseCompiler().build(cap, TEMPLATE)
    second = SafetyCaseCompiler().build(cap, TEMPLATE)
    assert first.subject.run_ids == [RUN] == second.subject.run_ids


def test_a_ulid_named_directory_without_a_manifest_still_names_itself(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, RUN, run_id=None)
    assert SafetyCaseCompiler().build(cap, TEMPLATE).subject.run_ids == [RUN]


@pytest.mark.parametrize("name", ["not-a-capsule", "missing"])
def test_a_directory_that_is_not_a_capsule_is_refused(tmp_path: Path, name: str) -> None:
    if name != "missing":
        _capsule(tmp_path, name, run_id=None)
    with pytest.raises(NotACapsuleError):
        SafetyCaseCompiler().build(tmp_path / name, TEMPLATE)


def test_a_manifest_without_a_valid_run_id_is_refused(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, "cap", run_id="not-a-ulid")
    with pytest.raises(NotACapsuleError):
        SafetyCaseCompiler().build(cap, TEMPLATE)


def _resolve_eval(cap: Path):  # type: ignore[no-untyped-def]
    c = SafetyCaseCompiler()
    return c._RESOLVERS["judge_aggregate"](c, cap)


def test_a_failing_eval_file_cannot_hide_behind_an_earlier_passing_one(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, RUN)
    _eval(cap, "a.json", successes=95, verdict="pass")
    _eval(cap, "z.json", successes=10, verdict="fail")
    resolved = _resolve_eval(cap)
    assert resolved.state is BackingState.UNSUPPORTED
    assert resolved.evidence.artifact_ref.path_in_bundle == "eval-results/z.json"


def test_the_verdict_does_not_depend_on_the_file_names(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, RUN)
    _eval(cap, "a.json", successes=10, verdict="fail")
    _eval(cap, "z.json", successes=95, verdict="pass")
    assert _resolve_eval(cap).state is BackingState.UNSUPPORTED


def test_all_passing_eval_files_stay_supported_with_stable_evidence_ids(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, RUN)
    _eval(cap, "a.json", successes=95, verdict="pass")
    _eval(cap, "b.json", successes=96, verdict="pass")
    case = SafetyCaseCompiler().build(cap, TEMPLATE)
    ids = [e.id for e in case.nodes if getattr(e, "id", "").startswith("E-eval")]
    assert ids and len(ids) == len(set(ids))
    assert _resolve_eval(cap).state is BackingState.SUPPORTED
