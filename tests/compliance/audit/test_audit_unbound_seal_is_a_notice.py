"""A seal that verifies but does not bind the evidence streams is a visible NOTICE, never silent.

``seal_invalid`` (a ``.seal`` that fails verification) is blocking. A seal that VERIFIES
but cannot show edits to the evidence streams used to pass the audit with no finding at
all, while knowledge-graph ingest already treated the same capsule as invalid. Two shapes:

* a seal made before ADR-0251: no ``evidence_digests``, it signs the manifest only;
* a stream added to the capsule AFTER sealing: the seal does not name it.

Both now carry a ``seal_unbound`` finding. It is additive and NON-GATING: it never changes
an exit code and never changes ``gate_passed``. Which codes block is one named notion
(``BLOCKING_FINDING_CODES`` / ``AuditFinding.blocking``) that every consumer uses.

The finding says what the seal cannot show; it does not say the capsule was edited.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from compliance.export.test_seal_invalid_is_refused import (
    _damage,
    _sealed_capsule,
    _unsealed_capsule,
)
from novafabric.cli.audit import app as audit_app
from novafabric.compliance.audit import engine as engine_mod
from novafabric.compliance.audit.engine import SEAL_UNBOUND_MESSAGE, AuditEngine
from novafabric.compliance.audit.loader import load_profile
from novafabric.compliance.audit.models import (
    BLOCKING_FINDING_CODES,
    AuditFinding,
    AuditReport,
)
from novafabric.kg.capsule_ingest import capsule_event_files as capsule_event_files
from novafabric.trust.novaseal import capsule_verify

runner = CliRunner()

UNBOUND_SHAPES = {
    "legacy_seal": lambda root, name: _sealed_capsule(root, name, bind_evidence=False),
    "stream_added_after_sealing": lambda root, name: _sealed_capsule(
        root, name, late_streams=("tool-calls.jsonl",)
    ),
}


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "_home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)


def _data(tmp_path: Path, **capsules: Any) -> Path:
    """A data dir whose ``capsules/`` holds one capsule per keyword.

    Each value is a callable ``(build_root, name) -> capsule``; the capsule is built in a
    scratch directory (the sealer leaves a work dir beside it) and moved into place.
    """
    build = tmp_path / "build"
    build.mkdir(exist_ok=True)
    store = tmp_path / "data" / "capsules"
    store.mkdir(parents=True)
    for name, make in capsules.items():
        shutil.move(str(make(build, name)), store / name)
    return tmp_path / "data"


def _scan(data: Path) -> AuditReport:
    return AuditEngine(data, load_profile("iso42001")).scan()


def _bound(root: Path, name: str) -> Path:
    return _sealed_capsule(root, name)


def _broken(root: Path, name: str) -> Path:
    cap = _sealed_capsule(root, name)
    _damage(cap, "edit-stream", root)
    return cap


def _all_controls_covered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every control satisfied, so the only thing left to fail an audit is the seal."""
    monkeypatch.setattr(engine_mod, "_has_evidence", lambda _cd, _t: True)


# -- the one named notion of "blocking" ---------------------------------------------------


def test_only_seal_invalid_is_blocking() -> None:
    assert BLOCKING_FINDING_CODES == frozenset({"seal_invalid"})
    invalid = AuditFinding(code="seal_invalid", capsule_id="c", message="m")
    unbound = AuditFinding(code="seal_unbound", capsule_id="c", message="m")
    assert invalid.blocking is True
    assert unbound.blocking is False


def test_the_report_exposes_its_blocking_findings() -> None:
    report = AuditReport.model_construct(
        findings=[
            AuditFinding(code="seal_unbound", capsule_id="a", message="m"),
            AuditFinding(code="seal_invalid", capsule_id="b", message="m"),
        ]
    )
    assert [f.capsule_id for f in report.blocking_findings] == ["b"]
    only_unbound = AuditReport.model_construct(
        findings=[AuditFinding(code="seal_unbound", capsule_id="a", message="m")]
    )
    assert only_unbound.blocking_findings == []


# -- the engine ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", UNBOUND_SHAPES)
def test_an_unbound_seal_is_an_explicit_finding(tmp_path: Path, shape: str) -> None:
    report = _scan(_data(tmp_path, c1=UNBOUND_SHAPES[shape]))
    assert [(f.code, f.capsule_id) for f in report.findings] == [("seal_unbound", "c1")]
    assert report.findings[0].blocking is False
    # the seal still verifies, so it still counts as seal evidence in the score
    assert report.evidence_seal_verified is True


@pytest.mark.parametrize("shape", UNBOUND_SHAPES)
def test_the_shapes_really_are_what_they_claim(tmp_path: Path, shape: str) -> None:
    """Guard the fixtures: plain verification passes, coverage of the streams does not."""
    cap = UNBOUND_SHAPES[shape](tmp_path, "c")
    assert capsule_verify.capsule_seal_status(cap) == "valid"
    streams = capsule_event_files(cap)
    assert capsule_verify.capsule_seal_coverage(cap, must_cover=streams) == "unbound"


def test_a_fully_bound_seal_has_no_finding(tmp_path: Path) -> None:
    report = _scan(_data(tmp_path, c1=_bound))
    assert report.findings == []
    assert report.evidence_seal_verified is True


def test_an_unsealed_capsule_has_no_finding(tmp_path: Path) -> None:
    report = _scan(_data(tmp_path, c1=lambda root, name: _unsealed_capsule(root, name)))
    assert report.findings == []
    assert report.evidence_seal_verified is False


def test_a_broken_seal_is_still_seal_invalid_and_still_blocking(tmp_path: Path) -> None:
    report = _scan(_data(tmp_path, c1=_broken))
    assert [(f.code, f.blocking) for f in report.findings] == [("seal_invalid", True)]


def test_each_capsule_gets_its_own_finding(tmp_path: Path) -> None:
    data = _data(
        tmp_path,
        a_bound=_bound,
        b_legacy=UNBOUND_SHAPES["legacy_seal"],
        c_late=UNBOUND_SHAPES["stream_added_after_sealing"],
        d_broken=_broken,
        e_unsealed=lambda root, name: _unsealed_capsule(root, name),
    )
    report = _scan(data)
    assert [(f.capsule_id, f.code) for f in report.findings] == [
        ("b_legacy", "seal_unbound"),
        ("c_late", "seal_unbound"),
        ("d_broken", "seal_invalid"),
    ]
    assert [f.capsule_id for f in report.blocking_findings] == ["d_broken"]


def test_a_single_capsule_audit_reports_its_own_unbound_seal(tmp_path: Path) -> None:
    data = _data(tmp_path, c1=UNBOUND_SHAPES["legacy_seal"], c2=_bound)
    report = AuditEngine(
        data, load_profile("iso42001"), capsule_paths=[data / "capsules" / "c1"]
    ).scan()
    assert [(f.code, f.capsule_id) for f in report.findings] == [("seal_unbound", "c1")]


# -- the message says exactly what it can claim ---------------------------------------------


def test_the_unbound_message_claims_no_more_than_the_seal_shows() -> None:
    msg = " ".join(SEAL_UNBOUND_MESSAGE.split())
    lower = msg.lower()
    assert "verifies" in lower
    assert "does not name" in lower
    assert "would not be detected" in lower
    assert "does not say the capsule was edited" in lower
    assert "nova verify <capsule>" in msg
    for word in ("tamper", "failed", "forged", "invalid"):
        assert word not in lower, word


def test_the_finding_carries_that_message(tmp_path: Path) -> None:
    report = _scan(_data(tmp_path, c1=UNBOUND_SHAPES["legacy_seal"]))
    assert report.findings[0].message == SEAL_UNBOUND_MESSAGE


def test_the_invalid_message_points_at_the_unbound_finding() -> None:
    flat = " ".join(engine_mod.SEAL_INVALID_MESSAGE.split())
    assert "failed seal verification" in flat  # semantics unchanged
    assert "seal_unbound" in flat


# -- a capsule is verified once -----------------------------------------------------------


def _spy_on_verify(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    real = capsule_verify.verify_capsule
    calls: list[Path] = []

    def counting(capsule_dir: Path, *a: Any, **kw: Any) -> Any:
        calls.append(capsule_dir)
        return real(capsule_dir, *a, **kw)

    monkeypatch.setattr(capsule_verify, "verify_capsule", counting)
    return calls


@pytest.mark.parametrize("make", [*UNBOUND_SHAPES.values(), _bound, _broken])
def test_the_findings_verify_each_capsule_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make: Any
) -> None:
    cap = make(tmp_path, "c1")
    calls = _spy_on_verify(monkeypatch)
    engine_mod._seal_findings([cap])
    assert calls == [cap]


def test_a_capsule_edited_between_two_verifications_cannot_flip_the_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify genuinely once, then behave as if the capsule were edited meanwhile."""
    cap = _sealed_capsule(tmp_path, "c1", bind_evidence=False)
    real = capsule_verify.verify_capsule
    calls: list[Path] = []

    def flipping(capsule_dir: Path, *a: Any, **kw: Any) -> Any:
        calls.append(capsule_dir)
        if len(calls) == 1:
            return real(capsule_dir, *a, **kw)
        raise RuntimeError("the capsule changed between two verifications")

    monkeypatch.setattr(capsule_verify, "verify_capsule", flipping)
    findings = engine_mod._seal_findings([cap])
    assert [f.code for f in findings] == ["seal_unbound"]
    assert len(calls) == 1


# -- the CLI: a notice, never a gate ------------------------------------------------------


@pytest.mark.parametrize("shape", UNBOUND_SHAPES)
def test_audit_map_exit_is_unchanged_and_the_notice_is_printed(
    tmp_path: Path, shape: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _all_controls_covered(monkeypatch)
    data = _data(tmp_path, c1=UNBOUND_SHAPES[shape])
    res = runner.invoke(audit_app, ["map", "--data-dir", str(data), "--profile", "iso42001"])
    flat = " ".join(res.output.split())
    assert res.exit_code == 0, res.output
    assert "seal_unbound" in flat and "c1" in flat
    assert "does not name" in flat
    assert "Notice" in flat and "Finding" not in flat
    assert "Seals : verified" not in flat  # a seal that cannot show edits is not "verified"


def test_audit_map_still_exits_1_on_seal_invalid_beside_an_unbound_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _all_controls_covered(monkeypatch)
    data = _data(tmp_path, a=UNBOUND_SHAPES["legacy_seal"], b=_broken)
    res = runner.invoke(audit_app, ["map", "--data-dir", str(data), "--profile", "iso42001"])
    flat = " ".join(res.output.split())
    assert res.exit_code == 1, res.output
    assert "seal_invalid" in flat and "seal_unbound" in flat


def test_audit_map_json_carries_the_finding_and_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _all_controls_covered(monkeypatch)
    data = _data(tmp_path, c1=UNBOUND_SHAPES["legacy_seal"])
    res = runner.invoke(
        audit_app, ["map", "--data-dir", str(data), "--profile", "iso42001", "--json"]
    )
    assert res.exit_code == 0, res.output
    assert [f["code"] for f in json.loads(res.output)["findings"]] == ["seal_unbound"]


def test_audit_map_json_without_findings_still_has_no_unbound_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _all_controls_covered(monkeypatch)
    data = _data(tmp_path, c1=_bound)
    res = runner.invoke(
        audit_app, ["map", "--data-dir", str(data), "--profile", "iso42001", "--json"]
    )
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["findings"] == []


@pytest.mark.parametrize("shape", UNBOUND_SHAPES)
def test_audit_coverage_passes_with_only_an_unbound_seal_and_shows_it(
    tmp_path: Path, shape: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _all_controls_covered(monkeypatch)
    data = _data(tmp_path, c1=UNBOUND_SHAPES[shape])
    res = runner.invoke(
        audit_app, ["coverage", "--data-dir", str(data), "--profile", "iso42001"]
    )
    flat = " ".join(res.output.split())
    assert res.exit_code == 0, res.output
    assert "PASS" in flat and "FAIL" not in flat
    assert "seal_unbound" in flat and "Notice" in flat


def test_audit_coverage_still_fails_below_the_threshold_with_a_notice(
    tmp_path: Path,
) -> None:
    data = _data(tmp_path, c1=UNBOUND_SHAPES["legacy_seal"])
    res = runner.invoke(
        audit_app,
        ["coverage", "--data-dir", str(data), "--profile", "iso42001", "--threshold", "1.0"],
    )
    assert res.exit_code == 1, res.output
    assert "seal_unbound" in " ".join(res.output.split())


def test_audit_coverage_still_fails_on_seal_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _all_controls_covered(monkeypatch)
    data = _data(tmp_path, c1=_broken)
    res = runner.invoke(
        audit_app, ["coverage", "--data-dir", str(data), "--profile", "iso42001"]
    )
    flat = " ".join(res.output.split())
    assert res.exit_code == 1, res.output
    assert "failed seal verification" in flat and "FAIL" in flat


def test_audit_report_text_lists_the_notice_as_a_notice(tmp_path: Path) -> None:
    data = _data(tmp_path, c1=UNBOUND_SHAPES["legacy_seal"])
    res = runner.invoke(
        audit_app,
        ["report", "--data-dir", str(data), "--profile", "iso42001", "--format", "text"],
    )
    assert res.exit_code == 0, res.output
    flat = " ".join(res.output.split())
    assert "NOTICE seal_unbound" in flat and "c1" in flat
    assert "FINDING seal_unbound" not in flat
