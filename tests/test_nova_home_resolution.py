"""Every NovaFabric home-relative path must follow ``NOVAFABRIC_HOME``.

Found in a real-browser pass of ``nova serve``: the health panel's keystore check
looked in a hard-coded ``~/.novafabric/keys`` while the rest of the process
(registry, tokens, audit log) followed ``NOVAFABRIC_HOME``. A server started with a
custom home therefore reported the *wrong* keystore state. Same family as the
registry-path fix (cd1c948).

Every test sets BOTH a throwaway ``NOVAFABRIC_HOME`` and a throwaway ``HOME`` so
nothing here can see the real ``~/.novafabric`` or the live data tree.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novafabric import _paths
from novafabric.serve.app import create_app

LOCALHOST_HEADERS = {"host": "127.0.0.1:4321"}


@pytest.fixture()
def homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """(custom NOVAFABRIC_HOME, fake user HOME) — both throwaway, other vars cleared."""
    custom = tmp_path / "custom-home"
    custom.mkdir()
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("NOVAFABRIC_HOME", str(custom))
    for var in (
        "NOVAFABRIC_DB_PATH",
        "NOVAFABRIC_CAPSULE_DIR",
        "NOVAFABRIC_DASHBOARD_AUDIT_FILE",
        "NOVAFABRIC_EVIDENCE_DIR",
        "NOVA_DATA_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    return custom, user_home


def _keystore_ok(tmp_path: Path) -> bool:
    capsules = tmp_path / "runs"
    capsules.mkdir(exist_ok=True)
    app = create_app(token="t", capsule_dir=capsules, db_path=tmp_path / "registry.db")
    resp = TestClient(app).get("/api/health", headers=LOCALHOST_HEADERS)
    assert resp.status_code == 200
    return bool(resp.json()["keystore_ok"])


def test_keystore_check_reports_the_custom_homes_state(
    homes: tuple[Path, Path], tmp_path: Path
) -> None:
    custom, _ = homes
    assert _keystore_ok(tmp_path) is False
    (custom / "keys").mkdir()
    assert _keystore_ok(tmp_path) is True


def test_keystore_check_never_looks_in_the_real_home(
    homes: tuple[Path, Path], tmp_path: Path
) -> None:
    _, user_home = homes
    (user_home / ".novafabric" / "keys").mkdir(parents=True)
    # A keystore exists only under ~/.novafabric; the custom home has none.
    assert _keystore_ok(tmp_path) is False


def test_keystore_check_defaults_to_user_home_when_unset(
    homes: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, user_home = homes
    monkeypatch.delenv("NOVAFABRIC_HOME")
    assert _keystore_ok(tmp_path) is False
    (user_home / ".novafabric" / "keys").mkdir(parents=True)
    assert _keystore_ok(tmp_path) is True


def test_shared_resolver_derives_everything_from_one_home(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    custom, user_home = homes
    assert _paths.keys_dir() == custom / "keys"
    assert _paths.local_key_path() == custom / "keys" / "local-key.pem"
    assert _paths.evidence_dir() == custom / "evidence"
    assert _paths.tokens_path() == custom / "tokens.jsonl"
    assert _paths.object_store_dir() == custom / "object_store"
    assert _paths.collector_health_path() == custom / "collector-health.json"
    assert _paths.metadata_db_path() == custom / "metadata.db"
    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_DIR", str(custom / "ev"))
    assert _paths.evidence_dir() == custom / "ev"
    monkeypatch.delenv("NOVAFABRIC_HOME")
    assert _paths.keys_dir() == user_home / ".novafabric" / "keys"


_ALLOWED = {
    "src/novafabric/_paths.py",  # the resolver itself
}
_HARDCODED = re.compile(r"""Path\.home\(\)\s*/\s*["']\.novafabric["']""")
_USER_GLOBAL_MARK = "user-global:"


def test_no_hardcoded_home_literal_remains_in_src() -> None:
    """A deliberately user-global site must say so with a ``user-global:`` comment."""
    root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for py in sorted((root / "src").rglob("*.py")):
        rel = py.relative_to(root).as_posix()
        if rel in _ALLOWED:
            continue
        lines = py.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            context = "".join(lines[max(0, i - 3) : i + 1])
            if _HARDCODED.search(line) and _USER_GLOBAL_MARK not in context:
                offenders.append(f"{rel}:{i + 1}: {line.strip()}")
    assert not offenders, (
        "use novafabric._paths (follows NOVAFABRIC_HOME) or mark the site "
        f"'{_USER_GLOBAL_MARK} <why>':\n" + "\n".join(offenders)
    )
