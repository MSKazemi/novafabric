"""``nova safety-case verify`` checks the case describes *this* capsule (review misc-1-03/04).

The verifier recomputed ``case_hash`` (an unkeyed self-hash anyone can recompute),
re-hashed the artifacts the case named, and required claims to have *some*
children or evidence ids. So a hand-forged all-SUPPORTED case passed with exit 0:
its subject was never compared with the capsule, ``evidence_ids: ["E-nope"]``
satisfied "no naked claims", and ``top_claim_id`` was never resolved. The
artifact paths came from the untrusted case file and were read without bound —
absolute paths and ``..`` included.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from novafabric.safetycase.compiler import SafetyCaseCompiler
from novafabric.safetycase.models import Claim, Evidence, SafetyCase
from novafabric.safetycase.verifier import verify_exit_code, verify_safety_case

RUN = "01HXAY7M5JZ8R7K4P9DPBYK2WX"


def _capsule(root: Path, run: str = RUN) -> Path:
    cap = root / run
    cap.mkdir(parents=True)
    (cap / "capsule.yaml").write_text(f"run_id: {run}\n")
    (cap / "completeness.json").write_text(
        '{"run_id": "%s", "generated_at": "2026-10-24T00:00:00Z"}' % run
    )
    return cap


def _case(cap: Path) -> SafetyCase:
    return SafetyCaseCompiler().build(cap, "clymer-generic-v0")


def _rehash(case: SafetyCase, **update: object) -> SafetyCase:
    return case.model_copy(update=update).with_case_hash()


def test_a_genuine_case_verifies(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    verdict = verify_safety_case(_case(cap), cap)
    assert verdict.ok, verdict.summary()


def test_a_case_for_another_capsule_fails(tmp_path: Path) -> None:
    a = _capsule(tmp_path / "a")
    b = _capsule(tmp_path / "b", "01HXAY7M5JZ8R7K4P9DPBYK2WY")
    (b / "capsule.yaml").write_text("run_id: 01HXAY7M5JZ8R7K4P9DPBYK2WY\nstatus: other\n")
    (b / "completeness.json").write_bytes((a / "completeness.json").read_bytes())
    verdict = verify_safety_case(_case(a), b)
    assert not verdict.ok
    assert verify_exit_code(verdict) == 6
    assert "subject" in verdict.summary()


def test_an_edited_capsule_yaml_fails_the_subject(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    case = _case(cap)
    (cap / "capsule.yaml").write_text(f"run_id: {RUN}\nstatus: edited\n")
    assert verify_exit_code(verify_safety_case(case, cap)) == 6


def test_a_dangling_evidence_id_violates_i1(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    case = _case(cap)
    nodes = [
        n.model_copy(update={"evidence_ids": ["E-does-not-exist"]})
        if isinstance(n, Claim) and n.evidence_ids else n
        for n in case.nodes
    ]
    verdict = verify_safety_case(_rehash(case, nodes=nodes), cap)
    assert not verdict.ok
    assert verify_exit_code(verdict) == 5
    assert "E-does-not-exist" in verdict.summary()


def test_a_dangling_child_violates_i1(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    case = _case(cap)
    nodes = [
        n.model_copy(update={"children": ["C-nowhere"]})
        if isinstance(n, Claim) and n.children else n
        for n in case.nodes
    ]
    assert verify_exit_code(verify_safety_case(_rehash(case, nodes=nodes), cap)) == 5


def test_an_unresolvable_top_claim_fails(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    case = _rehash(_case(cap), top_claim_id="C-missing")
    verdict = verify_safety_case(case, cap)
    assert not verdict.ok
    assert verify_exit_code(verdict) == 5


@pytest.mark.parametrize("path", ["/etc/hostname", "../outside.json", "a/../../x"])
def test_an_artifact_path_outside_the_capsule_is_refused(tmp_path: Path, path: str) -> None:
    cap = _capsule(tmp_path)
    (tmp_path / "outside.json").write_text("{}")
    case = _case(cap)
    nodes = []
    for n in case.nodes:
        if isinstance(n, Evidence) and n.artifact_ref.path_in_bundle is not None:
            ref = n.artifact_ref.model_copy(update={"path_in_bundle": path})
            n = n.model_copy(update={"artifact_ref": ref})
        nodes.append(n)
    verdict = verify_safety_case(_rehash(case, nodes=nodes), cap)
    assert not verdict.ok
    assert verify_exit_code(verdict) == 4
    assert "refused" in verdict.summary()
