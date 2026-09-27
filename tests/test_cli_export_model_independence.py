"""ADR-0159 D2 / NF-276 — the ``nova export-model-independence`` CLI.

Read-only. Exit 0 on render (a ``missing`` independence field is valid, honest output); exit 2
only on an unreadable store or a malformed model id. The honesty banner is always printed.
"""

from __future__ import annotations

import json
from pathlib import Path

from _help_assert import assert_flag_in_help, strip_ansi
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.registry.store import get_connection, init_schema

runner = CliRunner()


def _db(tmp_path: Path, approver: str) -> Path:
    db = tmp_path / "registry.db"
    conn = get_connection(db)
    init_schema(conn, force=True)
    conn.execute(
        "INSERT INTO promotion_proposals (proposal_id, asset_name, asset_version, to_status, "
        "proposer, proposer_key_fp, proposer_sig, proposed_at, state, approver, "
        "approver_key_fp, approver_sig, approved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "p1",
            "scorer",
            "1",
            "staging",
            "alice",
            "fp-a",
            "s",
            "2026-01-01T00:00:00",
            "approved",
            approver,
            "fp-b",
            "s2",
            "2026-01-02T00:00:00",
        ),
    )
    conn.commit()
    conn.close()
    return db


def _args(tmp_path: Path, db: Path, *extra: str) -> list[str]:
    return [
        "export-model-independence",
        "--model",
        "scorer",
        "--db",
        str(db),
        "--data-dir",
        str(tmp_path / "seal"),
        "--policy-db",
        str(tmp_path / "seal" / "m.db"),
        *extra,
    ]


def test_independent_renders_complete(tmp_path: Path) -> None:
    result = runner.invoke(app, _args(tmp_path, _db(tmp_path, "bob")))
    assert result.exit_code == 0, result.output
    # Rich colourises when FORCE_COLOR is set (CI); assert the rendered text
    rendered = strip_ansi(result.output)
    assert "independence" in rendered and "complete" in rendered
    assert "maker=alice checker=bob" in rendered
    assert "does not guarantee" in " ".join(rendered.split())  # honesty banner


def test_single_identity_json_exit_zero(tmp_path: Path) -> None:
    result = runner.invoke(app, _args(tmp_path, _db(tmp_path, "alice"), "--json"))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["independence"]["status"] == "missing"
    assert payload["independence"]["reason"] == "single-identity approval"
    assert payload["banner"]
    assert "rating" not in payload and "verdict" not in payload


def test_no_registry_and_unknown_capsule_is_missing(tmp_path: Path) -> None:
    result = runner.invoke(
        app, _args(tmp_path, tmp_path / "absent.db", "--capsule-id", "cap-x", "--json")
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["independence"]["status"] == "missing"
    assert not (tmp_path / "absent.db").exists()


def test_malformed_model_exits_two(tmp_path: Path) -> None:
    args = _args(tmp_path, _db(tmp_path, "bob"))
    args[2] = "scorer@"
    result = runner.invoke(app, args)
    assert result.exit_code == 2


def test_corrupt_registry_exits_two(tmp_path: Path) -> None:
    db = tmp_path / "bad.db"
    db.write_bytes(b"not sqlite" * 200)
    result = runner.invoke(app, _args(tmp_path, db))
    assert result.exit_code == 2


def test_help_lists_command() -> None:
    result = runner.invoke(app, ["export-model-independence", "--help"])
    assert result.exit_code == 0
    assert_flag_in_help(result, "--model")
