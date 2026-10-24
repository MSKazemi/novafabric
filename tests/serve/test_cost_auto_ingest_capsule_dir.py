"""Serve's ClickHouse cost auto-ingest scans the server's capsule store (round 10, B2).

It used to scan ``$NOVAFABRIC_HOME/capsules`` whatever ``--capsule-dir`` /
``NOVAFABRIC_CAPSULE_DIR`` said, so a server with its capsules anywhere else never
ingested a run and the cost tab stayed empty.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.cost import clickhouse_store  # noqa: E402
from novafabric.serve import app as serve_app  # noqa: E402

SERVER_TOKEN = "server-token-0123456789abcdef"


def test_ingest_all_capsules_scans_the_store_it_is_given(tmp_path: Path) -> None:
    store = tmp_path / "elsewhere"  # not <home>/capsules
    (store / "run-a").mkdir(parents=True)
    with patch.object(clickhouse_store, "ingest_capsule", return_value=2) as ingest:
        assert clickhouse_store.ingest_all_capsules(store) == {"run-a": 2}
    ingest.assert_called_once_with("run-a", store / "run-a", tenant_id="default")


def test_loop_ingests_the_capsule_dir_it_was_started_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_CLICKHOUSE_URL", "http://clickhouse.invalid:8123")
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    store = tmp_path / "store"
    seen: list[Path] = []

    def fake_ingest_all(root: Path, tenant_id: str = "default") -> dict[str, int]:
        seen.append(root)
        raise asyncio.CancelledError  # stop the loop after one tick

    with (
        patch.object(clickhouse_store, "ingest_all_capsules", side_effect=fake_ingest_all),
        pytest.raises(asyncio.CancelledError),
    ):
        asyncio.run(serve_app._cost_auto_ingest_loop(store, interval_seconds=0))
    assert seen == [store]


def test_loop_is_a_no_op_without_clickhouse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOVA_CLICKHOUSE_URL", raising=False)
    sleeps = 0

    async def fake_sleep(_: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps > 2:
            raise asyncio.CancelledError

    with (
        patch.object(clickhouse_store, "ingest_all_capsules") as ingest,
        patch.object(serve_app.asyncio, "sleep", fake_sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        asyncio.run(serve_app._cost_auto_ingest_loop(tmp_path, interval_seconds=0))
    ingest.assert_not_called()


def test_server_starts_the_loop_on_its_capsule_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(home / "audit.jsonl"))
    store = tmp_path / "runs"
    store.mkdir()
    started: list[Any] = []

    async def fake_loop(capsule_dir: Path, interval_seconds: float = 120.0) -> None:
        started.append(capsule_dir)

    monkeypatch.setattr(serve_app, "_cost_auto_ingest_loop", fake_loop)
    app = serve_app.create_app(
        token=SERVER_TOKEN, capsule_dir=store, static_mounted_by_caller=True
    )
    with TestClient(app):
        pass
    assert started == [store]
