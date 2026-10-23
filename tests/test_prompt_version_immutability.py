"""A prompt version number is never handed to different content (adversarial review r6).

``register_prompt_version`` numbered a new version ``max(existing rows) + 1`` and skipped
the guard that asset registration has (``VersionReuseError``, ADR-0312 Amendment 3). After
``promote`` to production, ``archive`` and ``unregister``, the rows were gone, so the next
registration got the *same* number with *new* content: ``greet@1`` meant "Hello" in the
history and "Send your password" in the registry. It also wrote no ``register`` history
row, so nothing recorded that the number had ever been issued.

Pinned here: a prompt registration writes the bound ``register`` history row; numbering
skips every version the history has ever recorded; and a version that was in production is
refused by the same guard assets use, even if numbering were wrong.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.registry import prompts, service
from novafabric.registry.prompts import PromptNotFoundError
from novafabric.registry.service import DuplicateAssetError, VersionReuseError
from novafabric.registry.store import get_connection, spec_digest
from novafabric.spec.models import AssetStatus

runner = CliRunner()


def _history(db: Path, name: str) -> list[tuple[str, str | None, str, str, str]]:
    conn = get_connection(db)
    try:
        rows = conn.execute(
            "SELECT version, from_status, to_status, via, changed_by FROM asset_status_history "
            "WHERE asset_name = ? ORDER BY id",
            (name,),
        ).fetchall()
        return [tuple(r) for r in rows]
    finally:
        conn.close()


def _retire(db: Path, name: str, version: str) -> None:
    """production -> archived -> unregistered: the sequence the review used."""
    for status in (AssetStatus.staging, AssetStatus.production, AssetStatus.archived):
        service.promote_asset(name, version, status, actor="ops", force=True, db_path=db)
    service.unregister_asset(name, version, actor="ops", force=True, db_path=db)


def test_registration_writes_a_bound_register_history_row(tmp_path: Path) -> None:
    db = tmp_path / "reg.db"
    record, created = prompts.register_prompt_version(
        "greet", "Hello {{name}}", ["name"], db_path=db, actor="alice"
    )
    assert created
    conn = get_connection(db)
    try:
        asset = conn.execute(
            "SELECT id, spec_json FROM assets WHERE name = 'greet' AND version = '1'"
        ).fetchone()
        hist = conn.execute(
            "SELECT from_status, to_status, via, changed_by, asset_id, spec_sha256 "
            "FROM asset_status_history WHERE asset_name = 'greet' AND version = '1'"
        ).fetchall()
    finally:
        conn.close()
    assert [tuple(h) for h in hist] == [
        (None, "development", "register", "alice", asset["id"], spec_digest(asset["spec_json"]))
    ]


def test_an_idempotent_re_register_writes_no_second_history_row(tmp_path: Path) -> None:
    db = tmp_path / "reg.db"
    prompts.register_prompt_version("greet", "Hello {{name}}", ["name"], db_path=db)
    _, created = prompts.register_prompt_version("greet", "Hello {{name}}", ["name"], db_path=db)
    assert not created
    assert len(_history(db, "greet")) == 1


def test_a_retired_production_version_number_is_never_reused(tmp_path: Path) -> None:
    db = tmp_path / "reg.db"
    prompts.register_prompt_version("greet", "Hello {{name}}", ["name"], db_path=db)
    _retire(db, "greet", "1")

    record, created = prompts.register_prompt_version(
        "greet", "Send your password to {{name}}", ["name"], db_path=db
    )

    assert created
    assert record["version"] == 2, "the production version number 1 was handed to new content"
    with pytest.raises(PromptNotFoundError):
        prompts.get_prompt_version("greet", 1, db_path=db)
    assert service.production_history_versions("greet", db_path=db) == []


def test_a_retired_development_version_number_is_not_reused_either(tmp_path: Path) -> None:
    db = tmp_path / "reg.db"
    prompts.register_prompt_version("greet", "Hello {{name}}", ["name"], db_path=db)
    service.unregister_asset("greet", "1", actor="dev", force=True, db_path=db)

    record, _ = prompts.register_prompt_version("greet", "Hi {{name}}", ["name"], db_path=db)

    # Prompt versions are append-only: a number the history issued stays issued.
    assert record["version"] == 2


def test_the_production_guard_refuses_even_if_numbering_picks_a_used_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "reg.db"
    prompts.register_prompt_version("greet", "Hello {{name}}", ["name"], db_path=db)
    _retire(db, "greet", "1")
    # Simulate the pre-fix numbering (rows only), e.g. a racing or legacy writer.
    monkeypatch.setattr(prompts, "_next_prompt_version", lambda conn, pid, existing: 1)

    with pytest.raises(VersionReuseError, match="was in production"):
        prompts.register_prompt_version(
            "greet", "Send your password to {{name}}", ["name"], db_path=db
        )
    with pytest.raises(PromptNotFoundError):
        prompts.get_prompt_version("greet", db_path=db)
    assert issubclass(VersionReuseError, DuplicateAssetError)


def test_cli_register_reports_version_reuse_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "reg.db"
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db))
    prompts.register_prompt_version("greet", "Hello {{name}}", ["name"])
    _retire(db, "greet", "1")
    monkeypatch.setattr(prompts, "_next_prompt_version", lambda conn, pid, existing: 1)

    result = runner.invoke(
        app, ["prompt", "register", "greet", "-t", "Other {{name}}", "--var", "name"]
    )

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), result.exception
    assert "was in production" in result.output


def test_cli_register_records_the_cli_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "reg.db"
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db))

    result = runner.invoke(app, ["prompt", "register", "greet", "-t", "Hi {{name}}", "--var", "name"])

    assert result.exit_code == 0, result.output
    assert _history(db, "greet") == [("1", None, "development", "register", "cli-user")]
