"""Protected-label maker-checker is bound to the ADR-0312 signer trust list.

Before this fix ``registry.protected_labels`` called the auto-creating
``ensure_keypair(identity)`` on both the propose and the approve path and compared
only the *typed* identity string and the freshly-derived fingerprint. One person
could therefore propose as ``alice`` and approve as ``alice-alt``: the second key
was minted on the spot, its fingerprint differed, the names differed, and the
protected label moved with a single human behind it.

These tests pin the closed behaviour:

* no key is ever created on the propose/approve path;
* both signatures are verified against the operator-held signer trust list
  (``NOVAFABRIC_PROMOTE_SIGNERS`` -> ``$NOVAFABRIC_HOME/promote-signers.yaml``),
  with the ``proposer`` role for the maker and ``approver`` for the checker;
* separation of duties compares the **bound** identity and key fingerprint;
* the stored maker signature is re-verified at approve time, and only
  trust-verified approvals count toward ``required_approvals``.
"""

from __future__ import annotations

import base64
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from typer.testing import CliRunner

import novafabric.trust.keyring as kr
from novafabric.cli.main import app
from novafabric.registry.labels import get_label, set_label
from novafabric.registry.prompts import register_prompt_version
from novafabric.registry.protected_labels import (
    SelfApprovalError,
    approve_move,
    get_move,
    list_moves,
    propose_move,
    protect_label,
)
from novafabric.registry.signers import (
    InvalidSignatureError,
    SignerIdentityMismatchError,
    SignerTrustListError,
    UntrustedSignerError,
)
from novafabric.trust.keyring import KeyNotFoundError

runner = CliRunner()


def _pub_b64(identity: str) -> str:
    """Create (explicit keygen in test setup) and return the raw public key, base64."""
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

    def bind(self, entries: list[tuple[str, str, list[str]]]) -> None:
        """Write the trust list: (bound identity, keyring identity holding the key, roles)."""
        rows: list[dict[str, Any]] = [
            {"identity": bound, "public_key": _pub_b64(holder), "roles": roles}
            for bound, holder, roles in entries
        ]
        self.trust.write_text(yaml.safe_dump({"signers": rows}), encoding="utf-8")

    def target(self) -> str:
        return str(get_label("triage", "production", db_path=self.db)["target_version"])


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    e = Env(tmp_path)
    monkeypatch.setattr(kr, "_KEYRING_DIR", e.keyring)
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(e.db))
    monkeypatch.setenv("NOVAFABRIC_PROMOTE_SIGNERS", str(e.trust))
    register_prompt_version(prompt_id="triage", template="v1 body", db_path=e.db)
    register_prompt_version(prompt_id="triage", template="v2 body", db_path=e.db)
    set_label("triage", "production", "1", db_path=e.db)
    protect_label("triage", "production", db_path=e.db)
    # alice is a maker, bob a checker: two people, two keys, explicitly bound.
    e.bind([("alice", "alice", ["proposer"]), ("bob", "bob", ["approver"])])
    return e


def _propose(env: Env, identity: str = "alice") -> dict[str, Any]:
    return propose_move("triage", "production", "2", identity=identity, db_path=env.db)


# ---------------------------------------------------------------------------
# The reported attack: one person, a second --identity, a key minted on the spot
# ---------------------------------------------------------------------------


def test_second_identity_cannot_self_approve_with_a_minted_key(env: Env) -> None:
    move = _propose(env)
    alt_key = kr._key_path("alice-alt")
    assert not alt_key.exists()
    with pytest.raises(KeyNotFoundError, match="nova promote keygen"):
        approve_move(
            "triage", "production", move["move_id"], identity="alice-alt", db_path=env.db
        )
    assert not alt_key.exists(), "the approve path minted a key for an unseen identity"
    assert env.target() == "1"
    assert get_move("triage", move["move_id"], db_path=env.db)["approvals"] == []


def test_existing_but_unlisted_key_cannot_approve(env: Env) -> None:
    move = _propose(env)
    kr.ensure_keypair("alice-alt", create=True)  # explicit keygen, never bound
    with pytest.raises(UntrustedSignerError) as exc_info:
        approve_move(
            "triage", "production", move["move_id"], identity="alice-alt", db_path=env.db
        )
    assert exc_info.value.party == "approver"
    assert env.target() == "1"


def test_key_bound_to_another_identity_is_refused(env: Env) -> None:
    """A copied checker key used under a different name does not inherit the binding."""
    move = _propose(env)
    kr._key_path("mallory").write_bytes(kr._key_path("bob").read_bytes())
    with pytest.raises(SignerIdentityMismatchError):
        approve_move(
            "triage", "production", move["move_id"], identity="mallory", db_path=env.db
        )
    assert env.target() == "1"


def test_same_bound_identity_with_both_roles_cannot_self_approve(env: Env) -> None:
    env.bind([("alice", "alice", ["proposer", "approver"]), ("bob", "bob", ["approver"])])
    move = _propose(env)
    with pytest.raises(SelfApprovalError):
        approve_move(
            "triage", "production", move["move_id"], identity="alice", db_path=env.db
        )
    assert env.target() == "1"


def test_trusted_distinct_checker_applies(env: Env) -> None:
    move = _propose(env)
    record, detail = approve_move(
        "triage", "production", move["move_id"], identity="bob", db_path=env.db
    )
    assert detail == "applied"
    assert record["approvals"][0]["approver"] == "bob"
    assert env.target() == "2"


# ---------------------------------------------------------------------------
# Maker side, roles, and a missing trust list
# ---------------------------------------------------------------------------


def test_untrusted_proposer_refused_and_nothing_recorded(env: Env) -> None:
    kr.ensure_keypair("eve", create=True)
    with pytest.raises(UntrustedSignerError) as exc_info:
        _propose(env, identity="eve")
    assert exc_info.value.party == "proposer"
    assert list_moves("triage", db_path=env.db) == []


def test_propose_never_mints_a_key(env: Env) -> None:
    with pytest.raises(KeyNotFoundError):
        _propose(env, identity="nobody")
    assert not kr._key_path("nobody").exists()
    assert list_moves("triage", db_path=env.db) == []


def test_roles_are_enforced(env: Env) -> None:
    with pytest.raises(UntrustedSignerError, match="proposer"):
        _propose(env, identity="bob")  # bob is approver-only
    move = _propose(env)
    env.bind([("alice", "alice", ["proposer"]), ("bob", "bob", ["proposer"])])
    with pytest.raises(UntrustedSignerError, match="approver"):
        approve_move(
            "triage", "production", move["move_id"], identity="bob", db_path=env.db
        )
    assert env.target() == "1"


def test_missing_trust_list_fails_closed(env: Env) -> None:
    move = _propose(env)
    env.trust.unlink()
    with pytest.raises(SignerTrustListError):
        approve_move("triage", "production", move["move_id"], identity="bob", db_path=env.db)
    with pytest.raises(SignerTrustListError):
        propose_move("triage", "production", "2", identity="alice", db_path=env.db)
    assert env.target() == "1"


# ---------------------------------------------------------------------------
# Tamper, removal from the list, and legacy rows
# ---------------------------------------------------------------------------


def test_edited_pending_move_is_refused(env: Env) -> None:
    register_prompt_version(prompt_id="triage", template="v3 body", db_path=env.db)
    move = _propose(env)
    conn = sqlite3.connect(env.db)
    conn.execute(
        "UPDATE asset_label_pending_moves SET proposed_version = '3' WHERE move_id = ?",
        (move["move_id"],),
    )
    conn.commit()
    conn.close()
    with pytest.raises(InvalidSignatureError) as exc_info:
        approve_move("triage", "production", move["move_id"], identity="bob", db_path=env.db)
    assert exc_info.value.party == "proposer"
    assert env.target() == "1"


def test_maker_removed_from_list_cannot_be_approved(env: Env) -> None:
    move = _propose(env)
    env.bind([("bob", "bob", ["approver"])])
    with pytest.raises(UntrustedSignerError) as exc_info:
        approve_move("triage", "production", move["move_id"], identity="bob", db_path=env.db)
    assert exc_info.value.party == "proposer"
    assert env.target() == "1"


def test_unverifiable_stored_approval_does_not_count(env: Env) -> None:
    """A pre-fix approval made with a minted key is recorded but never counted."""
    protect_label("triage", "production", required_approvals=2, db_path=env.db)
    env.bind([
        ("alice", "alice", ["proposer"]),
        ("bob", "bob", ["approver"]),
        ("carol", "carol", ["approver"]),
    ])
    move = _propose(env)
    key, fp = kr.ensure_keypair("alice-alt", create=True)
    approved_at = "2026-10-22T00:00:00+00:00"
    from novafabric.registry.protected_labels import approval_payload

    sig = kr.sign_payload(
        key, approval_payload(move["move_id"], "approve", "alice-alt", approved_at)
    )
    conn = sqlite3.connect(env.db)
    conn.execute(
        "INSERT INTO asset_label_move_approvals (approval_id, move_id, approver, "
        "approver_key_fp, approver_sig, decision, note, approved_at) "
        "VALUES ('legacy-1', ?, 'alice-alt', ?, ?, 'approve', NULL, ?)",
        (move["move_id"], fp, sig, approved_at),
    )
    conn.commit()
    conn.close()

    record, detail = approve_move(
        "triage", "production", move["move_id"], identity="bob", db_path=env.db
    )
    assert detail == "pending (1/2 distinct approvals)"
    assert env.target() == "1"
    record, detail = approve_move(
        "triage", "production", move["move_id"], identity="carol", db_path=env.db
    )
    assert detail == "applied"
    assert env.target() == "2"


def test_trusted_checker_can_reject_an_unverifiable_legacy_move(env: Env) -> None:
    """A move proposed before the fix (untrusted maker key) must not block the label forever."""
    env.bind([("eve", "eve", ["proposer"]), ("bob", "bob", ["approver"])])
    legacy = _propose(env, identity="eve")
    env.bind([("alice", "alice", ["proposer"]), ("bob", "bob", ["approver"])])
    with pytest.raises(UntrustedSignerError):
        approve_move("triage", "production", legacy["move_id"], identity="bob", db_path=env.db)
    record, detail = approve_move(
        "triage", "production", legacy["move_id"], identity="bob", reject=True,
        db_path=env.db,
    )
    assert detail == "rejected"
    assert env.target() == "1"
    fresh = _propose(env)
    assert fresh["state"] == "pending"


def test_reject_still_requires_a_trusted_checker(env: Env) -> None:
    move = _propose(env)
    kr.ensure_keypair("alice-alt", create=True)
    with pytest.raises(UntrustedSignerError):
        approve_move(
            "triage", "production", move["move_id"], identity="alice-alt", reject=True,
            db_path=env.db,
        )
    assert get_move("triage", move["move_id"], db_path=env.db)["state"] == "pending"


# ---------------------------------------------------------------------------
# CLI exit codes (consistent with `nova promote`: 3 maker side, 4 checker side)
# ---------------------------------------------------------------------------


def _cli_propose(identity: str) -> Any:
    return runner.invoke(
        app,
        ["label", "propose-move", "prompt:triage", "production", "--to", "2",
         "--identity", identity, "--json"],
    )


def _cli_approve(move_id: str, identity: str) -> Any:
    return runner.invoke(
        app,
        ["label", "approve-move", "prompt:triage", "production", move_id,
         "--identity", identity],
    )


def test_cli_minted_identity_exits_1_with_keygen_guidance(env: Env) -> None:
    move_id = _propose(env)["move_id"]
    result = _cli_approve(move_id, "alice-alt")
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "nova promote keygen" in result.output
    assert not kr._key_path("alice-alt").exists()
    assert env.target() == "1"


def test_cli_untrusted_checker_exits_4(env: Env) -> None:
    move_id = _propose(env)["move_id"]
    kr.ensure_keypair("alice-alt", create=True)
    result = _cli_approve(move_id, "alice-alt")
    assert result.exit_code == 4, result.output
    assert isinstance(result.exception, SystemExit)
    assert "trust list" in result.output
    assert env.target() == "1"


def test_cli_untrusted_maker_exits_3(env: Env) -> None:
    kr.ensure_keypair("eve", create=True)
    result = _cli_propose("eve")
    assert result.exit_code == 3, result.output
    assert isinstance(result.exception, SystemExit)


def test_cli_happy_path(env: Env) -> None:
    proposed = _cli_propose("alice")
    assert proposed.exit_code == 0, proposed.output
    import json

    move_id = json.loads(proposed.output)["move_id"]
    approved = _cli_approve(move_id, "bob")
    assert approved.exit_code == 0, approved.output
    assert "applied" in approved.output
    assert env.target() == "2"
