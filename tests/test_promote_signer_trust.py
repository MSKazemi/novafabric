"""ADR-0312 — maker-checker signatures are verified against a bound signer trust list,
and ``nova promote direct`` into a protected stage is policy-gated.

These tests pin the observable behaviour of the CLI (Typer ``CliRunner``) and the
registry service. Where possible they drive the CLI, so a regression reads as the
real symptom (an approval that went through, an asset that reached ``production``)
rather than as a changed function signature.

Exit codes (consistent with the NovaSeal ``verify_sod`` convention):
3 = proposal-side signature/trust failure, 4 = approval-side signature/trust
failure, 6 = self-approval by the same bound identity.
"""

from __future__ import annotations

import base64
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from typer.testing import CliRunner

import novafabric.trust.keyring as kr
from novafabric.cli.main import app
from novafabric.policy._models import PolicyDecision, PolicyInput
from novafabric.registry.service import (
    get_asset,
    promote_asset,
    register_asset,
)
from novafabric.spec.models import AssetStatus
from novafabric.spec.validator import validate_spec

runner = CliRunner()

ASSET = "fraud-model@1.0.0"
NAME, VERSION = "fraud-model", "1.0.0"


# ---------------------------------------------------------------------------
# Fixtures / helpers (deliberately built only on APIs that predate ADR-0312,
# so the red run at HEAD fails on behaviour, not on a missing helper)
# ---------------------------------------------------------------------------


def _pub_b64(identity: str) -> tuple[str, str]:
    key, fp = kr.ensure_keypair(identity)  # create=True is the default for direct callers
    raw = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return base64.b64encode(raw).decode(), fp


def _write_trust(path: Path, entries: list[dict[str, Any]]) -> None:
    path.write_text(yaml.safe_dump({"signers": entries}), encoding="utf-8")


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.db = tmp_path / "reg.db"
        self.trust = tmp_path / "promote-signers.yaml"
        self.keyring = tmp_path / "keyring"

    def status(self) -> str:
        return str(get_asset(NAME, VERSION, db_path=self.db)["status"])

    def key_file(self, identity: str) -> Path:
        return kr._key_path(identity)

    def bind(self, entries: list[tuple[str, str, list[str]]]) -> None:
        """Write a trust list: (bound identity, keyring identity holding the key, roles)."""
        rows = []
        for bound, key_holder, roles in entries:
            pub, fp = _pub_b64(key_holder)
            rows.append({"identity": bound, "public_key": pub, "roles": roles})
        _write_trust(self.trust, rows)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixtures_dir: Path) -> Env:
    e = Env(tmp_path)
    monkeypatch.setattr(kr, "_KEYRING_DIR", e.keyring)
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(e.db))
    monkeypatch.setenv("NOVAFABRIC_PROMOTE_SIGNERS", str(e.trust))
    spec = validate_spec(fixtures_dir / "valid_model.yaml")
    register_asset(spec, fixtures_dir / "valid_model.yaml", db_path=e.db)
    promote_asset(NAME, VERSION, AssetStatus.validated, actor="ci", db_path=e.db)
    # alice is a maker, bob a checker — two people, two keys, explicitly bound.
    e.bind([("alice", "alice", ["proposer"]), ("bob", "bob", ["approver"])])
    return e


def _propose(identity: str = "alice", to: str = "staging") -> Any:
    return runner.invoke(app, ["promote", "propose", ASSET, "--to", to, "--identity", identity])


def _approve(identity: str) -> Any:
    return runner.invoke(app, ["promote", "approve", ASSET, "--identity", identity])


# ---------------------------------------------------------------------------
# Happy path, end to end through the CLI
# ---------------------------------------------------------------------------


def test_happy_path_keygen_propose_approve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixtures_dir: Path
) -> None:
    e = Env(tmp_path)
    monkeypatch.setattr(kr, "_KEYRING_DIR", e.keyring)
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(e.db))
    monkeypatch.setenv("NOVAFABRIC_PROMOTE_SIGNERS", str(e.trust))
    spec = validate_spec(fixtures_dir / "valid_model.yaml")
    register_asset(spec, fixtures_dir / "valid_model.yaml", db_path=e.db)
    promote_asset(NAME, VERSION, AssetStatus.validated, actor="ci", db_path=e.db)

    # Keys are created only by the explicit key-management command.
    for who in ("alice", "bob"):
        res = runner.invoke(app, ["promote", "keygen", "--identity", who])
        assert res.exit_code == 0, res.output
        assert e.key_file(who).is_file()
        assert "public_key:" in res.output
    e.bind([("alice", "alice", ["proposer"]), ("bob", "bob", ["approver"])])

    res = _propose("alice")
    assert res.exit_code == 0, res.output
    assert e.status() == "validated"

    res = _approve("bob")
    assert res.exit_code == 0, res.output
    assert e.status() == "staging"

    from novafabric.compliance.export.finance.model_independence_collect import (
        collect_registry_records,
    )

    (record,) = collect_registry_records(e.db, ASSET)
    assert record.state == "approved"
    assert record.maker == "alice" and record.checker == "bob"
    assert record.signature_verification == "verified"


def test_keygen_is_idempotent_and_never_overwrites(env: Env) -> None:
    before = env.key_file("alice").read_bytes()
    res = runner.invoke(app, ["promote", "keygen", "--identity", "alice"])
    assert res.exit_code == 0, res.output
    assert "already exists" in res.output
    assert env.key_file("alice").read_bytes() == before


# ---------------------------------------------------------------------------
# (a) a signature that does not verify is refused
# ---------------------------------------------------------------------------


def test_a_tampered_proposal_is_refused_at_approval(env: Env) -> None:
    """The proposal row is edited after signing (staging → production)."""
    assert _propose("alice").exit_code == 0
    conn = sqlite3.connect(env.db)
    conn.execute("UPDATE promotion_proposals SET to_status = 'production'")
    conn.commit()
    conn.close()

    res = _approve("bob")
    assert res.exit_code == 3, res.output
    assert "signature" in res.output.lower()
    assert env.status() == "validated"


def test_a_invalid_approver_signature_is_refused(env: Env) -> None:
    from novafabric.registry.service import approve_promotion
    from novafabric.registry.signers import InvalidSignatureError

    assert _propose("alice").exit_code == 0
    _, bob_fp = kr.ensure_keypair("bob")
    forged = base64.urlsafe_b64encode(b"\x00" * 64).rstrip(b"=").decode()
    with pytest.raises(InvalidSignatureError):
        approve_promotion(
            NAME, VERSION, approver="bob", approver_key_fp=bob_fp,
            approver_sig=forged, db_path=env.db,
        )
    assert env.status() == "validated"


def test_a_invalid_proposer_signature_is_refused(env: Env) -> None:
    import uuid

    from novafabric.registry.service import propose_promotion
    from novafabric.registry.signers import InvalidSignatureError

    _, alice_fp = kr.ensure_keypair("alice")
    with pytest.raises(InvalidSignatureError):
        propose_promotion(
            NAME, VERSION, AssetStatus.staging, "alice", alice_fp, "not-a-signature",
            db_path=env.db, proposal_id=str(uuid.uuid4()),
        )


def test_a_approval_signature_is_bound_to_the_proposal(env: Env) -> None:
    """A valid approval signature over a *different* proposal does not transfer."""
    import uuid

    from novafabric.registry.service import (
        approve_promotion,
        asset_spec_sha256,
        get_open_proposal,
    )
    from novafabric.registry.signers import InvalidSignatureError, approval_payload

    assert _propose("alice").exit_code == 0
    open_ = get_open_proposal(NAME, VERSION, db_path=env.db)
    assert open_ is not None
    bob_key, bob_fp = kr.ensure_keypair("bob")
    other = approval_payload(
        proposal_id=str(uuid.uuid4()), name=NAME, version=VERSION,
        to_status=open_["to_status"], proposer_sig=open_["proposer_sig"],
        spec_sha256=asset_spec_sha256(NAME, VERSION, db_path=env.db),
    )
    with pytest.raises(InvalidSignatureError):
        approve_promotion(
            NAME, VERSION, approver="bob", approver_key_fp=bob_fp,
            approver_sig=kr.sign_payload(bob_key, other), db_path=env.db,
        )


# ---------------------------------------------------------------------------
# (b) a fingerprint that is not in the trust list is refused
# ---------------------------------------------------------------------------


def test_b_unlisted_approver_key_is_refused(env: Env) -> None:
    kr.ensure_keypair("carol")  # a real key, but nobody bound it
    assert _propose("alice").exit_code == 0
    res = _approve("carol")
    assert res.exit_code == 4, res.output
    assert "trust list" in res.output
    assert env.status() == "validated"


def test_b_unlisted_proposer_key_is_refused(env: Env) -> None:
    kr.ensure_keypair("carol")
    res = _propose("carol")
    assert res.exit_code == 3, res.output
    assert "trust list" in res.output


def test_b_missing_trust_list_is_refused_with_an_actionable_message(env: Env) -> None:
    env.trust.unlink()
    res = _propose("alice")
    assert res.exit_code == 3, res.output
    assert "promote-signers.yaml" in res.output or str(env.trust) in res.output


def test_b_role_is_enforced(env: Env) -> None:
    """bob is bound as an approver only — he cannot act as a maker."""
    res = _propose("bob")
    assert res.exit_code == 3, res.output
    assert "proposer" in res.output


# ---------------------------------------------------------------------------
# (c) one human under two identity names
# ---------------------------------------------------------------------------


def test_c_second_name_with_a_freshly_minted_key_is_refused(env: Env) -> None:
    assert _propose("alice").exit_code == 0
    kr.ensure_keypair("alice-alt")  # alice mints herself a second, unbound key
    res = _approve("alice-alt")
    assert res.exit_code == 4, res.output
    assert env.status() == "validated"


def test_c_second_name_reusing_the_same_key_is_refused(env: Env) -> None:
    assert _propose("alice").exit_code == 0
    env.key_file("alice-alt").write_bytes(env.key_file("alice").read_bytes())
    res = _approve("alice-alt")
    assert res.exit_code == 4, res.output
    assert "alice" in res.output
    assert env.status() == "validated"


def test_c_one_key_bound_to_two_identities_is_a_config_error(env: Env) -> None:
    assert _propose("alice").exit_code == 0  # under the valid trust list
    env.key_file("alice-alt").write_bytes(env.key_file("alice").read_bytes())
    # An operator mistake (or an attempt) binds alice's one key to a second name.
    env.bind([
        ("alice", "alice", ["proposer"]),
        ("alice-alt", "alice", ["approver"]),
        ("bob", "bob", ["approver"]),
    ])
    res = _approve("alice-alt")
    assert res.exit_code == 4, res.output
    assert "more than once" in res.output
    assert env.status() == "validated"
    # The malformed list refuses the maker side too.
    res = runner.invoke(app, ["promote", "propose", "fraud-model@1.0.0", "--to", "staging",
                              "--identity", "alice"])
    assert res.exit_code == 3, res.output


def test_c_self_approval_by_the_same_bound_identity_is_refused(env: Env) -> None:
    env.bind([("alice", "alice", ["proposer", "approver"]), ("bob", "bob", ["approver"])])
    assert _propose("alice").exit_code == 0
    res = _approve("alice")
    assert res.exit_code == 6, res.output
    assert env.status() == "validated"


# ---------------------------------------------------------------------------
# (d) --identity is required on propose and approve
# ---------------------------------------------------------------------------


def test_d_approve_without_identity_errors(env: Env) -> None:
    assert _propose("alice").exit_code == 0
    res = runner.invoke(app, ["promote", "approve", ASSET])
    assert res.exit_code == 2, res.output
    assert "--identity" in res.output
    assert env.status() == "validated"


def test_d_propose_without_identity_errors(env: Env) -> None:
    res = runner.invoke(app, ["promote", "propose", ASSET, "--to", "staging"])
    assert res.exit_code == 2, res.output
    assert "--identity" in res.output


# ---------------------------------------------------------------------------
# (e) the approve/propose path never creates a key
# ---------------------------------------------------------------------------


def test_e_approve_does_not_create_a_key_file(env: Env) -> None:
    assert _propose("alice").exit_code == 0
    assert not env.key_file("dave").exists()
    res = _approve("dave")
    assert res.exit_code == 1, res.output
    assert "nova promote keygen" in res.output
    assert not env.key_file("dave").exists()
    assert env.status() == "validated"


def test_e_propose_does_not_create_a_key_file(env: Env) -> None:
    res = _propose("dave")
    assert res.exit_code == 1, res.output
    assert not env.key_file("dave").exists()


def test_e_ensure_keypair_create_false_never_writes(env: Env) -> None:
    from novafabric.trust.keyring import KeyNotFoundError

    with pytest.raises(KeyNotFoundError):
        kr.ensure_keypair("dave", create=False)
    assert not env.key_file("dave").exists()


# ---------------------------------------------------------------------------
# (f) promote direct into a protected stage needs an explicit policy allow
# ---------------------------------------------------------------------------


def test_f_direct_to_production_refused_under_default_policy(env: Env) -> None:
    res = runner.invoke(app, ["promote", "direct", ASSET, "--to", "production"])
    assert res.exit_code == 1, res.output
    assert "promote propose" in res.output
    assert env.status() == "validated"


def test_f_force_does_not_bypass_the_protected_stage_gate(env: Env) -> None:
    res = runner.invoke(
        app, ["promote", "direct", ASSET, "--to", "production", "--force"], input=f"{NAME}\n"
    )
    assert res.exit_code == 1, res.output
    assert env.status() == "validated"


def test_f_direct_to_staging_still_works(env: Env) -> None:
    res = runner.invoke(app, ["promote", "direct", ASSET, "--to", "staging"])
    assert res.exit_code == 0, res.output
    assert env.status() == "staging"


class _AllowDirect:
    def __init__(self) -> None:
        self.inputs: list[PolicyInput] = []

    def evaluate(self, input_: PolicyInput) -> PolicyDecision:
        self.inputs.append(input_)
        return PolicyDecision(allow=True, reason="test allow", decision_id="t-1")


def test_f_an_allow_policy_permits_production(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _AllowDirect()
    monkeypatch.setattr("novafabric.registry.service.get_policy_engine", lambda: engine)
    res = runner.invoke(app, ["promote", "direct", ASSET, "--to", "production"])
    assert res.exit_code == 0, res.output
    assert env.status() == "production"
    direct = [i for i in engine.inputs if i.action == "promote_direct"]
    assert len(direct) == 1
    assert direct[0].resource.to_status == "production"


def test_f_a_deny_policy_refuses_production(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Deny:
        def evaluate(self, input_: PolicyInput) -> PolicyDecision:
            return PolicyDecision(allow=input_.action != "promote_direct", reason="nope")

    monkeypatch.setattr("novafabric.registry.service.get_policy_engine", lambda: _Deny())
    res = runner.invoke(app, ["promote", "direct", ASSET, "--to", "production"])
    assert res.exit_code == 1, res.output
    assert env.status() == "validated"


def test_f_maker_checker_still_reaches_production(env: Env) -> None:
    """The gate is on *direct* promotion only; the two-person path is the way in."""
    assert _propose("alice", to="production").exit_code == 0
    res = _approve("bob")
    assert res.exit_code == 0, res.output
    assert env.status() == "production"


@pytest.mark.real_opa
@pytest.mark.skipif(shutil.which("opa") is None, reason="opa binary not installed")
def test_f_default_rego_denies_production_and_allows_staging() -> None:
    from novafabric.policy import OpaEngine, PolicyResource, PolicySubject

    def _inp(stage: str) -> PolicyInput:
        return PolicyInput(
            action="promote_direct",
            subject=PolicySubject(user="cli-user"),
            resource=PolicyResource(kind="asset", ref=ASSET, to_status=stage),
            context={"protected_stage": stage == "production"},
        )

    assert OpaEngine().evaluate(_inp("production")).allow is False
    assert OpaEngine().evaluate(_inp("staging")).allow is True


# ---------------------------------------------------------------------------
# (g) rows stored before ADR-0312 still read, reported as unverified
# ---------------------------------------------------------------------------

_LEGACY_DDL = """
CREATE TABLE IF NOT EXISTS promotion_proposals (
    proposal_id      TEXT PRIMARY KEY,
    asset_name       TEXT NOT NULL,
    asset_version    TEXT NOT NULL,
    to_status        TEXT NOT NULL,
    proposer         TEXT NOT NULL,
    proposer_key_fp  TEXT NOT NULL,
    proposer_sig     TEXT NOT NULL,
    proposed_at      TEXT NOT NULL,
    state            TEXT NOT NULL DEFAULT 'open',
    approver         TEXT,
    approver_key_fp  TEXT,
    approver_sig     TEXT,
    approved_at      TEXT
);
"""


def _legacy_db(path: Path, *, state: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_LEGACY_DDL)
    conn.execute(
        "INSERT INTO promotion_proposals (proposal_id, asset_name, asset_version, to_status,"
        " proposer, proposer_key_fp, proposer_sig, proposed_at, state, approver,"
        " approver_key_fp, approver_sig, approved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "00000000-0000-4000-8000-000000000001", NAME, VERSION, "staging",
            "alice", "aaaaaaaaaaaaaaaa", "legacy-sig", "2026-01-01T00:00:00+00:00", state,
            "bob" if state == "approved" else None,
            "bbbbbbbbbbbbbbbb" if state == "approved" else None,
            "legacy-sig-b" if state == "approved" else None,
            "2026-01-02T00:00:00+00:00" if state == "approved" else None,
        ),
    )
    conn.commit()
    conn.close()


def test_g_legacy_row_lists_as_unverified(tmp_path: Path) -> None:
    from novafabric.compliance.export.finance.model_independence_collect import (
        collect_registry_records,
    )

    db = tmp_path / "legacy.db"
    _legacy_db(db, state="approved")  # read-only collector, pre-ADR-0312 schema
    (record,) = collect_registry_records(db, ASSET)
    assert record.state == "approved"
    assert record.maker == "alice" and record.checker == "bob"
    assert record.signature_verification == "unverified"


def test_g_legacy_open_proposal_reads_and_is_not_approvable(env: Env) -> None:
    from novafabric.registry.service import get_open_proposal

    env.db.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(env.db) + suffix).unlink(missing_ok=True)
    _legacy_db(env.db, state="open")
    # Same path, new file: forget that init_schema already ran on it this process.
    from novafabric.registry.store import reset_schema_memo

    reset_schema_memo()
    # The assets table is recreated by init_schema; re-register the asset.
    spec = validate_spec(Path(__file__).parent / "fixtures" / "valid_model.yaml")
    register_asset(spec, Path(__file__).parent / "fixtures" / "valid_model.yaml", db_path=env.db)
    promote_asset(NAME, VERSION, AssetStatus.validated, actor="ci", db_path=env.db)

    open_ = get_open_proposal(NAME, VERSION, db_path=env.db)
    assert open_ is not None and open_["verification"] == "unverified"

    res = _approve("bob")
    assert res.exit_code == 3, res.output
    assert "unverified" in res.output
    assert env.status() == "validated"

    # A verified proposal supersedes the unverified one instead of deadlocking.
    res = _propose("alice")
    assert res.exit_code == 0, res.output
    assert _approve("bob").exit_code == 0
    assert env.status() == "staging"
    conn = sqlite3.connect(env.db)
    states = dict(conn.execute("SELECT proposal_id, state FROM promotion_proposals").fetchall())
    conn.close()
    assert states["00000000-0000-4000-8000-000000000001"] == "superseded"


def test_keyring_dir_is_resolved_when_called_never_at_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A HOME/env change after import must move the keyring (it was fixed at import)."""
    from novafabric.trust import keyring as kr

    monkeypatch.setattr(kr, "_KEYRING_DIR", None)
    monkeypatch.setenv(kr.KEYRING_DIR_ENV, str(tmp_path / "configured"))
    assert kr.keyring_dir() == tmp_path / "configured"
    monkeypatch.delenv(kr.KEYRING_DIR_ENV)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert kr.keyring_dir() == tmp_path / "home" / ".config" / "novafabric" / "keyring"


def test_hermetic_tests_never_reach_the_real_keyring() -> None:
    from novafabric.trust import keyring as kr

    real = Path(os.path.expanduser("~")).resolve() / ".config" / "novafabric" / "keyring"
    assert kr.keyring_dir().resolve() != real
