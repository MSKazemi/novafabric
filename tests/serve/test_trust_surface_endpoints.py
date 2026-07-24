"""ADR-0173/0174 trust-surface endpoints on `nova serve`.

The radar and X-Ray models were CLI-only, so nothing could consume them
programmatically and the capsule-detail glyphs had no data source.

The property that matters: these use the **same projection** the CLI renders.
Two code paths reporting a capsule's trust posture could disagree, and in this
subsystem — whose whole job is stating what is proven — a disagreement is
worse than exposing nothing at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from novafabric.serve.app import create_app

TOKEN = "t0ken"
_SECRET = "sk-ant-api03-averyrealsecretvalue0123456789"


def _capsule(root: Path, run_id: str, *, proof: dict | None = None) -> Path:
    cap = root / run_id
    cap.mkdir(parents=True)
    (cap / "capsule.yaml").write_text(
        yaml.dump({"run_id": run_id, "created_at": "2026-07-01T00:00:00Z"})
    )
    (cap / "trace.jsonl").write_text('{"event":"start"}\n')
    if proof is not None:
        (cap / "redaction-proof.json").write_text(json.dumps(proof))
    return cap


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    root = tmp_path / "capsules"
    root.mkdir()
    _capsule(
        root,
        "01HXAY7M5JZ8R7K4P9DPBYK2WX",
        proof={
            "capsule_run_id": "01HXAY7M5JZ8R7K4P9DPBYK2WX",
            "findings": [
                {"target_ref": "inputs.prompt", "redaction_strategy": "mask"},
                {"target_ref": "env.API_KEY", "action_taken": "scrub"},
            ],
        },
    )
    _capsule(root, "01HXAY7M5JZ8R7K4P9DPBYK2WY")  # no masking pipeline
    app = create_app(
        token=TOKEN, capsule_dir=root, db_path=tmp_path / "registry.db",
        static_mounted_by_caller=True,
    )
    return TestClient(app, base_url="http://localhost")


def _get(client: TestClient, path: str):
    return client.get(path, params={"token": TOKEN})


# ---------------------------------------------------------------------------
# Trust radar
# ---------------------------------------------------------------------------


def test_radar_endpoint_returns_the_axes(client: TestClient) -> None:
    r = _get(client, "/api/runs/01HXAY7M5JZ8R7K4P9DPBYK2WX/trust-radar")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["capsule_id"] == "01HXAY7M5JZ8R7K4P9DPBYK2WX"
    assert {a["key"] for a in body["axes"]} >= {"signature", "policy", "secret_scan"}


def test_radar_reports_na_not_fail_for_an_unsealed_capsule(client: TestClient) -> None:
    """Unverified is not failed — the distinction this subsystem exists for."""
    body = _get(client, "/api/runs/01HXAY7M5JZ8R7K4P9DPBYK2WX/trust-radar").json()
    seal_axes = [a for a in body["axes"] if a["key"] in ("signature", "log_integrity")]
    assert all(a["state"] == "na" for a in seal_axes)
    assert body["verdict"] == "unsealed"


def test_radar_matches_the_cli_projection(client: TestClient, tmp_path: Path) -> None:
    """One projection, two surfaces. If these diverge, one is lying."""
    from novafabric.trust.capsule_flags import flags_from_capsule
    from novafabric.trust.radar import build_trust_radar

    run_id = "01HXAY7M5JZ8R7K4P9DPBYK2WX"
    cdir = tmp_path / "capsules" / run_id
    expected = build_trust_radar(flags_from_capsule(cdir), capsule_id=run_id)
    assert _get(client, f"/api/runs/{run_id}/trust-radar").json() == expected.model_dump(
        mode="json"
    )


# ---------------------------------------------------------------------------
# Redaction X-Ray
# ---------------------------------------------------------------------------


def test_xray_endpoint_returns_paths_and_states(client: TestClient) -> None:
    body = _get(client, "/api/runs/01HXAY7M5JZ8R7K4P9DPBYK2WX/redaction-xray").json()
    assert {f["path"] for f in body["fields"]} == {"inputs.prompt", "env.API_KEY"}


def test_xray_never_returns_a_field_value(client: TestClient, tmp_path: Path) -> None:
    """ADR-0009: paths and states only. A value must never leave."""
    root = tmp_path / "capsules"
    _capsule(
        root,
        "01HXAY7M5JZ8R7K4P9DPBYK2WZ",
        proof={
            "findings": [
                {
                    "target_ref": "env.KEY",
                    "redaction_strategy": "mask",
                    "value": _SECRET,
                    "replacement": _SECRET,
                }
            ]
        },
    )
    body = _get(client, "/api/runs/01HXAY7M5JZ8R7K4P9DPBYK2WZ/redaction-xray")
    assert _SECRET not in body.text


def test_xray_without_a_masking_pipeline_is_empty_not_404(client: TestClient) -> None:
    """"Nothing was scanned" is a real answer, not a missing resource."""
    r = _get(client, "/api/runs/01HXAY7M5JZ8R7K4P9DPBYK2WY/redaction-xray")
    assert r.status_code == 200, r.text
    assert r.json()["fields"] == []


# ---------------------------------------------------------------------------
# Auth + unknown capsules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("surface", ["trust-radar", "redaction-xray"])
def test_endpoints_require_a_token(client: TestClient, surface: str) -> None:
    r = client.get(f"/api/runs/01HXAY7M5JZ8R7K4P9DPBYK2WX/{surface}")
    assert r.status_code == 401


@pytest.mark.parametrize("surface", ["trust-radar", "redaction-xray"])
def test_unknown_capsule_is_404(client: TestClient, surface: str) -> None:
    assert _get(client, f"/api/runs/01HXNOSUCHCAPSULE00000000/{surface}").status_code == 404
