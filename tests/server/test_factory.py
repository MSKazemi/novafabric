"""Unit tests for the multi-worker ASGI app factory (novafabric.server.factory).

The factory is what ``nova server start --workers N`` imports in each worker
process. These tests exercise its config-resolution logic in isolation; the
CLI plumbing that launches it is covered in
``tests/cli/test_verify_server_serve_cov.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from novafabric.server import factory  # noqa: E402


def test_resolve_config_reads_path_and_checks_bind(monkeypatch, tmp_path):
    seen = {}

    class _Cfg:
        host = "127.0.0.1"
        port = 7433
        insecure_no_auth = False

    cfg_path = tmp_path / "server.yaml"

    def _load(path):
        seen["path"] = path
        return _Cfg()

    def _check(cfg):
        seen["checked"] = cfg

    monkeypatch.setattr("novafabric.server.config.load_config", _load)
    monkeypatch.setattr("novafabric.server.config.check_insecure_bind", _check)

    cfg = factory.resolve_config(cfg_path)
    assert seen["path"] == cfg_path
    assert seen["checked"] is cfg  # the ADR-0184 bind guard ran


def test_make_app_uses_config_env_and_sets_local_token(monkeypatch, tmp_path):
    cfg_path = tmp_path / "server.yaml"
    monkeypatch.setenv(factory.CONFIG_ENV, str(cfg_path))

    class _Cfg:
        host = "127.0.0.1"
        port = 7433
        insecure_no_auth = False
        local_token = None

        class oidc:
            enabled = False  # local mode → token must be resolved

    resolved = {}

    def _resolve(path):
        resolved["path"] = path
        return _Cfg()

    monkeypatch.setattr(factory, "resolve_config", _resolve)
    monkeypatch.setattr(
        "novafabric.server.local_token.ensure_local_token",
        lambda: ("tok-shared", Path("/x/.server-token")),
    )
    built = {}
    monkeypatch.setattr(
        "novafabric.server.app.create_app",
        lambda cfg: built.setdefault("cfg", cfg) or object(),
    )

    factory.make_app()

    # Worker read the config path from the env, and all workers converge on the
    # same file-backed local token.
    assert resolved["path"] == cfg_path
    assert built["cfg"].local_token == "tok-shared"


def test_make_app_skips_token_when_oidc_enabled(monkeypatch):
    class _Cfg:
        host = "127.0.0.1"
        port = 7433
        insecure_no_auth = False
        local_token = None

        class oidc:
            enabled = True  # OIDC mode → no local token

    monkeypatch.setattr(factory, "resolve_config", lambda path: _Cfg())

    def _boom():  # pragma: no cover — must not be called
        raise AssertionError("ensure_local_token should not run under OIDC")

    monkeypatch.setattr(
        "novafabric.server.local_token.ensure_local_token", _boom
    )
    built = {}
    monkeypatch.setattr(
        "novafabric.server.app.create_app",
        lambda cfg: built.setdefault("cfg", cfg) or object(),
    )

    factory.make_app()
    assert built["cfg"].local_token is None
