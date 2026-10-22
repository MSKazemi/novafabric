"""Round-4 default: ``nova rebuild-metadata-db`` never reports an empty rebuild as done.

``--data-dir`` was forwarded to ``make_adapter`` as ``data_dir=…``: the ``local``
backend (an in-memory test adapter) ignored it and the command printed "Done" with
0 capsules — a false success on a disaster-recovery path — while every real backend
failed with ``TypeError: unexpected keyword argument 'data_dir'``. No object-store
backend reads a local directory, so ``--data-dir`` now exits 2 with that explanation,
and a rebuild from the in-memory ``local`` adapter (empty in any fresh process) is
refused the same way instead of writing an empty metadata DB.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.object_capsule_store.rebuild import (
    RebuildSourceError,
    require_durable_source,
)

runner = CliRunner()


def test_data_dir_is_refused_and_never_forwarded(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    def fake_make_adapter(**kwargs: Any) -> MagicMock:
        calls.append(kwargs)
        return MagicMock()

    with patch(
        "novafabric.object_capsule_store.backend_router.make_adapter", fake_make_adapter
    ):
        result = runner.invoke(
            app,
            ["rebuild-metadata-db", "--data-dir", str(tmp_path), "--backend", "s3",
             "--target-db", str(tmp_path / "out.db")],
            env={"COLUMNS": "300"},
        )
    assert result.exit_code == 2, result.output
    assert isinstance(result.exception, SystemExit)
    assert "--data-dir" in result.output
    assert calls == []
    assert not (tmp_path / "out.db").exists()


def test_in_memory_local_backend_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "out.db"
    result = runner.invoke(
        app, ["rebuild-metadata-db", "--backend", "local", "--target-db", str(out)],
        env={"COLUMNS": "300"},
    )
    assert result.exit_code == 2, result.output
    assert isinstance(result.exception, SystemExit)
    assert "in-memory" in result.output
    assert not out.exists()


def test_require_durable_source() -> None:
    from novafabric.object_capsule_store.backend_router import InMemoryWormAdapter

    try:
        require_durable_source(InMemoryWormAdapter())
    except RebuildSourceError as exc:
        assert "in-memory" in str(exc)
    else:  # pragma: no cover - the assertion below reports it
        raise AssertionError("an in-memory adapter must be refused")
    require_durable_source(MagicMock())  # any real/mocked backend passes


def test_serve_endpoint_refuses_the_in_memory_backend(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import pytest

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from novafabric.serve.app import create_app

    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "nova-home"))
    token = "t-rebuild"
    app_ = create_app(
        capsule_dir=tmp_path / "runs", token=token, db_path=tmp_path / "r.db", static_dir=None
    )
    with TestClient(app_) as client:
        r = client.post(
            f"/api/admin/rebuild-metadata-db?token={token}",
            headers={"host": "127.0.0.1:4321"},
            json={"confirmed": True},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "in-memory" in body["error"]
