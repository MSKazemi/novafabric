"""The HTTP routes show a ``seal_unbound`` finding without gating on it.

``GET /api/compliance/audit/coverage`` adds the finding to ``findings`` but
``gate_passed`` stays ``score >= threshold AND no BLOCKING finding``: only ``seal_invalid``
blocks. ``POST /api/evidence/{run_id}/bind`` carries it on the bindings of the controls
that ask for seal evidence (and omits ``findings`` when there is nothing to say).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from compliance.export.test_seal_invalid_is_refused import (
    _damage,
    _sealed_capsule,
    _unsealed_capsule,
)
from fastapi.testclient import TestClient

from novafabric.serve.app import create_app

TOKEN = "unbound-seal-token"
AUTH = {"token": TOKEN}
HOST = {"host": "127.0.0.1:4321"}

SHAPES = {
    "legacy_seal": lambda root, name: _sealed_capsule(root, name, bind_evidence=False),
    "stream_added_after_sealing": lambda root, name: _sealed_capsule(
        root, name, late_streams=("tool-calls.jsonl",)
    ),
}


@pytest.fixture()
def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Path]:
    monkeypatch.setenv("HOME", str(tmp_path / "_home"))
    monkeypatch.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)
    store = tmp_path / "runs"
    store.mkdir()
    app = create_app(token=TOKEN, capsule_dir=store, db_path=tmp_path / "registry.db")
    return TestClient(app), store


def _place(store: Path, build: Path, make: object, name: str = "cap") -> Path:
    """Build a capsule in a scratch dir (the sealer leaves a work dir) and move it in."""
    build.mkdir(exist_ok=True)
    cap = make(build, name)  # type: ignore[operator]
    dest = store / name
    shutil.move(str(cap), dest)
    return dest


def _coverage(client: TestClient, threshold: float = 0.0) -> dict:  # type: ignore[type-arg]
    resp = client.get(
        "/api/compliance/audit/coverage",
        params={**AUTH, "profile": "iso42001", "threshold": threshold}, headers=HOST,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()  # type: ignore[no-any-return]


def _bind(client: TestClient, run_id: str) -> dict:  # type: ignore[type-arg]
    resp = client.post(
        f"/api/evidence/{run_id}/bind", params=AUTH, headers=HOST,
        json={"profile": "iso42001"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True, body
    return body  # type: ignore[no-any-return]


# -- /api/compliance/audit/coverage ---------------------------------------------------------


@pytest.mark.parametrize("shape", SHAPES)
def test_an_unbound_seal_is_listed_and_does_not_gate(
    stack: tuple[TestClient, Path], tmp_path: Path, shape: str
) -> None:
    client, store = stack
    _place(store, tmp_path / "b", SHAPES[shape])
    body = _coverage(client)  # threshold 0.0: the score alone always passes
    assert [(f["code"], f["capsule_id"]) for f in body["findings"]] == [("seal_unbound", "cap")]
    assert body["gate_passed"] is True
    assert body["threshold_met"] is True


def test_an_unbound_seal_does_not_rescue_a_score_below_the_threshold(
    stack: tuple[TestClient, Path], tmp_path: Path
) -> None:
    client, store = stack
    _place(store, tmp_path / "b", SHAPES["legacy_seal"])
    body = _coverage(client, threshold=1.0)
    assert body["threshold_met"] is False and body["gate_passed"] is False
    assert [f["code"] for f in body["findings"]] == ["seal_unbound"]


def test_a_broken_seal_still_fails_the_gate_beside_an_unbound_one(
    stack: tuple[TestClient, Path], tmp_path: Path
) -> None:
    client, store = stack
    _place(store, tmp_path / "b", SHAPES["legacy_seal"], "unbound")

    def broken(root: Path, name: str) -> Path:
        cap = _sealed_capsule(root, name)
        _damage(cap, "edit-stream", root)
        return cap

    _place(store, tmp_path / "b", broken, "broken")
    body = _coverage(client)
    assert sorted((f["capsule_id"], f["code"]) for f in body["findings"]) == [
        ("broken", "seal_invalid"), ("unbound", "seal_unbound"),
    ]
    assert body["gate_passed"] is False


@pytest.mark.parametrize("make", [_sealed_capsule, _unsealed_capsule])
def test_bound_and_unsealed_capsules_leave_the_response_as_it_was(
    stack: tuple[TestClient, Path], tmp_path: Path, make: object
) -> None:
    client, store = stack
    _place(store, tmp_path / "b", make)
    body = _coverage(client)
    assert body["findings"] == []
    assert body["gate_passed"] is True


# -- POST /api/evidence/{run_id}/bind -------------------------------------------------------


@pytest.mark.parametrize("shape", SHAPES)
def test_bind_route_carries_the_unbound_finding(
    stack: tuple[TestClient, Path], tmp_path: Path, shape: str
) -> None:
    client, store = stack
    _place(store, tmp_path / "b", SHAPES[shape])
    bindings = _bind(client, "cap")["bindings"]
    with_findings = [b for b in bindings if "findings" in b]
    assert with_findings
    assert all([f["code"] for f in b["findings"]] == ["seal_unbound"] for b in with_findings)
    assert all(
        ".seal/manifest.dsse" in [r["path"] for r in b["evidence_refs"]] for b in with_findings
    )


@pytest.mark.parametrize("make", [_sealed_capsule, _unsealed_capsule])
def test_bind_route_omits_findings_when_there_is_nothing_to_report(
    stack: tuple[TestClient, Path], tmp_path: Path, make: object
) -> None:
    client, store = stack
    _place(store, tmp_path / "b", make)
    assert all("findings" not in b for b in _bind(client, "cap")["bindings"])


def test_bind_route_still_reports_an_invalid_seal(
    stack: tuple[TestClient, Path], tmp_path: Path
) -> None:
    client, store = stack

    def broken(root: Path, name: str) -> Path:
        cap = _sealed_capsule(root, name)
        _damage(cap, "edit-stream", root)
        return cap

    _place(store, tmp_path / "b", broken)
    codes = {f["code"] for b in _bind(client, "cap")["bindings"] for f in b.get("findings", [])}
    assert codes == {"seal_invalid"}
