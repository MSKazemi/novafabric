"""Weakening a protected label needs the same maker-checker as moving it (ADR-0114).

Attack (adversarial review, 2026-10-22): one person moved a protected
``production`` label with no second principal::

    nova label protect prompt:triage production --unprotect     # exit 0
    nova label set     prompt:triage production 2               # exit 0
    nova label protect prompt:triage production -n 2            # exit 0

``protect_label(protected=False)`` was ungated, so the two-person rule on moves
could be switched off by one person and switched back on afterwards.

Pinned here: removing protection, lowering ``required_approvals`` or changing an
existing ``policy_ref`` is a *relaxation*. It is proposed by a trust-listed maker
and applies only after as many distinct trust-listed checkers approve as the
protection in force requires (ADR-0312 trust list). Raising protection stays a
single-actor action.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from typer.testing import CliRunner

import novafabric.trust.keyring as kr
from novafabric.cli.main import app
from novafabric.registry import protected_labels as pl
from novafabric.registry.labels import LabelError, get_label, set_label
from novafabric.registry.prompts import register_prompt_version
from novafabric.registry.signers import UntrustedSignerError

runner = CliRunner()


def _pub_b64(identity: str) -> str:
    key, _ = kr.ensure_keypair(identity, create=True)
    raw = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return base64.b64encode(raw).decode()


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.db = tmp_path / "registry.db"
        self.trust = tmp_path / "promote-signers.yaml"
        self.keyring = tmp_path / "keyring"

    def bind(self, entries: list[tuple[str, list[str]]]) -> None:
        rows: list[dict[str, Any]] = [
            {"identity": who, "public_key": _pub_b64(who), "roles": roles}
            for who, roles in entries
        ]
        self.trust.write_text(yaml.safe_dump({"signers": rows}), encoding="utf-8")

    def target(self) -> str:
        return str(get_label("triage", "production", db_path=self.db)["target_version"])

    def protection(self) -> dict[str, Any] | None:
        return pl.get_protection("triage", "production", db_path=self.db)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.chdir(tmp_path)
    e = Env(tmp_path)
    monkeypatch.setattr(kr, "_KEYRING_DIR", e.keyring)
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(e.db))
    monkeypatch.setenv("NOVAFABRIC_PROMOTE_SIGNERS", str(e.trust))
    register_prompt_version(prompt_id="triage", template="v1", db_path=e.db)
    register_prompt_version(prompt_id="triage", template="v2-evil", db_path=e.db)
    set_label("triage", "production", "1", db_path=e.db)
    pl.protect_label("triage", "production", required_approvals=2, db_path=e.db)
    e.bind(
        [
            ("alice", ["proposer"]),
            ("bob", ["approver"]),
            ("carol", ["approver"]),
        ]
    )
    return e


def _invoke(args: list[str]) -> Any:
    res = runner.invoke(app, args)
    assert isinstance(res.exception, SystemExit) or res.exception is None, res.exception
    return res


# ---------------------------------------------------------------------------
# The reported attack, end to end through the CLI
# ---------------------------------------------------------------------------


def test_one_person_cannot_unprotect_move_and_reprotect(env: Env) -> None:
    res = _invoke(["label", "protect", "prompt:triage", "production", "--unprotect"])
    assert res.exit_code == 1, res.output  # the OS user has no trusted maker key
    assert "maker-checker" in res.output
    assert env.protection() is not None and env.protection()["protected"] is True

    res = _invoke(["label", "set", "prompt:triage", "production", "2"])
    assert res.exit_code == 1, res.output
    assert env.target() == "1"


def test_lowering_required_approvals_is_not_single_actor(env: Env) -> None:
    res = _invoke(
        ["label", "protect", "prompt:triage", "production", "--required-approvals", "1",
         "--identity", "alice"]
    )
    assert res.exit_code == 0, res.output
    assert "proposed protection change" in res.output
    assert env.protection()["required_approvals"] == 2


def test_raising_protection_stays_single_actor(env: Env) -> None:
    res = _invoke(
        ["label", "protect", "prompt:triage", "production", "--required-approvals", "3"]
    )
    assert res.exit_code == 0, res.output
    assert env.protection()["required_approvals"] == 3


def test_relaxation_without_a_trusted_maker_is_refused(env: Env) -> None:
    kr.ensure_keypair("mallory", create=True)  # a key, but not on the trust list
    res = _invoke(
        ["label", "protect", "prompt:triage", "production", "--unprotect",
         "--identity", "mallory"]
    )
    assert res.exit_code == 3, res.output
    assert pl.list_protection_changes("triage", db_path=env.db) == []
    assert env.protection()["protected"] is True


# ---------------------------------------------------------------------------
# Python API
# ---------------------------------------------------------------------------


def test_protect_label_refuses_a_relaxation(env: Env) -> None:
    with pytest.raises(pl.ProtectionRelaxationRequiresApprovalError):
        pl.protect_label("triage", "production", protected=False, db_path=env.db)
    with pytest.raises(pl.ProtectionRelaxationRequiresApprovalError):
        pl.protect_label("triage", "production", required_approvals=1, db_path=env.db)
    assert env.protection()["protected"] is True
    assert env.protection()["required_approvals"] == 2


def test_changing_an_existing_policy_ref_is_a_relaxation(env: Env, tmp_path: Path) -> None:
    strict = tmp_path / "strict.rego"
    strict.write_text("package x\n")
    pl.protect_label(
        "triage", "production", required_approvals=2, policy_ref=str(strict), db_path=env.db
    )
    with pytest.raises(pl.ProtectionRelaxationRequiresApprovalError):
        pl.protect_label("triage", "production", required_approvals=2, db_path=env.db)


def test_relaxation_applies_only_after_the_current_quorum(env: Env) -> None:
    change = pl.propose_protection_change(
        "triage", "production", protected=False, identity="alice", db_path=env.db
    )
    assert change["state"] == "pending"
    record, detail = pl.approve_protection_change(
        "triage", "production", change["change_id"], identity="bob", db_path=env.db
    )
    assert record["state"] == "pending", detail
    assert env.protection()["protected"] is True
    record, detail = pl.approve_protection_change(
        "triage", "production", change["change_id"], identity="carol", db_path=env.db
    )
    assert record["state"] == "applied", detail
    assert env.protection()["protected"] is False


def test_the_maker_cannot_approve_their_own_relaxation(env: Env) -> None:
    env.bind([("alice", ["proposer", "approver"]), ("bob", ["approver"])])
    change = pl.propose_protection_change(
        "triage", "production", protected=False, identity="alice", db_path=env.db
    )
    with pytest.raises(pl.SelfApprovalError):
        pl.approve_protection_change(
            "triage", "production", change["change_id"], identity="alice", db_path=env.db
        )
    assert env.protection()["protected"] is True


def test_an_unlisted_checker_does_not_count(env: Env) -> None:
    change = pl.propose_protection_change(
        "triage", "production", protected=False, identity="alice", db_path=env.db
    )
    kr.ensure_keypair("alice-alt", create=True)
    with pytest.raises(UntrustedSignerError):
        pl.approve_protection_change(
            "triage", "production", change["change_id"], identity="alice-alt", db_path=env.db
        )
    assert env.protection()["protected"] is True


def test_a_rejected_relaxation_never_applies(env: Env) -> None:
    change = pl.propose_protection_change(
        "triage", "production", protected=False, identity="alice", db_path=env.db
    )
    record, _ = pl.approve_protection_change(
        "triage", "production", change["change_id"], identity="bob", reject=True,
        db_path=env.db,
    )
    assert record["state"] == "rejected"
    with pytest.raises(pl.MoveStateError):
        pl.approve_protection_change(
            "triage", "production", change["change_id"], identity="carol", db_path=env.db
        )
    assert env.protection()["protected"] is True


def test_a_stale_relaxation_cannot_apply_after_the_config_changed(env: Env) -> None:
    """Approvals for 'drop 2 -> 1' must not apply over a config raised to 5 meanwhile."""
    change = pl.propose_protection_change(
        "triage", "production", required_approvals=1, identity="alice", db_path=env.db
    )
    pl.protect_label("triage", "production", required_approvals=5, db_path=env.db)
    with pytest.raises(pl.MoveStateError, match="changed"):
        pl.approve_protection_change(
            "triage", "production", change["change_id"], identity="bob", db_path=env.db
        )
    assert pl.find_protection_change("triage", change["change_id"], db_path=env.db)[
        "state"
    ] == "superseded"
    with pytest.raises(pl.MoveStateError):
        pl.approve_protection_change(
            "triage", "production", change["change_id"], identity="carol", db_path=env.db
        )
    assert env.protection()["required_approvals"] == 5


def test_an_edited_change_row_is_refused(env: Env) -> None:
    """The maker signature covers the requested config; editing the row breaks it."""
    import sqlite3

    change = pl.propose_protection_change(
        "triage", "production", required_approvals=1, identity="alice", db_path=env.db
    )
    conn = sqlite3.connect(env.db)
    conn.execute(
        "UPDATE asset_label_protection_changes SET protected = 0 WHERE change_id = ?",
        (change["change_id"],),
    )
    conn.commit()
    conn.close()
    from novafabric.registry.signers import InvalidSignatureError

    with pytest.raises(InvalidSignatureError):
        pl.approve_protection_change(
            "triage", "production", change["change_id"], identity="bob", db_path=env.db
        )


def test_proposing_a_strengthening_is_refused(env: Env) -> None:
    with pytest.raises(LabelError, match="not a relaxation"):
        pl.propose_protection_change(
            "triage", "production", required_approvals=3, identity="alice", db_path=env.db
        )


# ---------------------------------------------------------------------------
# CLI: approve-move resolves a protection-change id; status lists it
# ---------------------------------------------------------------------------


def test_cli_full_relaxation_flow(env: Env) -> None:
    res = _invoke(
        ["label", "protect", "prompt:triage", "production", "--unprotect",
         "--identity", "alice", "--json"]
    )
    assert res.exit_code == 0, res.output
    import json

    change_id = json.loads(res.output)["change_id"]
    status = _invoke(["label", "status", "prompt:triage", "--json"])
    assert [c["change_id"] for c in json.loads(status.output)["protection_changes"]] == [
        change_id
    ]
    for checker in ("bob", "carol"):
        res = _invoke(
            ["label", "approve-move", "prompt:triage", "production", change_id,
             "--identity", checker]
        )
        assert res.exit_code == 0, res.output
    assert "applied protection change" in res.output
    assert env.protection()["protected"] is False
    res = _invoke(["label", "set", "prompt:triage", "production", "2"])
    assert res.exit_code == 0, res.output
