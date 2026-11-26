"""A criterion binding names a seal that verifies but does not bind the evidence streams.

``seal_unbound`` is the additive, non-blocking sibling of ``seal_invalid`` (see
``tests/compliance/audit/test_audit_unbound_seal_is_a_notice.py``): the seal verifies, so
it is still cited as evidence exactly as before, but it does not name some or all of the
evidence streams, so an edit to them would not be detected by the seal. A binding with no
finding still serialises without a ``findings`` key. Seal verification happens once per
capsule per ``build_bindings`` call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from compliance.export.test_seal_invalid_is_refused import (
    _damage,
    _sealed_capsule,
    _unsealed_capsule,
)
from typer.testing import CliRunner

from novafabric.cli.evidence import evidence_app
from novafabric.compliance.audit.engine import SEAL_UNBOUND_MESSAGE
from novafabric.compliance.audit.loader import load_profile
from novafabric.compliance.audit.models import BLOCKING_FINDING_CODES
from novafabric.evidence.binding import BindingFinding, CriterionBinding, build_bindings
from novafabric.trust.novaseal import capsule_verify

UNBOUND_SHAPES = {
    "legacy_seal": lambda root: _sealed_capsule(root, "cap", bind_evidence=False),
    "stream_added_after_sealing": lambda root: _sealed_capsule(
        root, "cap", late_streams=("tool-calls.jsonl",)
    ),
}


def _split(capsule: Path) -> tuple[list[CriterionBinding], list[CriterionBinding]]:
    """(bindings of controls asking for a seal, the rest) under the iso42001 profile."""
    profile = load_profile("iso42001")
    seal_controls = {
        c.id for c in profile.controls
        if any(r.evidence_type == "seal_signature" for r in c.evidence_requirements)
    }
    assert seal_controls, "the profile no longer asks for a seal"
    bindings = build_bindings(capsule, profile)
    return (
        [b for b in bindings if b.criterion_id in seal_controls],
        [b for b in bindings if b.criterion_id not in seal_controls],
    )


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "_home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)


def test_the_binding_finding_shares_the_one_blocking_notion() -> None:
    assert BindingFinding(code="seal_invalid", message="m").blocking is True
    assert BindingFinding(code="seal_unbound", message="m").blocking is False
    assert BLOCKING_FINDING_CODES == frozenset({"seal_invalid"})


@pytest.mark.parametrize("shape", UNBOUND_SHAPES)
def test_an_unbound_seal_is_a_finding_on_the_seal_controls_and_still_cited(
    tmp_path: Path, shape: str
) -> None:
    seal_bindings, other = _split(UNBOUND_SHAPES[shape](tmp_path))
    for b in seal_bindings:
        assert [f.code for f in b.findings] == ["seal_unbound"], b
        assert b.findings[0].message == SEAL_UNBOUND_MESSAGE
        # the seal verifies, so it is cited as before; the finding only qualifies it
        assert ".seal/manifest.dsse" in [r.path for r in b.evidence_refs]
    assert all(b.findings == [] for b in other)


def test_a_fully_bound_seal_carries_no_finding(tmp_path: Path) -> None:
    seal_bindings, other = _split(_sealed_capsule(tmp_path))
    assert all(b.findings == [] for b in [*seal_bindings, *other])
    assert ".seal/manifest.dsse" in {r.path for b in seal_bindings for r in b.evidence_refs}


def test_an_unsealed_capsule_carries_no_finding(tmp_path: Path) -> None:
    seal_bindings, other = _split(_unsealed_capsule(tmp_path))
    assert all(b.findings == [] for b in [*seal_bindings, *other])
    assert ".seal/manifest.dsse" not in {r.path for b in seal_bindings for r in b.evidence_refs}


def test_a_broken_seal_is_still_seal_invalid_and_not_cited(tmp_path: Path) -> None:
    cap = _sealed_capsule(tmp_path)
    _damage(cap, "edit-stream", tmp_path)
    seal_bindings, _ = _split(cap)
    for b in seal_bindings:
        assert [f.code for f in b.findings] == ["seal_invalid"]
        assert ".seal/manifest.dsse" not in [r.path for r in b.evidence_refs]


def test_a_binding_without_findings_still_omits_the_field(tmp_path: Path) -> None:
    seal_bindings, other = _split(_sealed_capsule(tmp_path))
    for b in [*seal_bindings, *other]:
        assert "findings" not in b.model_dump()
        assert "findings" not in json.loads(b.model_dump_json())


def test_an_unbound_binding_serialises_the_finding(tmp_path: Path) -> None:
    seal_bindings, _ = _split(UNBOUND_SHAPES["legacy_seal"](tmp_path))
    assert seal_bindings[0].model_dump()["findings"][0]["code"] == "seal_unbound"


def test_the_seal_is_verified_once_per_capsule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = UNBOUND_SHAPES["legacy_seal"](tmp_path)
    real = capsule_verify.verify_capsule
    calls: list[Path] = []

    def counting(capsule_dir: Path, *a: Any, **kw: Any) -> Any:
        calls.append(capsule_dir)
        return real(capsule_dir, *a, **kw)

    monkeypatch.setattr(capsule_verify, "verify_capsule", counting)
    bindings = build_bindings(cap, "nist-ai-rmf")
    assert sum(1 for b in bindings if b.findings) > 1  # several controls ask for the seal
    assert calls == [cap]


# -- the CLI -------------------------------------------------------------------------------


def _bind(capsule: Path, out: Path) -> Any:
    return CliRunner().invoke(
        evidence_app, ["bind", str(capsule), "--profile", "iso42001", "--output", str(out)]
    )


@pytest.mark.parametrize("shape", UNBOUND_SHAPES)
def test_evidence_bind_prints_a_notice_not_an_error_and_writes_the_finding(
    tmp_path: Path, shape: str
) -> None:
    cap = UNBOUND_SHAPES[shape](tmp_path)
    out = tmp_path / "bindings.json"
    res = _bind(cap, out)
    flat = " ".join(res.output.split())
    assert res.exit_code == 0, res.output
    assert "Notice" in flat and "does not name" in flat
    assert "Finding" not in flat and "failed seal verification" not in flat
    codes = {f["code"] for b in json.loads(out.read_text())["bindings"]
             for f in b.get("findings", [])}
    assert codes == {"seal_unbound"}


def test_evidence_bind_still_reports_an_invalid_seal_as_a_finding(tmp_path: Path) -> None:
    cap = _sealed_capsule(tmp_path)
    _damage(cap, "edit-stream", tmp_path)
    res = _bind(cap, tmp_path / "bindings.json")
    flat = " ".join(res.output.split())
    assert res.exit_code == 0, res.output
    assert "Finding" in flat and "failed seal verification" in flat
    assert "Notice" not in flat


def test_evidence_bind_says_nothing_for_a_bound_or_unsealed_capsule(tmp_path: Path) -> None:
    bound = _bind(_sealed_capsule(tmp_path), tmp_path / "a.json")
    unsealed = _bind(_unsealed_capsule(tmp_path), tmp_path / "b.json")
    for res in (bound, unsealed):
        assert res.exit_code == 0, res.output
        assert "Notice" not in res.output and "Finding" not in res.output
