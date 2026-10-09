"""ADR-0239 D3/D5/D6/D8 — the evidence-cart export route, end to end.

Acceptance criteria (design/spec/evidence-cart-v0.md §Acceptance), each pinned
by a test below:

* export requires ``admin`` scope and emits an audit record with the bundle
  digest — here both the Layer-B dashboard record **and** a hash-chained
  ``evidence.export`` entry whose chain still verifies;
* an unresolvable item fails explicitly (409, per item); no path yields a
  silently smaller bundle — accepting omissions records them in the bundle;
* the curation record always says ``curated`` and never claims exhaustiveness;
* held evidence exports, carries ``contains_held_evidence`` and the hold ids,
  and the export path never modifies a hold;
* the bundle verifies with the shipped ``nova verify`` — no new format;
* item count and capsule bytes are bounded, with the bound stated;
* an export the chained audit log cannot record is refused and leaves no file.
"""

from __future__ import annotations

import json
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.audit import AuditLog  # noqa: E402
from novafabric.capture.orchestrator import CaptureOrchestrator  # noqa: E402
from novafabric.serve import audit, token_store  # noqa: E402
from novafabric.serve.app import create_app  # noqa: E402
from novafabric.serve.routers import evidence_cart  # noqa: E402

SERVER_TOKEN = "server-token-0123456789abcdef"
HEADERS = {"host": "127.0.0.1:4321"}
URL = f"/api/evidence/cart/export?token={SERVER_TOKEN}"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    h = tmp_path / "home"
    (h / ".novafabric").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    monkeypatch.setenv("NOVAFABRIC_HOME", str(h / ".novafabric"))
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(h / "dashboard-audit.jsonl"))
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(h / "chained-audit.jsonl"))
    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_DIR", str(h / "evidence"))
    # The bundle builder's policy-decision entries resolve the same audit log
    # (one resolver since 2026-10-09), so they land in chained-audit.jsonl too.
    yield h


@pytest.fixture
def capsules(tmp_path: Path) -> Path:
    base = tmp_path / "runs"
    base.mkdir()
    return base


@pytest.fixture
def client(capsules: Path, home: Path) -> Iterator[TestClient]:
    app = create_app(token=SERVER_TOKEN, capsule_dir=capsules, static_mounted_by_caller=True)
    with TestClient(app) as c:
        yield c


def _run(capsules: Path) -> str:
    cap = CaptureOrchestrator(base_dir=capsules).run(command=[sys.executable, "-c", "pass"])
    return str(cap.capsule_dir.name)


def _item(ref: str, kind: str = "run") -> dict[str, Any]:
    return {
        "kind": kind,
        "ref": ref,
        "added_at": "2026-10-02T10:00:00Z",
        "added_by": "alice@example.org",
        "added_from": "?tab=runs&f=status%3Aerror",
    }


def _export(client: TestClient, items: list[dict[str, Any]], **extra: Any) -> Any:
    body = {"items": items, "confirmed": True, **extra}
    return client.post(URL, json=body, headers=HEADERS)


def _curation(bundle: Path) -> dict[str, Any]:
    with zipfile.ZipFile(bundle) as zf:
        return json.loads(zf.read("curation.json"))


def _chained(home: Path) -> list[dict[str, Any]]:
    path = home / "chained-audit.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


# ---------------------------------------------------------------------------
# D6 — admin scope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scope", ["read", "operate", "audit"])
def test_export_requires_admin_scope(
    client: TestClient, capsules: Path, scope: str
) -> None:
    a, b = _run(capsules), _run(capsules)
    secret = f"issued-{scope}-token-abcdefghijklmnop"
    token_store.issue(f"test-{scope}", secret, scope)
    r = client.post(
        f"/api/evidence/cart/export?token={secret}",
        json={"items": [_item(a), _item(b)], "confirmed": True},
        headers=HEADERS,
    )
    assert r.status_code == 403, r.text


def test_the_route_is_classified_admin() -> None:
    from novafabric.serve.authz import ROUTE_SCOPES, Scope

    assert ROUTE_SCOPES[("POST", "/api/evidence/cart/export")] is Scope.admin


# ---------------------------------------------------------------------------
# D3/D5 — one verifiable bundle that says it is curated
# ---------------------------------------------------------------------------


def test_a_cart_exports_one_bundle_the_shipped_verifier_accepts(
    client: TestClient, capsules: Path, home: Path
) -> None:
    import typer

    from novafabric.cli.verify import _verify_evidence_bundle

    a, b = _run(capsules), _run(capsules)
    r = _export(client, [_item(a), _item(b)])
    assert r.status_code == 200, r.text
    body = r.json()
    bundle = Path(body["bundle_path"])
    assert bundle.is_file()
    assert not bundle.with_suffix(".zip.partial").exists()
    assert body["capsule_count"] == 2
    assert body["unresolved"] == []
    assert body["cli_verify"] == f"nova verify {bundle}"
    try:
        _verify_evidence_bundle(bundle)
    except typer.Exit as exc:  # pragma: no cover - failure path
        raise AssertionError(f"shipped verifier rejected the cart bundle: {exc}") from exc


def test_the_curation_record_is_curated_never_exhaustive(
    client: TestClient, capsules: Path
) -> None:
    a, b = _run(capsules), _run(capsules)
    body = _export(client, [_item(a), _item(b)]).json()
    curation = _curation(Path(body["bundle_path"]))
    assert curation["completeness"]["claim"] == "curated"
    assert curation["exhaustive"] is False
    assert curation["operator_assembled"] is True
    assert curation["assembly"]["method"] == "operator-curated"
    provenance = curation["assembly"]["item_provenance"]
    assert [p["ref"] for p in provenance] == [a, b]
    assert all(p["added_from"] == "?tab=runs&f=status%3Aerror" for p in provenance)
    assert curation["assembly"]["assembled_by"][0]["identity_source"] == "shared-token"
    assert curation["contains_held_evidence"] is False


# ---------------------------------------------------------------------------
# D6 — audited: hash-chained and Layer B
# ---------------------------------------------------------------------------


def test_every_export_appends_a_chained_audit_entry_naming_the_digest(
    client: TestClient, capsules: Path, home: Path
) -> None:
    a, b = _run(capsules), _run(capsules)
    body = _export(client, [_item(a), _item(b)]).json()
    entries = [e for e in _chained(home) if e["event_type"] == "evidence.export"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["resource_id"] == f"sha256:{body['bundle_sha256']}"
    assert entry["entry_hash"] == body["audit_entry_hash"]
    assert entry["details"]["items"] == [
        {"kind": "run", "ref": a},
        {"kind": "run", "ref": b},
    ]
    assert AuditLog(home / "chained-audit.jsonl").verify() == []

    # A second export extends the same chain.
    _export(client, [_item(b), _item(a)])
    chained = _chained(home)
    assert chained[-1]["prev_hash"] == chained[-2]["entry_hash"]
    assert AuditLog(home / "chained-audit.jsonl").verify() == []


def test_the_dashboard_audit_records_the_export(client: TestClient, capsules: Path) -> None:
    a, b = _run(capsules), _run(capsules)
    body = _export(client, [_item(a), _item(b)]).json()
    records = [r for r in audit.read_recent(50) if r["action"] == "evidence_cart_export"]
    assert records, "no Layer-B audit record"
    rec = records[0]
    assert rec["result"] == "ok"
    assert rec["resource"] == f"sha256:{body['bundle_sha256']}"
    assert rec["required_scope"] == "admin"
    assert rec["extra"]["audit_entry_hash"] == body["audit_entry_hash"]


def test_no_audit_means_no_export(
    client: TestClient, capsules: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = _run(capsules), _run(capsules)
    unwritable = home / "audit-is-a-directory"
    unwritable.mkdir()
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(unwritable))
    r = _export(client, [_item(a), _item(b)])
    assert r.status_code == 503, r.text
    assert r.json()["error"] == "audit_unavailable"
    evidence = home / "evidence"
    assert not any(evidence.glob("*.zip")), "a bundle survived an unaudited export"
    assert not any(evidence.glob("*.partial"))


# ---------------------------------------------------------------------------
# §2.1 — unresolved is explicit, never a smaller bundle
# ---------------------------------------------------------------------------


def test_an_unresolvable_item_stops_the_export_and_names_it(
    client: TestClient, capsules: Path, home: Path
) -> None:
    a, b = _run(capsules), _run(capsules)
    r = _export(client, [_item(a), _item(b), _item("no-such-run")])
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "unresolved_items"
    assert [u["ref"] for u in body["unresolved"]] == ["no-such-run"]
    assert body["unresolved"][0]["unresolved_reason"]
    assert "remedy" in body
    assert not (home / "evidence").exists() or not any((home / "evidence").glob("*.zip"))


def test_accepting_omissions_records_them_in_the_bundle(
    client: TestClient, capsules: Path
) -> None:
    a, b = _run(capsules), _run(capsules)
    items = [_item(a), _item(b), _item("no-such-run"), _item("{\"x\":1}", kind="chart")]
    r = _export(client, items, accept_unresolved=True)
    assert r.status_code == 200, r.text
    body = r.json()
    assert {u["ref"] for u in body["unresolved"]} == {"no-such-run", "{\"x\":1}"}
    curation = _curation(Path(body["bundle_path"]))
    assert curation["item_count"] == 4
    assert curation["unresolved_count"] == 2
    assert curation["all_references_resolved"] is False
    chart = next(i for i in curation["items"] if i["kind"] == "chart")
    assert "no resolver" in chart["unresolved_reason"]


# ---------------------------------------------------------------------------
# D8 — held evidence exports, disclosed, and untouched
# ---------------------------------------------------------------------------


def test_held_evidence_exports_with_the_hold_disclosed_and_unchanged(
    client: TestClient, capsules: Path
) -> None:
    a, b = _run(capsules), _run(capsules)
    holds = capsules.parent / "registries" / "default" / "holds.jsonl"
    holds.parent.mkdir(parents=True)
    holds.write_text(
        json.dumps(
            {
                "hold_id": "hold-abc12345",
                "registry": "default",
                "reason": "litigation",
                "duration_days": None,
                "created_at": "2026-10-01T00:00:00+00:00",
                "released_at": None,
            }
        )
        + "\n"
    )
    before = holds.read_bytes()
    body = _export(client, [_item(a), _item(b)]).json()
    assert body["contains_held_evidence"] is True
    assert body["legal_holds"] == ["hold-abc12345"]
    curation = _curation(Path(body["bundle_path"]))
    assert curation["contains_held_evidence"] is True
    assert all(i["legal_holds"] == ["hold-abc12345"] for i in curation["items"])
    assert holds.read_bytes() == before, "the export path modified a hold"


# ---------------------------------------------------------------------------
# Bounds and input validation
# ---------------------------------------------------------------------------


def test_item_count_is_bounded(client: TestClient) -> None:
    items = [_item(f"r{i}") for i in range(evidence_cart.MAX_CART_ITEMS + 1)]
    r = _export(client, items)
    assert r.status_code == 413
    assert str(evidence_cart.MAX_CART_ITEMS) in r.json()["detail"]


def test_capsule_bytes_are_bounded(
    client: TestClient, capsules: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = _run(capsules), _run(capsules)
    monkeypatch.setattr(evidence_cart, "MAX_CART_CAPSULE_BYTES", 10)
    r = _export(client, [_item(a), _item(b)])
    assert r.status_code == 413
    assert "bounded at 10 bytes" in r.json()["detail"]


def test_a_one_run_cart_exports_a_schema_valid_bundle_that_verifies(
    client: TestClient, capsules: Path
) -> None:
    """Was a 422: the builder wrote ``subject`` as a 1-element array (schema needs >=2)."""
    import jsonschema
    import typer

    from novafabric.cli.verify import _verify_evidence_bundle

    a = _run(capsules)
    r = _export(client, [_item(a)])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["capsule_count"] == 1
    bundle = Path(body["bundle_path"])
    with zipfile.ZipFile(bundle) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    schema = json.loads(
        (Path(__file__).parents[2] / "src/novafabric/schemas/evidence-bundle.schema.json").read_text()
    )
    jsonschema.validate(manifest, schema)
    assert isinstance(manifest["subject"], dict)
    assert _curation(bundle)["completeness"]["claim"] == "curated"
    try:
        _verify_evidence_bundle(bundle)
    except typer.Exit as exc:  # pragma: no cover - failure path
        raise AssertionError(f"shipped verifier rejected the one-run bundle: {exc}") from exc


def test_a_one_run_cart_still_requires_admin_scope(
    client: TestClient, capsules: Path
) -> None:
    a = _run(capsules)
    secret = "issued-operate-token-abcdefghijklmnop"
    token_store.issue("test-operate-one", secret, "operate")
    r = client.post(
        f"/api/evidence/cart/export?token={secret}",
        json={"items": [_item(a)], "confirmed": True},
        headers=HEADERS,
    )
    assert r.status_code == 403, r.text


def test_a_cart_resolving_to_no_capsule_is_refused(client: TestClient) -> None:
    r = _export(client, [_item("does-not-exist")], accept_unresolved=True)
    assert r.status_code == 422
    assert "no capsule" in r.json()["detail"]


def test_confirmation_is_required(client: TestClient, capsules: Path) -> None:
    a, b = _run(capsules), _run(capsules)
    r = client.post(URL, json={"items": [_item(a), _item(b)]}, headers=HEADERS)
    assert r.status_code == 400


def test_an_unknown_kind_and_an_empty_cart_are_rejected(client: TestClient) -> None:
    assert _export(client, []).status_code == 422
    r = _export(client, [_item("x", kind="screenshot")])
    assert r.status_code == 422
    assert "allowed" in r.json()["detail"]
