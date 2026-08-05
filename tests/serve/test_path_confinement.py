"""S2: caller-supplied filesystem paths in the dashboard refuse system targets.

Several `serve` endpoints take a filesystem path in the request body. In
container/Helm dashboard mode the app binds 0.0.0.0 with token-only auth, so an
unconfined absolute path is arbitrary read/write as the server user.
`_confine_path` is a *denylist*, not a sandbox: it allows the endpoints' intended
arbitrary paths (home, project dirs, tmp, mounted volumes) but rejects
system-critical directories (/etc, /usr, /bin, ...), with a documented opt-out.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import _confine_path, create_app  # noqa: E402

TOKEN = "confine-token"
H = {"host": "127.0.0.1:4321"}
HJ = {"host": "127.0.0.1:4321", "content-type": "application/json"}
P = {"token": TOKEN}


# ---------------------------------------------------------------------------
# _confine_path unit tests
# ---------------------------------------------------------------------------


def test_allows_home_and_project_and_tmp_paths(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("NOVAFABRIC_SERVE_ALLOW_ANY_PATH", raising=False)
    # tmp dirs, home, and project paths are all legitimate targets → allowed.
    assert _confine_path(tmp_path / "evidence" / "x.zip") == (
        tmp_path / "evidence" / "x.zip"
    ).resolve()
    home = Path.home()
    assert _confine_path(home / ".novafabric" / "merkle.db") == (
        home / ".novafabric" / "merkle.db"
    ).resolve()


@pytest.mark.parametrize(
    "bad",
    ["/etc/passwd", "/usr/bin/nova", "/bin/sh", "/root/.ssh/authorized_keys", "/boot/x"],
)
def test_rejects_system_paths(bad: str, monkeypatch) -> None:
    monkeypatch.delenv("NOVAFABRIC_SERVE_ALLOW_ANY_PATH", raising=False)
    with pytest.raises(HTTPException) as exc:
        _confine_path(bad)
    assert exc.value.status_code == 403


def test_rejects_traversal_into_system_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("NOVAFABRIC_SERVE_ALLOW_ANY_PATH", raising=False)
    # ../ traversal that resolves into /etc is caught after resolution.
    with pytest.raises(HTTPException) as exc:
        _confine_path(tmp_path / ".." / ".." / ".." / ".." / ".." / "etc" / "passwd")
    assert exc.value.status_code == 403


def test_opt_out_allows_system_path(monkeypatch) -> None:
    monkeypatch.setenv("NOVAFABRIC_SERVE_ALLOW_ANY_PATH", "1")
    assert _confine_path("/etc/novafabric/x") == Path("/etc/novafabric/x").resolve()


# ---------------------------------------------------------------------------
# Endpoint wiring — capsule-migrate rejects a system-dir source
# ---------------------------------------------------------------------------


def test_capsule_migrate_rejects_system_source(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("NOVAFABRIC_SERVE_ALLOW_ANY_PATH", raising=False)
    base = tmp_path / "capsules"
    base.mkdir()
    client = TestClient(
        create_app(token=TOKEN, capsule_dir=base, db_path=None, static_dir=None)
    )
    resp = client.post(
        "/api/capsule-migrate",
        params=P,
        headers=HJ,
        json={"source": "/etc", "output": str(base / "out")},
    )
    assert resp.status_code == 403
