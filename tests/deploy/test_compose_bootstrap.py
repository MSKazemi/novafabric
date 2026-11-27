"""Unit tests for ``scripts/compose_bootstrap.py`` (the Compose secret generator).

The script is the only supported way the bundled Compose stack gets its
credentials, so its safety properties are pinned here: 0600 files, values never
printed, existing values never rotated, symlinks refused, demo values flagged.
Pure filesystem tests in ``tmp_path`` — no Docker, no network.
"""

from __future__ import annotations

import importlib.util
import os
import re
import stat
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "compose_bootstrap.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("compose_bootstrap", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cb = _load()


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _values(path: Path) -> dict[str, str]:
    return cb.parse_env(path.read_text(encoding="utf-8"))


def test_creates_every_secret_with_mode_0600(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    assert cb.main(["--env-file", str(env)]) == 0
    assert _mode(env) == 0o600
    values = _values(env)
    for name in cb.REQUIRED_SECRETS:
        assert re.fullmatch(r"[0-9a-f]{64}", values[name]), name
    assert len({values[name] for name in cb.REQUIRED_SECRETS}) == len(cb.REQUIRED_SECRETS)


def test_never_prints_a_secret(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env = tmp_path / ".env"
    cb.main(["--env-file", str(env)])
    cb.main(["--env-file", str(env)])
    cb.main(["--check", "--env-file", str(env)])
    captured = capsys.readouterr()
    output = captured.out + captured.err
    for name in cb.REQUIRED_SECRETS:
        assert name in output
        assert _values(env)[name] not in output


def test_rerun_keeps_existing_values_byte_for_byte(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    cb.main(["--env-file", str(env)])
    before = env.read_bytes()
    assert cb.main(["--env-file", str(env)]) == 0
    assert env.read_bytes() == before


def test_preserves_unrelated_lines_and_operator_values(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("NOVA_DATA_DIR=/srv/nova\nNOVA_POSTGRES_PASSWORD=operator-chosen\n")
    generated, kept = cb.bootstrap(env)
    assert kept == ["NOVA_POSTGRES_PASSWORD"]
    assert set(generated) == set(cb.REQUIRED_SECRETS) - {"NOVA_POSTGRES_PASSWORD"}
    values = _values(env)
    assert values["NOVA_DATA_DIR"] == "/srv/nova"
    assert values["NOVA_POSTGRES_PASSWORD"] == "operator-chosen"


def test_empty_placeholder_is_filled_without_a_duplicate_line(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("NOVA_AGE_PASSWORD=\n")
    cb.bootstrap(env)
    lines = [ln for ln in env.read_text().splitlines() if ln.startswith("NOVA_AGE_PASSWORD=")]
    assert len(lines) == 1
    assert _values(env)["NOVA_AGE_PASSWORD"]


def test_tightens_a_world_readable_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    cb.bootstrap(env)
    env.chmod(0o644)
    cb.bootstrap(env)
    assert _mode(env) == 0o600


def test_refuses_to_write_through_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("")
    link = tmp_path / ".env"
    link.symlink_to(target)
    assert cb.main(["--env-file", str(link)]) == 1
    assert target.read_text() == ""


def test_check_fails_when_the_file_is_missing(tmp_path: Path) -> None:
    assert cb.main(["--check", "--env-file", str(tmp_path / ".env")]) == 1
    assert not (tmp_path / ".env").exists(), "--check must never create the file"


def test_check_fails_on_a_missing_secret(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("NOVA_POSTGRES_PASSWORD=x\n")
    env.chmod(0o600)
    problems = cb.check(env)
    assert any("NOVA_AGE_PASSWORD" in p for p in problems)


def test_check_fails_on_a_loose_mode(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    cb.bootstrap(env)
    env.chmod(0o640)
    assert any("mode" in p for p in cb.check(env))


def test_check_fails_on_a_known_demo_value(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("NOVA_POSTGRES_PASSWORD=nova\n")
    cb.bootstrap(env)  # an upgrade keeps the volume's old password ...
    assert _values(env)["NOVA_POSTGRES_PASSWORD"] == "nova"
    problems = cb.check(env)  # ... but --check keeps flagging it until rotated
    assert any("NOVA_POSTGRES_PASSWORD" in p and "demonstration" in p for p in problems)


def test_check_passes_after_bootstrap(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    cb.bootstrap(env)
    assert cb.check(env) == []
    assert cb.main(["--check", "--env-file", str(env)]) == 0
