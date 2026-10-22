"""L1 — the MetadataStore Postgres alembic env applies the ONE shared DSN rule.

``metadata_store/migrations/postgres/env.py`` re-derived the driver rule instead
of calling :func:`novafabric.metadata_store.dsn.to_sqlalchemy_url`, and got it
wrong both ways: ``postgres://`` (what libpq and most hosts emit) was left
without a dialect, so SQLAlchemy failed to load it; and an explicit
``postgresql+psycopg2://`` was silently rewritten to psycopg3, overriding a
deliberate driver choice. Running the real ``env.py`` (with alembic's context
and ``create_engine`` stubbed) pins that the URL reaching the engine is exactly
what the shared rule produces.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

import novafabric.metadata_store as ms_pkg
from novafabric.metadata_store.dsn import to_sqlalchemy_url

ENV_PY = Path(ms_pkg.__file__).parent / "migrations" / "postgres" / "env.py"


class _EngineReached(Exception):
    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.url = url


def _url_reaching_engine(monkeypatch: pytest.MonkeyPatch, dsn: str | None) -> str:
    import alembic
    import sqlalchemy

    if dsn is None:
        monkeypatch.delenv("NOVAFABRIC_METADATA_DSN", raising=False)
    else:
        monkeypatch.setenv("NOVAFABRIC_METADATA_DSN", dsn)
    monkeypatch.setattr(alembic, "context", SimpleNamespace(config=None))

    def _engine(url: str, *_a: object, **_k: object) -> None:
        raise _EngineReached(url)

    monkeypatch.setattr(sqlalchemy, "create_engine", _engine)
    spec = importlib.util.spec_from_file_location("_nova_pg_env_under_test", ENV_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with pytest.raises(_EngineReached) as reached:
        spec.loader.exec_module(module)
    return reached.value.url


@pytest.mark.parametrize(
    "dsn",
    [
        "postgres://u:pw@db.example.com:5432/nova",
        "postgresql://u:pw@h/d?sslmode=require",
        "postgresql+psycopg://u:pw@h/d",
        "postgresql+psycopg2://u:pw@h/d",
    ],
)
def test_env_url_is_the_shared_rule(monkeypatch: pytest.MonkeyPatch, dsn: str) -> None:
    assert _url_reaching_engine(monkeypatch, dsn) == to_sqlalchemy_url(dsn)


def test_postgres_short_scheme_gets_the_psycopg3_dialect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = _url_reaching_engine(monkeypatch, "postgres://u:pw@h/d")
    assert url == "postgresql+psycopg://u:pw@h/d"


def test_an_explicit_psycopg2_driver_is_not_silently_rewritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = _url_reaching_engine(monkeypatch, "postgresql+psycopg2://u:pw@h/d")
    assert url == "postgresql+psycopg2://u:pw@h/d"


def test_missing_dsn_is_a_named_error_without_echoing_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import alembic

    monkeypatch.delenv("NOVAFABRIC_METADATA_DSN", raising=False)
    monkeypatch.setattr(alembic, "context", SimpleNamespace(config=None))
    spec = importlib.util.spec_from_file_location("_nova_pg_env_under_test", ENV_PY)
    assert spec is not None and spec.loader is not None
    with pytest.raises(RuntimeError, match="NOVAFABRIC_METADATA_DSN is not set"):
        spec.loader.exec_module(importlib.util.module_from_spec(spec))
