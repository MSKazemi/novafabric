"""Evidence Bundle read routes never follow a link (or block on a FIFO).

``POST /api/evidence/{id}/verify`` already opened ``<evidence_dir>/<id>.zip``
through :func:`novafabric._capsule_fs.open_capsule_file`. The list, detail and
``/download`` routes still used ``Path.exists()`` + ``zipfile.ZipFile(path)`` /
``FileResponse(path)``, which follow a symlink: a ``leak.zip -> /some/secret``
planted in the evidence directory was served to any read-scope caller, and a
FIFO named ``x.zip`` hung the worker. All four routes now read through the same
confinement.
"""

from __future__ import annotations

import io
import json
import os
import threading
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

TOKEN = "test-token-evidence-links"
HOST = {"host": "127.0.0.1:4321"}
SECRET = b"TOP-SECRET-NOT-EVIDENCE"


def _bundle_bytes(run_id: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"run_id": run_id, "created_at": "2026-10-23"}))
        zf.writestr("artifacts/secret.txt", SECRET)
    return buf.getvalue()


@pytest.fixture
def evidence_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "evidence"
    d.mkdir()
    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_DIR", str(d))
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    return d


@pytest.fixture
def client(tmp_path: Path, evidence_dir: Path) -> TestClient:
    caps = tmp_path / "runs"
    caps.mkdir()
    return TestClient(
        create_app(token=TOKEN, capsule_dir=caps, db_path=tmp_path / "r.db"),
        raise_server_exceptions=False,
    )


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A real bundle outside the evidence dir, the target of a planted link."""
    real = tmp_path / "elsewhere" / "real.zip"
    real.parent.mkdir()
    real.write_bytes(_bundle_bytes("outside-run"))
    return real


def _get(client: TestClient, path: str):  # type: ignore[no-untyped-def]
    return client.get(path, params={"token": TOKEN}, headers=HOST)


# ── the genuine case still works ───────────────────────────────────────────


def test_regular_bundle_download_and_detail(client: TestClient, evidence_dir: Path) -> None:
    data = _bundle_bytes("run-1")
    (evidence_dir / "b1.zip").write_bytes(data)
    r = _get(client, "/api/evidence/b1/download")
    assert r.status_code == 200
    assert r.content == data
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["content-length"] == str(len(data))
    assert 'filename="evidence-b1.zip"' in r.headers["content-disposition"]

    d = _get(client, "/api/evidence/b1")
    assert d.status_code == 200
    body = d.json()
    assert body["run_id"] == "run-1"
    assert body["size_bytes"] == len(data)
    assert {f["path"] for f in body["files"]} == {"manifest.json", "artifacts/secret.txt"}

    listed = _get(client, "/api/evidence").json()
    assert [b["bundle_id"] for b in listed["bundles"]] == ["b1"]


def test_missing_bundle_is_404(client: TestClient) -> None:
    assert _get(client, "/api/evidence/nope/download").status_code == 404
    assert _get(client, "/api/evidence/nope").status_code == 404


# ── links ──────────────────────────────────────────────────────────────────


def test_symlinked_bundle_is_not_downloaded(
    client: TestClient, evidence_dir: Path, outside: Path
) -> None:
    (evidence_dir / "linked.zip").symlink_to(outside)
    r = _get(client, "/api/evidence/linked/download")
    assert r.status_code == 422
    assert SECRET not in r.content
    assert b"PK" not in r.content[:2]


def test_link_to_a_non_zip_secret_is_not_downloaded(
    client: TestClient, evidence_dir: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "id_rsa"
    secret.write_bytes(SECRET)
    (evidence_dir / "leak.zip").symlink_to(secret)
    r = _get(client, "/api/evidence/leak/download")
    assert r.status_code == 422
    assert SECRET not in r.content


def test_symlinked_bundle_detail_is_refused(
    client: TestClient, evidence_dir: Path, outside: Path
) -> None:
    (evidence_dir / "linked.zip").symlink_to(outside)
    r = _get(client, "/api/evidence/linked")
    assert r.status_code == 422
    assert "outside-run" not in r.text


def test_symlinked_bundle_is_not_listed(
    client: TestClient, evidence_dir: Path, outside: Path
) -> None:
    (evidence_dir / "linked.zip").symlink_to(outside)
    (evidence_dir / "real.zip").write_bytes(_bundle_bytes("run-real"))
    body = _get(client, "/api/evidence").json()
    assert [b["bundle_id"] for b in body["bundles"]] == ["real"]
    assert body["total"] == 1
    assert "outside-run" not in json.dumps(body)


# ── special files ──────────────────────────────────────────────────────────


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
@pytest.mark.timeout(30)
def test_fifo_bundle_never_blocks(client: TestClient, evidence_dir: Path) -> None:
    fifo = evidence_dir / "pipe.zip"
    os.mkfifo(fifo)
    stop = threading.Event()
    blocked: list[bool] = []

    def _unblock() -> None:
        # A regression opens the FIFO with a blocking open(2), which waits for a
        # writer forever and no timeout can interrupt. Play the writer after a
        # grace period so that regression FAILS (below) instead of hanging.
        while not stop.wait(5.0):
            try:
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
                blocked.append(True)
            except OSError:  # ENXIO: no reader is blocked on it — good
                pass

    t = threading.Thread(target=_unblock, daemon=True)
    t.start()
    try:
        assert _get(client, "/api/evidence/pipe/download").status_code == 422
        assert _get(client, "/api/evidence/pipe").status_code == 422
        body = _get(client, "/api/evidence").json()
        assert body["bundles"] == []
    finally:
        stop.set()
        t.join(10)
    assert not blocked, "a route blocked opening the FIFO"
