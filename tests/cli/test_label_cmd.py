"""CLI tests for `nova label` (ADR-0113 + ADR-0114, experimental).

Covers:
- `nova label --help` smoke (all eight subcommands present)
- set: first set, move with previous version, --reason, --json, no-op skip
- set failures: unknown version/asset exit 1, reserved 'latest' exit 1,
  mixed-case label exit 1, malformed asset argument exit 2
- get: resolve to version + hash, auto 'latest', --json, unset label exit 1
- list: table with auto latest + explicit labels, empty asset
- history: audit trail newest-first with reasons, one-label filter, --json
- end-to-end coexistence with `nova prompt register` (ADR-0112)
- protected labels (ADR-0114): protect/unprotect, direct set refused with
  guidance, propose-move + approve-move by two distinct identities applies,
  same-identity approve refused (SoD exit 1), --reject, status table/--json
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import novafabric.trust.keyring as kr
from novafabric.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "nova-home"))
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(tmp_path / "registry.db"))
    monkeypatch.setattr(kr, "_KEYRING_DIR", tmp_path / "keyring")


def _register(body: str = "You triage {ticket}.") -> None:
    result = runner.invoke(
        app, ["prompt", "register", "triage", "--template", body]
    )
    assert result.exit_code == 0, result.output


def test_label_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["label", "--help"])
    assert result.exit_code == 0
    for sub in (
        "set", "get", "list", "history",
        "protect", "propose-move", "approve-move", "status",
    ):
        assert sub in result.output


def test_set_first_and_move() -> None:
    _register()
    _register("v2 body")
    first = runner.invoke(app, ["label", "set", "prompt:triage", "production", "1"])
    assert first.exit_code == 0, first.output
    assert "(unset) → 1" in first.output

    move = runner.invoke(app, ["label", "set", "prompt:triage", "production", "2"])
    assert move.exit_code == 0
    assert "1 → 2" in move.output


def test_set_json_and_reason() -> None:
    _register()
    result = runner.invoke(
        app,
        ["label", "set", "prompt:triage", "canary", "1",
         "--reason", "trial run", "--json"],
    )
    assert result.exit_code == 0, result.output
    record = json.loads(result.output)
    assert record["label"] == "canary"
    assert record["target_version"] == "1"
    assert record["reason"] == "trial run"
    assert record["content_hash"].startswith("sha256:")
    assert record["move_id"]


def test_set_noop_records_nothing() -> None:
    _register()
    runner.invoke(app, ["label", "set", "prompt:triage", "production", "1"])
    noop = runner.invoke(app, ["label", "set", "prompt:triage", "production", "1"])
    assert noop.exit_code == 0
    assert "already points at" in noop.output
    history = runner.invoke(app, ["label", "history", "prompt:triage", "--json"])
    assert len(json.loads(history.output)) == 1


def test_set_failures_exit_1() -> None:
    _register()
    unknown_version = runner.invoke(
        app, ["label", "set", "prompt:triage", "production", "99"]
    )
    assert unknown_version.exit_code == 1
    assert "no version '99'" in unknown_version.output

    unknown_asset = runner.invoke(
        app, ["label", "set", "prompt:ghost", "production", "1"]
    )
    assert unknown_asset.exit_code == 1

    reserved = runner.invoke(app, ["label", "set", "prompt:triage", "latest", "1"])
    assert reserved.exit_code == 1
    assert "auto-maintained" in reserved.output

    mixed_case = runner.invoke(
        app, ["label", "set", "prompt:triage", "Production", "1"]
    )
    assert mixed_case.exit_code == 1


def test_malformed_asset_argument_exits_2() -> None:
    assert runner.invoke(app, ["label", "get", ":", "production"]).exit_code == 2


def test_get_resolves_version_and_hash() -> None:
    _register()
    runner.invoke(app, ["label", "set", "prompt:triage", "production", "1"])
    result = runner.invoke(app, ["label", "get", "prompt:triage", "production"])
    assert result.exit_code == 0
    assert "production → 1" in result.output

    as_json = runner.invoke(
        app, ["label", "get", "prompt:triage", "production", "--json"]
    )
    record = json.loads(as_json.output)
    assert record["target_version"] == "1"
    assert record["content_hash"].startswith("sha256:")


def test_get_latest_is_auto() -> None:
    _register()
    _register("v2 body")
    result = runner.invoke(app, ["label", "get", "prompt:triage", "latest"])
    assert result.exit_code == 0
    assert "latest → 2" in result.output
    assert "(auto)" in result.output


def test_get_unset_label_exits_1() -> None:
    _register()
    assert (
        runner.invoke(app, ["label", "get", "prompt:triage", "production"]).exit_code
        == 1
    )
    assert runner.invoke(app, ["label", "get", "prompt:ghost", "latest"]).exit_code == 1


def test_list_shows_latest_and_explicit_labels() -> None:
    _register()
    _register("v2 body")
    runner.invoke(app, ["label", "set", "prompt:triage", "production", "1"])
    runner.invoke(app, ["label", "set", "prompt:triage", "staging", "2"])
    result = runner.invoke(app, ["label", "list", "prompt:triage"])
    assert result.exit_code == 0
    for expected in ("latest (auto)", "production", "staging"):
        assert expected in result.output

    as_json = runner.invoke(app, ["label", "list", "prompt:triage", "--json"])
    labels = {r["label"] for r in json.loads(as_json.output)}
    assert labels == {"latest", "production", "staging"}


def test_list_unknown_asset_is_empty_not_error() -> None:
    result = runner.invoke(app, ["label", "list", "prompt:ghost"])
    assert result.exit_code == 0
    assert "No versions or labels" in result.output


def test_history_audit_trail() -> None:
    _register()
    _register("v2 body")
    runner.invoke(app, ["label", "set", "prompt:triage", "production", "1"])
    runner.invoke(app, ["label", "set", "prompt:triage", "production", "2"])
    runner.invoke(
        app,
        ["label", "set", "prompt:triage", "production", "1",
         "--reason", "rollback: v2 regressed"],
    )
    result = runner.invoke(app, ["label", "history", "prompt:triage", "production"])
    assert result.exit_code == 0
    assert "rollback" in result.output
    assert "(unset) → 1" in result.output

    as_json = runner.invoke(
        app, ["label", "history", "prompt:triage", "production", "--json"]
    )
    moves = json.loads(as_json.output)
    assert [m["target_version"] for m in moves] == ["1", "2", "1"]
    assert moves[0]["reason"] == "rollback: v2 regressed"

    empty = runner.invoke(app, ["label", "history", "prompt:ghost"])
    assert empty.exit_code == 0
    assert "No label moves" in empty.output

    bad_label = runner.invoke(app, ["label", "history", "prompt:triage", "BAD"])
    assert bad_label.exit_code == 1


def test_end_to_end_with_prompt_registry() -> None:
    """0112 + 0113 flow: register → label → resolve matches the pinned get."""
    _register()
    pinned = runner.invoke(app, ["prompt", "get", "triage@1", "--json"])
    content_hash = json.loads(pinned.output)["content_hash"]

    runner.invoke(app, ["label", "set", "prompt:triage", "production", "1"])
    resolved = runner.invoke(
        app, ["label", "get", "prompt:triage", "production", "--json"]
    )
    assert json.loads(resolved.output)["content_hash"] == content_hash


# ---------------------------------------------------------------------------
# Protected labels — maker-checker (ADR-0114)
# ---------------------------------------------------------------------------


def _protect_production() -> None:
    _register()
    _register("v2 body")
    assert (
        runner.invoke(app, ["label", "set", "prompt:triage", "production", "1"]).exit_code
        == 0
    )
    protect = runner.invoke(app, ["label", "protect", "prompt:triage", "production"])
    assert protect.exit_code == 0, protect.output


def test_protect_then_direct_set_refused_with_guidance() -> None:
    _protect_production()
    refused = runner.invoke(app, ["label", "set", "prompt:triage", "production", "2"])
    assert refused.exit_code == 1
    assert "protected" in refused.output
    assert "propose-move" in refused.output
    # The label did not move.
    current = runner.invoke(
        app, ["label", "get", "prompt:triage", "production", "--json"]
    )
    assert json.loads(current.output)["target_version"] == "1"


def test_protect_json_and_unprotect() -> None:
    _protect_production()
    config = runner.invoke(
        app,
        ["label", "protect", "prompt:triage", "production",
         "--required-approvals", "2", "--json"],
    )
    assert config.exit_code == 0, config.output
    record = json.loads(config.output)
    assert record["protected"] is True
    assert record["required_approvals"] == 2

    unprotect = runner.invoke(
        app, ["label", "protect", "prompt:triage", "production", "--unprotect"]
    )
    assert unprotect.exit_code == 0
    assert "free again" in unprotect.output
    moved = runner.invoke(app, ["label", "set", "prompt:triage", "production", "2"])
    assert moved.exit_code == 0


def test_protect_reserved_latest_exits_1() -> None:
    _register()
    result = runner.invoke(app, ["label", "protect", "prompt:triage", "latest"])
    assert result.exit_code == 1
    assert "auto-maintained" in result.output


def test_propose_and_approve_by_two_identities_moves_label() -> None:
    _protect_production()
    proposed = runner.invoke(
        app,
        ["label", "propose-move", "prompt:triage", "production", "--to", "2",
         "--reason", "v2 passed the eval gate", "--identity", "alice", "--json"],
    )
    assert proposed.exit_code == 0, proposed.output
    move = json.loads(proposed.output)
    assert move["state"] == "pending"

    approved = runner.invoke(
        app,
        ["label", "approve-move", "prompt:triage", "production", move["move_id"],
         "--identity", "bob"],
    )
    assert approved.exit_code == 0, approved.output
    assert "applied" in approved.output

    current = runner.invoke(
        app, ["label", "get", "prompt:triage", "production", "--json"]
    )
    assert json.loads(current.output)["target_version"] == "2"
    # The 0113 append-only history carries the applied move (same ULID).
    history = runner.invoke(
        app, ["label", "history", "prompt:triage", "production", "--json"]
    )
    rows = json.loads(history.output)
    assert rows[0]["move_id"] == move["move_id"]
    assert rows[0]["moved_by"] == "alice"


def test_same_identity_approve_refused_sod() -> None:
    _protect_production()
    proposed = runner.invoke(
        app,
        ["label", "propose-move", "prompt:triage", "production", "--to", "2",
         "--identity", "alice", "--json"],
    )
    move_id = json.loads(proposed.output)["move_id"]
    refused = runner.invoke(
        app,
        ["label", "approve-move", "prompt:triage", "production", move_id,
         "--identity", "alice"],
    )
    assert refused.exit_code == 1
    assert "SoD" in refused.output
    current = runner.invoke(
        app, ["label", "get", "prompt:triage", "production", "--json"]
    )
    assert json.loads(current.output)["target_version"] == "1"


def test_propose_on_free_label_exits_1_with_guidance() -> None:
    _register()
    result = runner.invoke(
        app,
        ["label", "propose-move", "prompt:triage", "staging", "--to", "1",
         "--identity", "alice"],
    )
    assert result.exit_code == 1
    assert "not protected" in result.output


def test_reject_move_is_terminal() -> None:
    _protect_production()
    proposed = runner.invoke(
        app,
        ["label", "propose-move", "prompt:triage", "production", "--to", "2",
         "--identity", "alice", "--json"],
    )
    move_id = json.loads(proposed.output)["move_id"]
    rejected = runner.invoke(
        app,
        ["label", "approve-move", "prompt:triage", "production", move_id,
         "--reject", "--identity", "bob"],
    )
    assert rejected.exit_code == 0
    assert "rejected" in rejected.output
    again = runner.invoke(
        app,
        ["label", "approve-move", "prompt:triage", "production", move_id,
         "--identity", "carol"],
    )
    assert again.exit_code == 1
    assert "terminal" in again.output


def test_status_table_and_json() -> None:
    _protect_production()
    runner.invoke(
        app,
        ["label", "propose-move", "prompt:triage", "production", "--to", "2",
         "--identity", "alice"],
    )
    table = runner.invoke(app, ["label", "status", "prompt:triage"])
    assert table.exit_code == 0, table.output
    assert "production" in table.output
    assert "yes" in table.output
    assert "pending" in table.output

    as_json = runner.invoke(
        app, ["label", "status", "prompt:triage", "--label", "production", "--json"]
    )
    status = json.loads(as_json.output)
    assert status["protections"][0]["protected"] is True
    assert status["pending_moves"][0]["state"] == "pending"
    assert status["pointers"][0]["target_version"] == "1"


def test_status_empty_asset() -> None:
    result = runner.invoke(app, ["label", "status", "prompt:ghost"])
    assert result.exit_code == 0
    assert "No labels" in result.output
