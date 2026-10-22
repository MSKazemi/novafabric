"""No ``nova serve`` GET route returns data from behind a link in *any* capsule.

``test_capsule_link_reads.py`` pins the ``{run_id}`` routes for the run you
name. The second adversarial review found the routes that read *many* capsules
at once: ``/api/cost/report``, ``/api/runs/suggest-register`` and
``/api/runs/{id}/run-lineage`` (which scans its siblings) parsed a link planted
at the top level of a **sibling** capsule. A capsule whose *root* is a link to a
directory outside the capsule dir was listed and served by nine routes, including
``/api/runs``, ``/api/runs/search``, ``/api/runs/{id}``, ``/assure`` and
``/diagnose``.

This test plants each shape and then calls **every** GET route the app
registers (path parameters filled with each run id, ``run_id``-style query
parameters too), so a route added later is covered without being listed here.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")

TOKEN = "scan-link-token-0123456789abcdef0123"
HEADERS = {"host": "127.0.0.1:4321", "authorization": f"Bearer {TOKEN}"}
CANARY = "CANARY-SCAN-7f3a"
RUN = "01J9ZZSERVESCANLINKS000001"
EVIL = "01J9ZZSERVESCANLINKS0000EV"
ROOTLINK = "01J9ZZSERVESCANLINKS0000RL"

#: Every top-level stream a capsule reader parses.
_STREAMS = (
    "lineage.jsonl",
    "model-calls.jsonl",
    "tool-calls.jsonl",
    "trace.jsonl",
    "assets.jsonl",
    "events.jsonl",
    "network_events.jsonl",
)
#: Query parameters that name a run or a capsule.
_RUN_QUERY_PARAMS = frozenset({"run_id", "run", "run_ids", "capsule", "capsule_path", "runs"})


def _canary_record(run_id: str) -> dict[str, object]:
    """One record that every capsule reader would surface somewhere."""
    return {
        "CANARYKEY": CANARY,
        "run_id": run_id,
        "source_run_id": run_id,
        "target_run_id": RUN,
        "parent_run_id": RUN,
        "edge_type": "contains",
        "model_call_id": CANARY,
        "tool_call_id": CANARY,
        "tool_name": CANARY,
        "name": CANARY,
        "status": "success",
        "gen_ai.request.model": CANARY,
        "gen_ai.response.model": CANARY,
        "gen_ai.usage.input_tokens": 7,
        "message": CANARY,
        "event_type": CANARY,
        "started_at": "2099-01-01T00:00:00Z",
        "timestamp": "2099-01-01T00:00:00Z",
    }


def _write_clean_capsule(cap: Path, run_id: str) -> None:
    (cap / "inputs").mkdir(parents=True)
    (cap / "outputs").mkdir()
    manifest = {
        "run_id": run_id,
        "status": "success",
        "created_at": "2099-01-01T00:00:00Z",
        "command": ["python", "agent.py"],
        "model_call_count": 0,
    }
    (cap / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    for name in _STREAMS:
        (cap / name).write_text("")
    (cap / "trace.jsonl").write_text('{"span": 1}\n')
    (cap / "outputs" / "stdout.txt").write_text("hello\n")


@dataclass
class Env:
    client: TestClient
    app: object
    capsules: Path


def _plant_sibling_links(capsules: Path, secret: Path) -> list[str]:
    """A sibling capsule whose manifest is real but whose streams are links."""
    evil = capsules / EVIL
    evil.mkdir()
    (evil / "capsule.yaml").write_text(
        yaml.safe_dump({"run_id": EVIL, "status": "success", "created_at": "2099-01-01T00:00:00Z"})
    )
    for name in _STREAMS:
        (evil / name).symlink_to(secret)
    return [RUN, EVIL]


def _plant_root_link(capsules: Path, secret: Path) -> list[str]:
    """A capsule-dir entry that is itself a link to a directory outside it."""
    outside = secret.parent / "rootcap"
    outside.mkdir()
    (outside / "capsule.yaml").write_text(
        yaml.safe_dump({"run_id": ROOTLINK, "status": CANARY, "command": [CANARY]})
    )
    for name in _STREAMS:
        (outside / name).write_text(secret.read_text())
    (capsules / ROOTLINK).symlink_to(outside, target_is_directory=True)
    return [RUN, ROOTLINK]


def _plant_nested_links(capsules: Path, secret: Path) -> list[str]:
    """Links one level down in the clean capsule, where the top-level check never looks."""
    cap = capsules / RUN
    for rel in (
        "inputs/leak.jsonl",
        "outputs/leak.jsonl",
        "extensions/dev.novafabric.dataset-provenance/leak.json",
        "extensions/io.novafabric.x/leak.jsonl",
        "seal/leak.json",
        "evidence/leak.json",
        "replays/leak.json",
        "scores/leak.jsonl",
        "events/leak.jsonl",
    ):
        p = cap / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.symlink_to(secret)
    return [RUN]


def _plant_special_files(capsules: Path, secret: Path) -> list[str]:
    """A sibling whose streams are FIFOs or links to ``/dev/zero``: a read would hang or OOM."""
    evil = capsules / EVIL
    evil.mkdir()
    (evil / "capsule.yaml").write_text(
        yaml.safe_dump({"run_id": EVIL, "status": "success", "created_at": "2099-01-01T00:00:00Z"})
    )
    for i, name in enumerate(_STREAMS):
        if i % 2:
            (evil / name).symlink_to("/dev/zero")
        else:
            os.mkfifo(evil / name)
    return [RUN, EVIL]


_SHAPES = {
    "sibling-top-level-links": _plant_sibling_links,
    "sibling-special-files": _plant_special_files,
    "capsule-root-link": _plant_root_link,
    "nested-links": _plant_nested_links,
}


@pytest.fixture(params=sorted(_SHAPES))
def shaped(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Env, list[str]]]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    monkeypatch.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)
    monkeypatch.delenv("NOVA_DATA_DIR", raising=False)
    monkeypatch.delenv("NOVA_CLICKHOUSE_URL", raising=False)
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(home / "audit.jsonl"))
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.jsonl"
    secret.write_text(json.dumps(_canary_record(EVIL)) + "\n")
    capsules = tmp_path / "capsules"
    capsules.mkdir()
    _write_clean_capsule(capsules / RUN, RUN)
    run_ids = _SHAPES[request.param](capsules, secret)
    app = create_app(token=TOKEN, capsule_dir=capsules, db_path=None, static_dir=None)
    with TestClient(app, base_url="http://127.0.0.1:4321") as client:
        yield Env(client=client, app=app, capsules=capsules), run_ids


def _get_urls(app: object, run_ids: list[str]) -> list[str]:
    urls: list[str] = []
    for route in getattr(app, "routes", []):
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", "")
        if "GET" not in methods or "stream" in path or path.startswith("/ws"):
            continue
        params = re.findall(r"\{([^}:]+)(?::[^}]*)?\}", path)
        dependant = getattr(route, "dependant", None)
        query_names = {
            p.alias for p in (getattr(dependant, "query_params", None) or [])
        } & _RUN_QUERY_PARAMS
        for rid in run_ids if (params or query_names) else [""]:
            url = re.sub(r"\{[^}]+\}", rid or "x", path)
            if query_names:
                url += "?" + "&".join(f"{q}={rid}" for q in sorted(query_names))
            urls.append(url)
    return urls


@pytest.mark.timeout(240)
def test_no_get_route_returns_data_from_behind_a_link(shaped: tuple[Env, list[str]]) -> None:
    env, run_ids = shaped
    urls = _get_urls(env.app, run_ids)
    assert len(urls) > 100, "the route walk found too few GET routes to mean anything"
    leaks = []
    for url in urls:
        r = env.client.get(url, headers=HEADERS)
        if CANARY in r.text:
            leaks.append(f"{r.status_code} GET {url}")
    assert leaks == [], "routes followed a link out of the capsule:\n" + "\n".join(leaks)


def test_a_capsule_root_link_outside_the_capsule_dir_is_refused_and_unlisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(home / "audit.jsonl"))
    secret = tmp_path / "outside" / "secret.jsonl"
    secret.parent.mkdir()
    secret.write_text(json.dumps(_canary_record(ROOTLINK)) + "\n")
    capsules = tmp_path / "capsules"
    capsules.mkdir()
    _write_clean_capsule(capsules / RUN, RUN)
    _plant_root_link(capsules, secret)
    app = create_app(token=TOKEN, capsule_dir=capsules, db_path=None, static_dir=None)
    with TestClient(app, base_url="http://127.0.0.1:4321") as client:
        r = client.get(f"/api/runs/{ROOTLINK}", headers=HEADERS)
        assert r.status_code == 403, r.text[:300]
        assert "outside" in r.json()["detail"]
        listed = client.get("/api/runs", headers=HEADERS).json()
        ids = [row["run_id"] for row in listed["runs"]]
        assert ids == [RUN]


def test_an_alias_link_inside_the_capsule_dir_still_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``latest -> 01J...`` stays usable by name, and is not listed twice."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(home / "audit.jsonl"))
    capsules = tmp_path / "capsules"
    capsules.mkdir()
    _write_clean_capsule(capsules / RUN, RUN)
    os.symlink(RUN, capsules / "latest", target_is_directory=True)
    app = create_app(token=TOKEN, capsule_dir=capsules, db_path=None, static_dir=None)
    with TestClient(app, base_url="http://127.0.0.1:4321") as client:
        r = client.get("/api/runs/latest", headers=HEADERS)
        assert r.status_code == 200, r.text[:300]
        assert r.json()["run_id"] == RUN
        listed = client.get("/api/runs", headers=HEADERS).json()
        assert [row["run_id"] for row in listed["runs"]] == [RUN]
