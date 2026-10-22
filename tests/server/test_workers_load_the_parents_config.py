"""L17 — ``--workers`` children must load the SAME config file as the parent.

``nova server start`` resolves its config as ``--config`` or the default
``~/.config/novafabric/server.yaml`` — there is no other search path, and
``NOVAFABRIC_SERVER_CONFIG`` is documented as internal ("set internally to hand
the path to ``--workers`` child processes; do not set it yourself"). The parent
exported it only when ``--config`` was given, and the worker factory read it
whenever present. So with a value already in the environment (exported by a
previous ``--config`` run in the same shell, a wrapper, a container env) and no
``--config``, the parent validated and printed one config while every worker
served another — different OIDC, rate limits, db_path.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novafabric.cli import server as server_cli
from novafabric.server import config as config_mod
from novafabric.server import factory
from novafabric.server.config import load_config

_EXPORTED = (
    factory.CONFIG_ENV,
    "NOVAFABRIC_SERVER_HOST",
    "NOVAFABRIC_SERVER_PORT",
    "NOVAFABRIC_SERVER_BACKEND",
    "NOVAFABRIC_SERVER_INSECURE_NO_AUTH",
    "NOVAFABRIC_SERVER_I_KNOW_THIS_IS_PUBLIC",
    "NOVAFABRIC_SERVER_I_ACCEPT_SHARED_CAPSULE_STORE",
)


def _worker_db_path(
    monkeypatch: pytest.MonkeyPatch, parent_cfg: Any, config_path: Path | None
) -> str | None:
    seen: dict[str, Any] = {}

    def _run(*_a: object, **_k: object) -> None:
        # What each worker's make_app() would resolve, in the env the parent set.
        import os

        env = os.environ.get(factory.CONFIG_ENV)
        seen["db_path"] = factory.resolve_config(Path(env) if env else None).db_path

    server_cli._launch_workers(SimpleNamespace(run=_run), parent_cfg, config_path, 2)
    return seen["db_path"]


@pytest.fixture
def two_configs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    for name in _EXPORTED:  # restored at teardown even though the code sets them
        monkeypatch.delenv(name, raising=False)
    default = tmp_path / "default.yaml"
    default.write_text(f"db_path: {tmp_path / 'default.db'}\n")
    stale = tmp_path / "stale.yaml"
    stale.write_text(f"db_path: {tmp_path / 'stale.db'}\n")
    monkeypatch.setattr(config_mod, "_DEFAULT_CONFIG_PATH", default)
    return default, stale


def test_inherited_env_does_not_redirect_workers_without_config(
    two_configs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _default, stale = two_configs
    monkeypatch.setenv(factory.CONFIG_ENV, str(stale))
    parent = load_config(None)
    assert _worker_db_path(monkeypatch, parent, None) == parent.db_path


def test_explicit_config_is_handed_to_workers(
    two_configs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _default, stale = two_configs
    parent = load_config(stale)
    assert _worker_db_path(monkeypatch, parent, stale) == parent.db_path
