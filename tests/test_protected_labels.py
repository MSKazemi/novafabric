"""Protected-label maker-checker tests (ADR-0114, protected-labels v0).

Covers:
- JSON Schema conformance: 5 valid + 8 invalid golden fixtures (13/13)
- LabelProtectionConfig / PendingLabelMove model validation
- Free labels unchanged; protected direct set refused with guidance
- Maker-checker flow: propose (Ed25519-signed), approve by a distinct
  principal applies atomically (ADR-0113 history row shares the move ULID)
- SoD at the crypto level: same identity AND same keypair both refused
- required_approvals=2, duplicate approver recorded but counted once
- reject/expire terminal states; one non-terminal pending move per label
- Policy gate: missing policy_ref file fails closed; custom engine deny
  keeps the move pending; allow applies
- Append-only SQL trigger enforcement on the new tables
- status/list/get accessors
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

import novafabric.trust.keyring as kr
from novafabric.registry.labels import (
    LabelError,
    LabelTargetNotFoundError,
    ProtectedLabelError,
    ReservedLabelError,
    get_label,
    label_history,
    set_label,
)
from novafabric.registry.prompts import register_prompt_version
from novafabric.registry.protected_labels import (
    MoveStateError,
    NotProtectedError,
    PendingMoveExistsError,
    PendingMoveNotFoundError,
    SelfApprovalError,
    approval_payload,
    approve_move,
    get_move,
    get_protection,
    label_status,
    list_moves,
    list_protections,
    proposal_payload,
    propose_move,
    protect_label,
)
from novafabric.spec.protected_labels import (
    LabelProtectionConfig,
    PendingLabelMove,
)
from novafabric.trust.keyring import ensure_keypair, verify_sig

REPO_ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures" / "protected_labels"

CONFIG_SCHEMA = json.loads(
    (REPO_ROOT / "schemas" / "label-protection-config.schema.json").read_text()
)
PENDING_SCHEMA = json.loads(
    (REPO_ROOT / "schemas" / "protected-label-pending-move.schema.json").read_text()
)


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _isolated_keyring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kr, "_KEYRING_DIR", tmp_path / "keyring")


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


def _register(db_path: Path, prompt_id: str = "triage", body: str = "v1 body") -> dict[str, Any]:
    record, _ = register_prompt_version(
        prompt_id=prompt_id, template=body, db_path=db_path
    )
    return record


def _protected_prod(db_path: Path, **kwargs: Any) -> None:
    """Register two versions and protect 'production' pointing at v1."""
    _register(db_path)
    _register(db_path, body="v2 body")
    set_label("triage", "production", "1", db_path=db_path)
    protect_label("triage", "production", db_path=db_path, **kwargs)


# ---------------------------------------------------------------------------
# Schema conformance (golden fixtures)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["config-valid.json", "config-valid-minimal.json"])
def test_valid_config_fixtures_pass_schema(name: str) -> None:
    Draft202012Validator(CONFIG_SCHEMA).validate(_fixture(name))
    LabelProtectionConfig.model_validate(_fixture(name))


@pytest.mark.parametrize(
    "name",
    [
        "config-invalid-approvals-zero.json",
        "config-invalid-missing-protected.json",
        "config-invalid-unknown-key.json",
    ],
)
def test_invalid_config_fixtures_rejected(name: str) -> None:
    errors = list(Draft202012Validator(CONFIG_SCHEMA).iter_errors(_fixture(name)))
    assert errors, f"{name} unexpectedly passed the config schema"
    with pytest.raises(ValidationError):
        LabelProtectionConfig.model_validate(_fixture(name))


@pytest.mark.parametrize(
    "name",
    ["pending-valid.json", "pending-valid-applied.json", "pending-valid-first-set.json"],
)
def test_valid_pending_fixtures_pass_schema(name: str) -> None:
    Draft202012Validator(PENDING_SCHEMA).validate(_fixture(name))
    PendingLabelMove.model_validate(_fixture(name))


@pytest.mark.parametrize(
    "name",
    [
        "pending-invalid-bad-decision.json",
        "pending-invalid-bad-state.json",
        "pending-invalid-bad-ulid.json",
        "pending-invalid-missing-approvals.json",
        "pending-invalid-unknown-key.json",
    ],
)
def test_invalid_pending_fixtures_rejected(name: str) -> None:
    errors = list(Draft202012Validator(PENDING_SCHEMA).iter_errors(_fixture(name)))
    assert errors, f"{name} unexpectedly passed the pending-move schema"
    with pytest.raises(ValidationError):
        PendingLabelMove.model_validate(_fixture(name))


# ---------------------------------------------------------------------------
# Protection config (D1)
# ---------------------------------------------------------------------------


def test_unprotected_labels_unchanged(db_path: Path) -> None:
    """Free labels keep their one-command ADR-0113 behaviour."""
    _register(db_path)
    _register(db_path, body="v2 body")
    set_label("triage", "production", "1", db_path=db_path)
    record, moved = set_label("triage", "production", "2", db_path=db_path)
    assert moved is True
    assert get_label("triage", "production", db_path=db_path)["target_version"] == "2"
    assert get_protection("triage", "production", db_path=db_path) is None


def test_protect_refuses_direct_set_with_guidance(db_path: Path) -> None:
    _protected_prod(db_path)
    with pytest.raises(ProtectedLabelError, match="propose-move"):
        set_label("triage", "production", "2", db_path=db_path)
    # Nothing was written: the pointer and history are unchanged.
    assert get_label("triage", "production", db_path=db_path)["target_version"] == "1"
    assert len(label_history("triage", "production", db_path=db_path)) == 1


def test_protect_does_not_move_the_label(db_path: Path) -> None:
    _protected_prod(db_path)
    config = get_protection("triage", "production", db_path=db_path)
    assert config is not None
    assert config["protected"] is True
    assert config["required_approvals"] == 1
    Draft202012Validator(CONFIG_SCHEMA).validate(config)
    assert get_label("triage", "production", db_path=db_path)["target_version"] == "1"


def test_unprotect_restores_free_behaviour(db_path: Path) -> None:
    _protected_prod(db_path)
    protect_label("triage", "production", protected=False, db_path=db_path)
    record, moved = set_label("triage", "production", "2", db_path=db_path)
    assert moved is True
    # Projection: the newest event wins; both events remain (append-only).
    config = get_protection("triage", "production", db_path=db_path)
    assert config is not None and config["protected"] is False


def test_protect_validation_failures(db_path: Path) -> None:
    with pytest.raises(ReservedLabelError):
        protect_label("triage", "latest", db_path=db_path)
    with pytest.raises(ValueError):
        protect_label("triage", "Production", db_path=db_path)
    with pytest.raises(ValueError):
        protect_label("triage", "production", required_approvals=0, db_path=db_path)


def test_other_labels_on_same_asset_stay_free(db_path: Path) -> None:
    _protected_prod(db_path)
    record, moved = set_label("triage", "staging", "2", db_path=db_path)
    assert moved is True


def test_list_protections_projection(db_path: Path) -> None:
    _protected_prod(db_path, required_approvals=2, note="gates live traffic")
    protect_label("triage", "canary", db_path=db_path)
    protect_label("triage", "canary", protected=False, db_path=db_path)
    records = list_protections("triage", db_path=db_path)
    by_label = {r["label"]: r for r in records}
    assert by_label["production"]["protected"] is True
    assert by_label["production"]["required_approvals"] == 2
    assert by_label["canary"]["protected"] is False


# ---------------------------------------------------------------------------
# Maker step (D2.1)
# ---------------------------------------------------------------------------


def test_propose_requires_protection(db_path: Path) -> None:
    _register(db_path)
    _register(db_path, body="v2 body")
    set_label("triage", "production", "1", db_path=db_path)
    with pytest.raises(NotProtectedError, match="nova label set"):
        propose_move("triage", "production", "2", identity="alice", db_path=db_path)


def test_propose_fails_closed_on_unknown_target(db_path: Path) -> None:
    _protected_prod(db_path)
    with pytest.raises(LabelTargetNotFoundError):
        propose_move("triage", "production", "99", identity="alice", db_path=db_path)
    assert list_moves("triage", db_path=db_path) == []


def test_propose_noop_target_refused(db_path: Path) -> None:
    _protected_prod(db_path)
    with pytest.raises(LabelError, match="already points at"):
        propose_move("triage", "production", "1", identity="alice", db_path=db_path)


def test_propose_creates_signed_pending_move(db_path: Path) -> None:
    _protected_prod(db_path)
    record = propose_move(
        "triage", "production", "2",
        reason="v2 passed the eval gate", identity="alice", db_path=db_path,
    )
    assert record["state"] == "pending"
    assert record["from_version"] == "1"
    assert record["proposed_version"] == "2"
    assert record["proposed_by"] == "alice"
    assert record["approvals"] == []
    assert record["required_approvals"] == 1
    Draft202012Validator(PENDING_SCHEMA).validate(record)
    PendingLabelMove.model_validate(record)
    # The proposal signature is a real Ed25519 signature by alice's key.
    key, fp = ensure_keypair("alice")
    assert record["proposer_key_fp"] == fp
    payload = proposal_payload(
        record["move_id"], "triage", "production", "1", "2", record["proposed_at"]
    )
    assert verify_sig(key.public_key(), record["proposer_signature"], payload)
    # The label did NOT move.
    assert get_label("triage", "production", db_path=db_path)["target_version"] == "1"


def test_second_propose_refused_while_pending(db_path: Path) -> None:
    _protected_prod(db_path)
    first = propose_move("triage", "production", "2", identity="alice", db_path=db_path)
    with pytest.raises(PendingMoveExistsError, match=first["move_id"]):
        propose_move("triage", "production", "2", identity="bob", db_path=db_path)


# ---------------------------------------------------------------------------
# Checker step + SoD (D2.2) and atomic apply (D2.3)
# ---------------------------------------------------------------------------


def test_approve_by_distinct_principal_applies(db_path: Path) -> None:
    _protected_prod(db_path)
    move = propose_move(
        "triage", "production", "2",
        reason="ship v2", identity="alice", db_path=db_path,
    )
    record, detail = approve_move(
        "triage", "production", move["move_id"],
        identity="bob", note="reviewed diff", db_path=db_path,
    )
    assert detail == "applied"
    assert record["state"] == "applied"
    assert record["applied_at"]
    assert len(record["approvals"]) == 1
    approval = record["approvals"][0]
    assert approval["approver"] == "bob"
    assert approval["decision"] == "approve"
    assert approval["note"] == "reviewed diff"
    Draft202012Validator(PENDING_SCHEMA).validate(record)

    # The approval signature is a real Ed25519 signature by bob's key.
    key, fp = ensure_keypair("bob")
    assert approval["approver_key_fp"] == fp
    payload = approval_payload(
        record["move_id"], "approve", "bob", approval["approved_at"]
    )
    assert verify_sig(key.public_key(), approval["approver_signature"], payload)

    # The label moved, and the 0113 audit row reuses the pending move's ULID.
    assert get_label("triage", "production", db_path=db_path)["target_version"] == "2"
    history = label_history("triage", "production", db_path=db_path)
    assert len(history) == 2
    assert history[0]["move_id"] == move["move_id"]
    assert history[0]["previous_version"] == "1"
    assert history[0]["target_version"] == "2"
    assert history[0]["moved_by"] == "alice"
    assert history[0]["reason"] == "ship v2"


def test_self_approval_refused_same_identity(db_path: Path) -> None:
    _protected_prod(db_path)
    move = propose_move("triage", "production", "2", identity="alice", db_path=db_path)
    with pytest.raises(SelfApprovalError, match="SoD"):
        approve_move(
            "triage", "production", move["move_id"], identity="alice", db_path=db_path
        )
    # Nothing moved; the move is still pending with zero approvals.
    assert get_label("triage", "production", db_path=db_path)["target_version"] == "1"
    refreshed = get_move("triage", move["move_id"], db_path=db_path)
    assert refreshed["state"] == "pending"
    assert refreshed["approvals"] == []


def test_self_approval_refused_same_key_distinct_identity(db_path: Path) -> None:
    """SoD is enforced at the crypto level: a renamed identity reusing the
    maker's keypair is still refused (key fingerprints match)."""
    _protected_prod(db_path)
    move = propose_move("triage", "production", "2", identity="alice", db_path=db_path)
    ensure_keypair("alice")
    mallory_path = kr._key_path("mallory")
    mallory_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(kr._key_path("alice"), mallory_path)
    with pytest.raises(SelfApprovalError, match="key fingerprint"):
        approve_move(
            "triage", "production", move["move_id"], identity="mallory", db_path=db_path
        )
    assert get_label("triage", "production", db_path=db_path)["target_version"] == "1"


def test_two_required_approvals_duplicate_counts_once(db_path: Path) -> None:
    _protected_prod(db_path, required_approvals=2)
    move = propose_move("triage", "production", "2", identity="alice", db_path=db_path)

    record, detail = approve_move(
        "triage", "production", move["move_id"], identity="bob", db_path=db_path
    )
    assert record["state"] == "pending"
    assert detail == "pending (1/2 distinct approvals)"

    # Duplicate approver: recorded, but not double-counted.
    record, detail = approve_move(
        "triage", "production", move["move_id"], identity="bob", db_path=db_path
    )
    assert record["state"] == "pending"
    assert detail == "pending (1/2 distinct approvals)"
    assert len(record["approvals"]) == 2

    record, detail = approve_move(
        "triage", "production", move["move_id"], identity="carol", db_path=db_path
    )
    assert detail == "applied"
    assert get_label("triage", "production", db_path=db_path)["target_version"] == "2"


def test_reject_is_terminal(db_path: Path) -> None:
    _protected_prod(db_path)
    move = propose_move("triage", "production", "2", identity="alice", db_path=db_path)
    record, detail = approve_move(
        "triage", "production", move["move_id"],
        identity="bob", reject=True, note="regression", db_path=db_path,
    )
    assert detail == "rejected"
    assert record["state"] == "rejected"
    assert record["approvals"][0]["decision"] == "reject"
    assert get_label("triage", "production", db_path=db_path)["target_version"] == "1"

    with pytest.raises(MoveStateError, match="terminal"):
        approve_move(
            "triage", "production", move["move_id"], identity="carol", db_path=db_path
        )
    # A terminal move frees the (asset, label) slot for a fresh proposal.
    fresh = propose_move("triage", "production", "2", identity="alice", db_path=db_path)
    assert fresh["move_id"] != move["move_id"]


def test_expired_move_cannot_apply(db_path: Path) -> None:
    _protected_prod(db_path)
    move = propose_move(
        "triage", "production", "2",
        identity="alice", expires_at="2000-01-01T00:00:00+00:00", db_path=db_path,
    )
    with pytest.raises(MoveStateError, match="expired"):
        approve_move(
            "triage", "production", move["move_id"], identity="bob", db_path=db_path
        )
    assert get_move("triage", move["move_id"], db_path=db_path)["state"] == "expired"
    assert get_label("triage", "production", db_path=db_path)["target_version"] == "1"


def test_approve_unknown_move_or_wrong_label(db_path: Path) -> None:
    _protected_prod(db_path)
    move = propose_move("triage", "production", "2", identity="alice", db_path=db_path)
    with pytest.raises(PendingMoveNotFoundError):
        approve_move(
            "triage", "production", "01J2Q8ZK7M4YZ2K7N9DPBYK2WX",
            identity="bob", db_path=db_path,
        )
    with pytest.raises(PendingMoveNotFoundError):
        approve_move(
            "triage", "staging", move["move_id"], identity="bob", db_path=db_path
        )


# ---------------------------------------------------------------------------
# Policy gate (D3)
# ---------------------------------------------------------------------------


def test_missing_policy_ref_fails_closed(db_path: Path, tmp_path: Path) -> None:
    _protected_prod(db_path, policy_ref=str(tmp_path / "nope.rego"))
    move = propose_move("triage", "production", "2", identity="alice", db_path=db_path)
    record, detail = approve_move(
        "triage", "production", move["move_id"], identity="bob", db_path=db_path
    )
    assert record["state"] == "pending"
    assert "policy denied" in detail
    assert "fail-closed" in detail
    # The approval itself is still recorded evidence.
    assert len(record["approvals"]) == 1
    assert get_label("triage", "production", db_path=db_path)["target_version"] == "1"


class _FakeEngine:
    def __init__(self, allow: bool, reason: str) -> None:
        self._allow, self._reason = allow, reason
        self.inputs: list[Any] = []

    def evaluate(self, input_: Any) -> Any:
        from novafabric.policy import PolicyDecision

        self.inputs.append(input_)
        return PolicyDecision(allow=self._allow, reason=self._reason)


def test_custom_policy_deny_keeps_move_pending(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rego = tmp_path / "freeze.rego"
    rego.write_text("package novafabric.authz\n\ndefault allow := false\n")
    engine = _FakeEngine(allow=False, reason="freeze window")
    monkeypatch.setattr("novafabric.policy.get_policy_engine", lambda: engine)

    _protected_prod(db_path, policy_ref=str(rego))
    move = propose_move("triage", "production", "2", identity="alice", db_path=db_path)
    record, detail = approve_move(
        "triage", "production", move["move_id"], identity="bob", db_path=db_path
    )
    assert record["state"] == "pending"
    assert "freeze window" in detail
    assert engine.inputs and engine.inputs[0].action == "label.protected_move"
    # The snapshot travelled with the move.
    assert record["policy_ref"] == str(rego)


def test_custom_policy_allow_applies(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rego = tmp_path / "allow.rego"
    rego.write_text("package novafabric.authz\n\ndefault allow := true\n")
    engine = _FakeEngine(allow=True, reason="ok")
    monkeypatch.setattr("novafabric.policy.get_policy_engine", lambda: engine)

    _protected_prod(db_path, policy_ref=str(rego))
    move = propose_move("triage", "production", "2", identity="alice", db_path=db_path)
    record, detail = approve_move(
        "triage", "production", move["move_id"], identity="bob", db_path=db_path
    )
    assert detail == "applied"
    assert get_label("triage", "production", db_path=db_path)["target_version"] == "2"


def test_unprotect_midflight_uses_snapshot(db_path: Path) -> None:
    """Un-protecting leaves an in-flight move resolving under its snapshot."""
    _protected_prod(db_path, required_approvals=2)
    move = propose_move("triage", "production", "2", identity="alice", db_path=db_path)
    protect_label("triage", "production", protected=False, db_path=db_path)
    record, detail = approve_move(
        "triage", "production", move["move_id"], identity="bob", db_path=db_path
    )
    assert record["state"] == "pending"  # snapshot still requires 2 approvals
    assert record["required_approvals"] == 2


# ---------------------------------------------------------------------------
# Audit surfaces
# ---------------------------------------------------------------------------


def test_new_tables_are_append_only(db_path: Path) -> None:
    _protected_prod(db_path)
    move = propose_move("triage", "production", "2", identity="alice", db_path=db_path)
    approve_move(
        "triage", "production", move["move_id"], identity="bob", db_path=db_path
    )
    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE asset_label_protection SET protected = 0")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM asset_label_protection")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE asset_label_move_approvals SET decision = 'reject'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM asset_label_move_approvals")
        with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
            conn.execute("DELETE FROM asset_label_pending_moves")
    finally:
        conn.close()


def test_applied_history_row_is_schema_valid(db_path: Path) -> None:
    move_schema = json.loads(
        (REPO_ROOT / "schemas" / "asset-label-move.schema.json").read_text()
    )
    _protected_prod(db_path)
    move = propose_move("triage", "production", "2", identity="alice", db_path=db_path)
    approve_move(
        "triage", "production", move["move_id"], identity="bob", db_path=db_path
    )
    for row in label_history("triage", "production", db_path=db_path):
        Draft202012Validator(move_schema).validate(
            {k: v for k, v in row.items() if k != "auto"}
        )


def test_status_and_accessors(db_path: Path) -> None:
    _protected_prod(db_path, required_approvals=2)
    protect_label("triage", "canary", db_path=db_path)
    move = propose_move("triage", "production", "2", identity="alice", db_path=db_path)

    status = label_status("triage", db_path=db_path)
    assert status["asset_name"] == "triage"
    assert {p["label"] for p in status["protections"]} == {"production", "canary"}
    assert {p["label"] for p in status["pointers"]} == {"latest", "production"}
    assert [m["move_id"] for m in status["pending_moves"]] == [move["move_id"]]

    scoped = label_status("triage", "production", db_path=db_path)
    assert {p["label"] for p in scoped["protections"]} == {"production"}
    assert len(scoped["pending_moves"]) == 1

    assert get_move("triage", move["move_id"], db_path=db_path)["state"] == "pending"
    with pytest.raises(PendingMoveNotFoundError):
        get_move("triage", "01J2Q8ZK7M4YZ2K7N9DPBYK2WX", db_path=db_path)
    assert list_moves("triage", "production", state="pending", db_path=db_path)
    assert list_moves("triage", "production", state="applied", db_path=db_path) == []
