"""The Docker entrypoint migrates the Alembic track each mode actually checks.

``nova server start`` refuses to start unless the registry track's stamp in
``NOVAFABRIC_POSTGRES_DSN`` is at head (``novafabric.server.schema_skew``). The
entrypoint ran a bare ``nova db upgrade --backend postgres`` (the MetadataStore
track, ``nova db upgrade``'s default) for both modes. Both tracks keep their
stamp in one ``alembic_version`` table, and in the bundled compose stack both
DSNs name the same database, so server mode was stamped ``v005`` and refused
with ``SchemaSkewError``. The Helm chart had the same defect.

Server mode now runs ``--track registry`` only; dashboard mode keeps the
MetadataStore track. The script is executed for real with ``nova`` and
``pg_isready`` replaced by recording stubs, and ``/data`` redirected into
``tmp_path`` (the entrypoint creates its bind-mount directories there).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO / "deploy" / "docker" / "entrypoint.sh"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")


def _run(tmp_path: Path, mode: str) -> list[list[str]]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "nova-calls.log"
    (bindir / "pg_isready").write_text("#!/bin/sh\nexit 0\n")
    (bindir / "nova").write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\nexit 0\n')
    for stub in bindir.iterdir():
        stub.chmod(0o755)

    data = tmp_path / "data"
    script = tmp_path / "entrypoint.sh"
    script.write_text(ENTRYPOINT.read_text(encoding="utf-8").replace("/data/", f"{data}/"))

    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tmp_path),
        "NOVA_MODE": mode,
    }
    result = subprocess.run(
        ["sh", str(script)], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return [line.split() for line in log.read_text().splitlines()]


def _upgrades(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if c[:2] == ["db", "upgrade"]]


def test_server_mode_migrates_only_the_registry_track(tmp_path: Path) -> None:
    calls = _run(tmp_path, "server")
    upgrades = _upgrades(calls)
    assert upgrades == [
        ["db", "upgrade", "--track", "registry", "--backend", "postgres", "--revision", "head"]
    ], upgrades
    assert calls[-1][:2] == ["server", "start"], calls


def test_dashboard_mode_keeps_the_metadata_track(tmp_path: Path) -> None:
    calls = _run(tmp_path, "dashboard")
    upgrades = _upgrades(calls)
    assert upgrades == [["db", "upgrade", "--backend", "postgres", "--revision", "head"]], upgrades
    assert "--track" not in upgrades[0]
    assert calls[-1][0] == "serve", calls


def test_server_deps_still_bootstrap_the_metadata_store() -> None:
    """Server mode relies on the server creating its MetadataStore tables itself."""
    deps = (REPO / "src" / "novafabric" / "server" / "deps.py").read_text(encoding="utf-8")
    assert "store.bootstrap()" in deps, (
        "server/deps.py no longer bootstraps the MetadataStore; the entrypoint migrates "
        "only the registry track in server mode, so nothing would create those tables"
    )
