"""Every ``nova serve`` request parameter that becomes a filesystem path is confined.

An audit of every route (path, query and body parameters) after the
``/api/assure?capsule_path=`` finding turned up more parameters that reached
the filesystem unchecked:

* ``profile`` on the four ``/api/compliance/audit/*`` routes and
  ``/api/evidence/{run_id}/bind``: ``load_profile`` reads any non-built-in value
  as a YAML path. With a read token this was an existence and parse oracle.
* ``node_id`` on ``/api/seal/ratchet/*``: ``<ratchet dir>/<node_id>.json``.
  ``../`` or an absolute value wrote or read a ``*.json`` anywhere. The
  read-scope status route also returned the forward-secure ``chain_key_hex``.
* ``capsule_id`` on ``/api/seal/sigstore/{sign,verify}`` and the
  ``/api/seal/{capsule_id}/...`` routes, ``run_id`` on ``/children`` and
  ``/validate-distributed``: a ``..`` segment escaped the base directory.
* ``run_id`` on ``/api/ingest-capsule``: an absolute path indexed a capsule
  from anywhere.
* ``cart_id`` on ``/api/evidence/cart/export``: joined into the output name.

Each one now refuses a ``..``, separator or absolute value (400/422). Values
meant to be paths go through the read confinement (403 outside the roots).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402
from novafabric.serve.paths import PATH_ROOTS_ENV  # noqa: E402

TOKEN = "param-confine-token-0123456789abcdef"
H = {"host": "127.0.0.1:4321", "authorization": f"Bearer {TOKEN}"}
RUN = "01J9ZZPARAMCONFINE00000001"
BAD_SEGMENTS = ["../escape", "..", "/abs/escape", "a/b"]


@dataclass
class Env:
    home: Path
    capsules: Path
    outside: Path
    client: TestClient


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    home = tmp_path / "home"
    (home / "exports").mkdir(parents=True)
    capsules = tmp_path / "capsules"
    (capsules / RUN).mkdir(parents=True)
    (capsules / RUN / "capsule.yaml").write_text(yaml.safe_dump({"run_id": RUN}))
    outside = tmp_path / "outside"
    (outside / "capsule").mkdir(parents=True)
    (outside / "capsule" / "capsule.yaml").write_text(yaml.safe_dump({"run_id": "elsewhere"}))
    (outside / "profile.yaml").write_text(yaml.safe_dump({"id": "x", "controls": []}))
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(home / "dash-audit.jsonl"))
    monkeypatch.setenv("NOVAFABRIC_RATCHET_DIR", str(home / "seal" / "ratchet"))
    monkeypatch.delenv(PATH_ROOTS_ENV, raising=False)
    app = create_app(token=TOKEN, capsule_dir=capsules, db_path=None, static_dir=None)
    with TestClient(app) as c:
        yield Env(home=home, capsules=capsules, outside=outside, client=c)


# -- compliance profile ---------------------------------------------------------


def test_profile_outside_the_roots_is_refused(env: Env) -> None:
    profile = str(env.outside / "profile.yaml")
    r = env.client.get("/api/compliance/audit/map", headers=H, params={"profile": profile})
    assert r.status_code == 403
    r = env.client.get("/api/compliance/audit/coverage", headers=H, params={"profile": profile})
    assert r.status_code == 403
    r = env.client.post(
        "/api/compliance/audit/report", headers=H, json={"run_id": RUN, "profile": profile}
    )
    assert r.status_code == 403
    r = env.client.post(f"/api/evidence/{RUN}/bind", headers=H, json={"profile": profile})
    assert r.status_code == 403


def test_profile_is_not_an_existence_oracle(env: Env) -> None:
    present = env.client.get(
        "/api/compliance/audit/map", headers=H, params={"profile": str(env.outside / "profile.yaml")}
    )
    absent = env.client.get(
        "/api/compliance/audit/map", headers=H, params={"profile": str(env.outside / "nope.yaml")}
    )
    assert present.status_code == absent.status_code == 403


def test_a_built_in_profile_still_works(env: Env) -> None:
    r = env.client.get("/api/compliance/audit/map", headers=H, params={"profile": "nist-ai-rmf"})
    assert r.status_code == 200


# -- ratchet node_id ----------------------------------------------------------------


@pytest.mark.parametrize("node_id", BAD_SEGMENTS)
def test_ratchet_init_refuses_a_path_node_id(env: Env, node_id: str) -> None:
    r = env.client.post("/api/seal/ratchet/init", headers=H, json={"node_id": node_id})
    assert r.status_code == 400
    assert not (env.home / "seal" / "escape.json").exists()
    assert not list(env.home.parent.glob("*.json"))


@pytest.mark.parametrize("node_id", BAD_SEGMENTS)
def test_ratchet_status_and_rotate_refuse_a_path_node_id(env: Env, node_id: str) -> None:
    r = env.client.get("/api/seal/ratchet/status", headers=H, params={"node_id": node_id})
    assert r.status_code == 400
    r = env.client.post(
        "/api/seal/ratchet/rotate", headers=H, json={"node_id": node_id, "confirmed": True}
    )
    assert r.status_code == 400


def test_ratchet_responses_never_carry_the_chain_key(env: Env) -> None:
    r = env.client.post("/api/seal/ratchet/init", headers=H, json={"node_id": "node-a"})
    assert r.status_code == 200 and r.json()["ok"], r.text[:200]
    assert "chain_key_hex" not in r.text
    r = env.client.get("/api/seal/ratchet/status", headers=H, params={"node_id": "node-a"})
    assert r.json()["ok"] and r.json()["state"]["epoch"] == 0
    assert "chain_key_hex" not in r.text
    r = env.client.post(
        "/api/seal/ratchet/rotate", headers=H, json={"node_id": "node-a", "confirmed": True}
    )
    assert r.json()["ok"] and r.json()["state"]["epoch"] == 1
    assert "chain_key_hex" not in r.text


# -- capsule ids joined to a base directory -----------------------------------------


@pytest.mark.parametrize("capsule_id", BAD_SEGMENTS)
def test_sigstore_routes_refuse_a_path_capsule_id(env: Env, capsule_id: str) -> None:
    for route in ("/api/seal/sigstore/sign", "/api/seal/sigstore/verify"):
        r = env.client.post(route, headers=H, json={"capsule_id": capsule_id})
        assert r.status_code == 400, (route, r.status_code)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/seal/%2E%2E/proposals"),
        ("POST", "/api/seal/%2E%2E/verify"),
        ("POST", "/api/seal/%2E%2E/bypass"),
        ("GET", "/api/runs/%2E%2E/children"),
        ("POST", "/api/runs/%2E%2E/validate-distributed"),
    ],
)
def test_dotdot_path_segments_are_refused(env: Env, method: str, path: str) -> None:
    r = env.client.request(method, path, headers=H, json={})
    assert r.status_code == 400, r.text[:200]


def test_children_still_works_for_a_real_run(env: Env) -> None:
    r = env.client.get(f"/api/runs/{RUN}/children", headers=H)
    assert r.status_code == 200


# -- ingest-capsule -----------------------------------------------------------------


def test_ingest_capsule_refuses_a_path_outside_the_roots(env: Env) -> None:
    r = env.client.post(
        "/api/ingest-capsule", headers=H, json={"run_id": str(env.outside / "capsule")}
    )
    assert r.status_code == 403


@pytest.mark.parametrize("run_id", ["..", "../outside/capsule"])
def test_ingest_capsule_refuses_dotdot(env: Env, run_id: str) -> None:
    r = env.client.post("/api/ingest-capsule", headers=H, json={"run_id": run_id})
    assert r.status_code in (400, 403)


def test_ingest_capsule_still_takes_a_run_id(env: Env) -> None:
    r = env.client.post("/api/ingest-capsule", headers=H, json={"run_id": RUN})
    assert r.status_code == 200, r.text[:200]


# -- evidence cart ------------------------------------------------------------------


@pytest.mark.parametrize("cart_id", ["../escape", "a/b", "..", "x y"])
def test_cart_export_refuses_a_path_cart_id(env: Env, cart_id: str) -> None:
    r = env.client.post(
        "/api/evidence/cart/export",
        headers=H,
        json={"cart_id": cart_id, "items": [_CART_ITEM], "confirmed": True},
    )
    assert r.status_code == 422, r.text[:200]
    assert "cart_id" in r.text


def test_cart_export_accepts_a_plain_cart_id(env: Env) -> None:
    r = env.client.post(
        "/api/evidence/cart/export",
        headers=H,
        json={"cart_id": "cart_01-a", "items": [_CART_ITEM], "confirmed": True},
    )
    assert r.status_code != 422 or "cart_id" not in r.text, r.text[:200]


_CART_ITEM = {"kind": "run", "ref": RUN, "added_at": "2026-10-22T00:00:00Z"}
