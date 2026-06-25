"""Court-admissible evidence binding (ADR-0095 feature B, slice C2).

Chain-of-custody + FRE-902(14) self-authentication on Evidence Bundles. The
load-bearing invariant (I3): any field NovaFabric cannot witness is emitted
`null` with `provenance="operator_declared"` — never auto-filled with a
plausible value.
"""

from __future__ import annotations

from pathlib import Path

from novafabric.audit._log import AuditLog
from novafabric.audit._models import AuditEventType
from novafabric.evidence.admissibility import (
    Custodian,
    admissibility_block,
    build_chain_of_custody,
    build_self_authentication,
    compute_admissibility_status,
)
from novafabric.evidence.merkle import capsule_merkle_root
from novafabric.evidence.signing import LocalSigner, generate_keypair

_RUN_ID = "01HXAY7M5JZ8R7K4P9DPBYK2WX"


def _capsule(tmp: Path) -> Path:
    (tmp / "capsule.yaml").write_text(f"run_id: {_RUN_ID}\n")
    (tmp / "model-calls.jsonl").write_text('{"model_call_id":"x","model":"gpt-4"}\n')
    return tmp


def _audit_log(tmp: Path) -> Path:
    path = tmp / "audit.jsonl"
    log = AuditLog(path)
    log.append(AuditEventType.EVIDENCE_EXPORT, "alice@corp", _RUN_ID, {"step": "captured"})
    log.append(AuditEventType.PROMOTE, "alice@corp", _RUN_ID, {"step": "sealed"})
    return path


def test_chain_of_custody_built_from_audit_log(tmp_path):
    cap = tmp_path / "cap"
    cap.mkdir()
    _capsule(cap)
    audit = _audit_log(tmp_path)

    coc = build_chain_of_custody(
        cap,
        audit_log_path=audit,
        run_id=_RUN_ID,
        custodian=Custodian(
            identity="alice@corp",
            role="operator",
            organization="Corp",
            provenance="novaseal-identity",
        ),
    )
    assert len(coc.custody_events) == 2
    assert coc.custody_events[0].entry_hash  # hash-chained from the audit log
    assert coc.integrity_continuous is True
    assert coc.acquisition.hash_at_acquisition == capsule_merkle_root(cap)
    assert coc.acquisition.acquired_at == coc.custody_events[0].at


def test_unwitnessed_custodian_is_operator_declared_not_fabricated(tmp_path):
    cap = tmp_path / "cap"
    cap.mkdir()
    _capsule(cap)
    audit = _audit_log(tmp_path)

    coc = build_chain_of_custody(cap, audit_log_path=audit, run_id=_RUN_ID)
    # I3: no custodian supplied → declared, never a made-up identity
    assert coc.custodian.provenance == "operator_declared"
    assert coc.custodian.identity is None


def test_broken_audit_chain_sets_integrity_false(tmp_path):
    cap = tmp_path / "cap"
    cap.mkdir()
    _capsule(cap)
    audit = _audit_log(tmp_path)
    # tamper: rewrite the first audit line's actor, breaking the hash chain
    lines = audit.read_text().splitlines()
    lines[0] = lines[0].replace("alice@corp", "mallory@evil")
    audit.write_text("\n".join(lines) + "\n")

    coc = build_chain_of_custody(cap, audit_log_path=audit, run_id=_RUN_ID)
    assert coc.integrity_continuous is False


def test_status_self_authenticating_when_all_five_hold(tmp_path):
    cap = tmp_path / "cap"
    cap.mkdir()
    _capsule(cap)
    audit = _audit_log(tmp_path)
    coc = build_chain_of_custody(
        cap,
        audit_log_path=audit,
        run_id=_RUN_ID,
        custodian=Custodian(identity="a", role="op", organization="C", provenance="oidc"),
    )
    sa = build_self_authentication(
        signature_alg="ed25519", signature_verifies=True, hash_verified=True
    )
    status = compute_admissibility_status(coc, sa, timestamp_ok=True)
    assert status == "self-authenticating"


def test_status_requires_foundation_without_real_custodian(tmp_path):
    cap = tmp_path / "cap"
    cap.mkdir()
    _capsule(cap)
    audit = _audit_log(tmp_path)
    coc = build_chain_of_custody(cap, audit_log_path=audit, run_id=_RUN_ID)  # declared
    sa = build_self_authentication(
        signature_alg="ed25519", signature_verifies=True, hash_verified=True
    )
    status = compute_admissibility_status(coc, sa, timestamp_ok=True)
    assert status == "requires-foundation"


def test_admissibility_block_with_signer_is_self_authenticating(tmp_path):
    cap = tmp_path / "cap"
    cap.mkdir()
    _capsule(cap)
    audit = _audit_log(tmp_path)
    priv, _ = generate_keypair(tmp_path / "key")
    signer = LocalSigner(priv)

    block = admissibility_block(
        cap,
        audit_log_path=audit,
        run_id=_RUN_ID,
        custodian=Custodian(identity="a", role="op", organization="C", provenance="oidc"),
        signer=signer,
        timestamp_ok=True,
    )
    assert block["self_authentication"]["certification"]["signature_verifies"] is True
    assert block["admissibility_status"] == "self-authenticating"
    assert "chain_of_custody" in block


def test_block_validates_against_evidence_bundle_schema(tmp_path):
    import json

    from jsonschema import Draft202012Validator

    cap = tmp_path / "cap"
    cap.mkdir()
    _capsule(cap)
    audit = _audit_log(tmp_path)
    block = admissibility_block(
        cap,
        audit_log_path=audit,
        run_id=_RUN_ID,
        custodian=Custodian(identity="a", role="op", organization="C", provenance="oidc"),
    )
    schema_path = (
        Path(__file__).resolve().parents[2] / "schemas" / "evidence-bundle.schema.json"
    )
    schema = json.loads(schema_path.read_text())
    validator = Draft202012Validator(schema)
    # the additive blocks must validate against their schema $defs
    Draft202012Validator(schema["$defs"]["ChainOfCustody"]).validate(
        block["chain_of_custody"]
    )
    Draft202012Validator(schema["$defs"]["SelfAuthentication"]).validate(
        block["self_authentication"]
    )
    assert block["admissibility_status"] in (
        validator.schema["properties"]["admissibility_status"]["enum"]
    )


def test_cli_bind_custody_and_check_admissibility(tmp_path, monkeypatch):
    import json

    from typer.testing import CliRunner

    from novafabric.cli.evidence import evidence_app

    cap = tmp_path / _RUN_ID
    cap.mkdir()
    _capsule(cap)
    # point the global audit log at our fixture log with 2 chained entries for the run
    audit = _audit_log(tmp_path)
    monkeypatch.setattr("novafabric.cli.evidence.AUDIT_LOG_PATH", audit)
    priv, _ = generate_keypair(tmp_path / "key")

    runner = CliRunner()
    out = tmp_path / "custody.json"
    res = runner.invoke(
        evidence_app,
        [
            "bind-custody", str(cap),
            "--custodian", "alice@corp", "--provenance", "oidc",
            "--timestamp-ok", "--key", str(priv), "-o", str(out),
        ],
    )
    assert res.exit_code == 0, res.output
    block = json.loads(out.read_text())
    assert block["admissibility_status"] == "self-authenticating"

    # check-admissibility re-derives the gate; self-authenticating → exit 0
    ok = runner.invoke(evidence_app, ["check-admissibility", str(out), "--timestamp-ok"])
    assert ok.exit_code == 0

    # without the timestamp evidence, the gate drops to requires-foundation → non-zero
    no_ts = runner.invoke(evidence_app, ["check-admissibility", str(out)])
    assert no_ts.exit_code == 3


def test_cli_bind_custody_unwitnessed_is_requires_foundation(tmp_path, monkeypatch):
    import json

    from typer.testing import CliRunner

    from novafabric.cli.evidence import evidence_app

    cap = tmp_path / _RUN_ID
    cap.mkdir()
    _capsule(cap)
    audit = _audit_log(tmp_path)
    monkeypatch.setattr("novafabric.cli.evidence.AUDIT_LOG_PATH", audit)

    runner = CliRunner()
    out = tmp_path / "custody.json"
    # no --custodian and no --key → operator_declared + unsigned → not self-authenticating
    res = runner.invoke(
        evidence_app, ["bind-custody", str(cap), "--timestamp-ok", "-o", str(out)]
    )
    assert res.exit_code == 0
    assert json.loads(out.read_text())["admissibility_status"] == "requires-foundation"


def test_status_incomplete_when_block_missing():
    assert compute_admissibility_status(None, None, timestamp_ok=True) == "incomplete"
