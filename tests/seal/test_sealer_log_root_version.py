"""The sealer-log check cannot be bypassed by claiming the other root version.

Found by an adversarial review (2026-10-22). ``_sealer_log_keyid`` treated a local log
as the sealer's only when its root version equalled the record's ``root_version`` — a
field of ``log-entry.json``, which whoever wrote the capsule controls. A key holder who
signed outside the sealing path (into a side log) only had to claim the *other* version:
host log v2 + forged v1 record, and host log v1 + forged v2 record, both verified as
``carried-proof``, ``valid=True``.

``root_version`` is not in the signed payload (the log entry is appended after
signing), and even if it were, the attacker holds the signing key. So the binding has
to come from the verifier's side:

* the sealer's log is recognised by signer alone, whatever its version;
* a record claiming a version *newer* than every sealer's log (v2 against v1) fails —
  v2 logs came after v1 logs (ADR-0313), so no v1 log produced a v2 seal;
* a v1 record against a v2 sealer's log (the legitimate ADR-0313 migration) is checked
  against this host's earlier logs (``previous_merkle_dbs``) when configured — present
  there passes as ``local-log``, absent fails;
* without them, the result is no longer a silent pass: ``sealer_log`` is
  ``unverifiable-other-version`` and ``nova verify --strict`` fails. The default exit
  code stays 0, because a genuine pre-migration capsule is indistinguishable from the
  forgery without the old log (residual, documented in ADR-0297 Amendment 2).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.trust.novaseal import (
    LOG_INCLUSION_FAILED,
    LOG_INCLUSION_LOCAL,
    LOG_INCLUSION_PROOF,
    SEALER_LOG_ENTRY_FOUND,
    SEALER_LOG_MISSING,
    SEALER_LOG_NO_LOG,
    SEALER_LOG_NOT_SEALERS,
    SEALER_LOG_OTHER_VERSION,
    NovaSeal,
    unanchored_claims,
    verify_seal_dir,
    verify_with_profile,
)
from novafabric.trust.novaseal.config import SealConfigError, load_signing_profile
from novafabric.trust.novaseal.merkle import MerkleLog

from .test_sealer_log_inclusion import _Identity, _new_log, _write_seal

runner = CliRunner()


@pytest.fixture()
def me(tmp_path: Path) -> _Identity:
    return _Identity(tmp_path, "sealer")


def _sealer(me: _Identity, db: Path, root_version: int) -> NovaSeal:
    """A sealer over a log of *root_version* that already holds one of its seals."""
    seal = me.sealer(_new_log(db, root_version))
    seal.seal({"run_id": f"really-sealed-v{root_version}"})
    return seal


def _forged(tmp_path: Path, me: _Identity, root_version: int) -> Path:
    """Signed with *me*'s key into a side log of *root_version* the verifier never sees."""
    side = me.sealer(_new_log(tmp_path / f"side-v{root_version}.db", root_version))
    side.seal({"run_id": "padding"})
    return _write_seal(side.seal({"run_id": "forged"}), tmp_path / f"forged-v{root_version}")


def _verify(seal_dir: Path, current: Path | None, *previous: Path):  # type: ignore[no-untyped-def]
    logs = [MerkleLog(p) for p in previous]
    log = MerkleLog(current) if current is not None else None
    try:
        return verify_seal_dir(seal_dir, merkle_log=log, previous_logs=logs)
    finally:
        for opened in (log, *logs):
            if opened is not None:
                opened.close()


# ---------------------------------------------------------------------------
# The bypass, both directions
# ---------------------------------------------------------------------------


def test_v2_record_against_the_v1_sealer_log_fails(tmp_path: Path, me: _Identity) -> None:
    host = _sealer(me, tmp_path / "host.db", 1)
    result = host.verify("", str(_forged(tmp_path, me, 2)))
    assert result.log_inclusion == LOG_INCLUSION_FAILED, result.log_notes
    assert result.sealer_log == SEALER_LOG_MISSING
    assert result.valid is False
    assert any("claims root v2" in e for e in result.errors), result.errors


def test_v1_record_against_the_v2_sealer_log_is_unverified_not_passed(
    tmp_path: Path, me: _Identity
) -> None:
    host = _sealer(me, tmp_path / "host.db", 2)
    result = host.verify("", str(_forged(tmp_path, me, 1)))
    # No silent pass: the exit-code result stands, but it is labelled unverified.
    assert result.log_inclusion == LOG_INCLUSION_PROOF
    assert result.sealer_log == SEALER_LOG_OTHER_VERSION
    assert "sealer-log-other-version" in unanchored_claims(result)
    assert any(n.startswith("UNVERIFIED") for n in result.log_notes), result.log_notes


def test_v1_forgery_fails_when_the_hosts_v1_log_is_supplied(
    tmp_path: Path, me: _Identity
) -> None:
    _sealer(me, tmp_path / "old-v1.db", 1)._merkle.close()
    _sealer(me, tmp_path / "new-v2.db", 2)._merkle.close()
    result = _verify(_forged(tmp_path, me, 1), tmp_path / "new-v2.db", tmp_path / "old-v1.db")
    assert result.log_inclusion == LOG_INCLUSION_FAILED, result.log_notes
    assert result.sealer_log == SEALER_LOG_MISSING
    assert result.valid is False


# ---------------------------------------------------------------------------
# Legitimate cases
# ---------------------------------------------------------------------------


def test_migrated_v1_capsule_is_confirmed_by_the_previous_log(
    tmp_path: Path, me: _Identity
) -> None:
    """ADR-0313 migration with the retired v1 log listed: a real ``local-log`` check."""
    old = _sealer(me, tmp_path / "old-v1.db", 1)
    seal_dir = _write_seal(old.seal({"run_id": "v1-era"}), tmp_path / "v1-era")
    old._merkle.close()
    _sealer(me, tmp_path / "new-v2.db", 2)._merkle.close()
    result = _verify(seal_dir, tmp_path / "new-v2.db", tmp_path / "old-v1.db")
    assert result.log_inclusion == LOG_INCLUSION_LOCAL, result.errors
    assert result.sealer_log == SEALER_LOG_ENTRY_FOUND
    assert result.valid


def test_migrated_v1_capsule_without_the_previous_log_still_verifies(
    tmp_path: Path, me: _Identity
) -> None:
    """The residual: without the old log the genuine capsule looks like the forgery."""
    old = _sealer(me, tmp_path / "old-v1.db", 1)
    seal_dir = _write_seal(old.seal({"run_id": "v1-era"}), tmp_path / "v1-era")
    old._merkle.close()
    _sealer(me, tmp_path / "new-v2.db", 2)._merkle.close()
    result = _verify(seal_dir, tmp_path / "new-v2.db")
    assert result.valid, result.errors
    assert result.log_inclusion == LOG_INCLUSION_PROOF
    assert result.sealer_log == SEALER_LOG_OTHER_VERSION


def test_new_v2_capsule_on_a_migrated_host_is_checked_in_the_current_log(
    tmp_path: Path, me: _Identity
) -> None:
    _sealer(me, tmp_path / "old-v1.db", 1)._merkle.close()
    new = _sealer(me, tmp_path / "new-v2.db", 2)
    seal_dir = _write_seal(new.seal({"run_id": "v2-era"}), tmp_path / "v2-era")
    new._merkle.close()
    result = _verify(seal_dir, tmp_path / "new-v2.db", tmp_path / "old-v1.db")
    assert result.log_inclusion == LOG_INCLUSION_LOCAL, result.errors
    assert result.valid


@pytest.mark.parametrize(("host_v", "capsule_v"), [(1, 2), (2, 1), (1, 1), (2, 2)])
def test_another_signers_log_of_either_version_keeps_carried_proof(
    tmp_path: Path, me: _Identity, host_v: int, capsule_v: int
) -> None:
    auditor = _sealer(_Identity(tmp_path, "auditor"), tmp_path / "auditor.db", host_v)
    result = auditor.verify("", str(_forged(tmp_path, me, capsule_v)))
    assert result.log_inclusion == LOG_INCLUSION_PROOF, result.errors
    assert result.sealer_log == SEALER_LOG_NOT_SEALERS
    assert result.valid


@pytest.mark.parametrize(("fresh_v", "capsule_v"), [(1, 2), (2, 1)])
def test_a_fresh_log_of_either_version_keeps_carried_proof(
    tmp_path: Path, me: _Identity, fresh_v: int, capsule_v: int
) -> None:
    fresh = _new_log(tmp_path / "fresh.db", fresh_v)
    result = _verify(_forged(tmp_path, me, capsule_v), fresh)
    assert result.log_inclusion == LOG_INCLUSION_PROOF
    assert result.sealer_log == SEALER_LOG_NOT_SEALERS
    assert result.valid


def test_no_log_reports_no_log(tmp_path: Path, me: _Identity) -> None:
    result = verify_seal_dir(_forged(tmp_path, me, 2))
    assert result.sealer_log == SEALER_LOG_NO_LOG
    assert result.valid


# ---------------------------------------------------------------------------
# Configuration and CLI
# ---------------------------------------------------------------------------


def _config(tmp_path: Path, me: _Identity, current: Path, previous: list[Path]) -> Path:
    path = me.config(tmp_path, current)
    if previous:
        lines = "".join(f"  - {p}\n" for p in previous)
        path.write_text(path.read_text() + f"previous_merkle_dbs:\n{lines}")
    return path


def test_nova_verify_strict_fails_the_v1_forgery_against_a_v2_host(
    tmp_path: Path, me: _Identity
) -> None:
    _sealer(me, tmp_path / "host.db", 2)._merkle.close()
    capsule = _forged(tmp_path, me, 1).parent
    config = _config(tmp_path, me, tmp_path / "host.db", [])

    plain = runner.invoke(app, ["verify", str(capsule), "--seal-config", str(config), "--json"])
    assert plain.exit_code == 0, plain.output
    report = json.loads(plain.stdout)
    assert report["sealer_log"] == SEALER_LOG_OTHER_VERSION
    assert "sealer-log-other-version" in report["unanchored_claims"]

    strict = runner.invoke(
        app, ["verify", str(capsule), "--seal-config", str(config), "--json", "--strict"]
    )
    assert strict.exit_code == 1, strict.output

    human = runner.invoke(app, ["verify", str(capsule), "--seal-config", str(config)])
    assert "Sealer's log: UNVERIFIED" in human.output


def test_nova_verify_with_previous_merkle_dbs_decides_both_ways(
    tmp_path: Path, me: _Identity
) -> None:
    old = _sealer(me, tmp_path / "old-v1.db", 1)
    genuine = _write_seal(old.seal({"run_id": "v1-era"}), tmp_path / "v1-era").parent
    old._merkle.close()
    _sealer(me, tmp_path / "new-v2.db", 2)._merkle.close()
    forged = _forged(tmp_path, me, 1).parent
    config = _config(tmp_path, me, tmp_path / "new-v2.db", [tmp_path / "old-v1.db"])

    ok = runner.invoke(app, ["verify", str(genuine), "--seal-config", str(config), "--json"])
    assert ok.exit_code == 0, ok.output
    assert json.loads(ok.stdout)["log_inclusion"] == LOG_INCLUSION_LOCAL

    bad = runner.invoke(app, ["verify", str(forged), "--seal-config", str(config), "--json"])
    assert bad.exit_code == 1, bad.output
    assert json.loads(bad.stdout)["sealer_log"] == SEALER_LOG_MISSING


def test_verify_with_profile_reads_previous_merkle_dbs(
    tmp_path: Path, me: _Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _sealer(me, tmp_path / "old-v1.db", 1)
    genuine = _write_seal(old.seal({"run_id": "v1-era"}), tmp_path / "v1-era")
    old._merkle.close()
    _sealer(me, tmp_path / "new-v2.db", 2)._merkle.close()
    config = _config(tmp_path, me, tmp_path / "new-v2.db", [tmp_path / "old-v1.db"])
    monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(config))
    profile = load_signing_profile()
    assert profile is not None
    assert profile.previous_merkle_dbs == [str(tmp_path / "old-v1.db")]
    result = verify_with_profile(genuine, profile)
    assert result.log_inclusion == LOG_INCLUSION_LOCAL, result.errors


def test_previous_merkle_dbs_entry_that_does_not_exist_is_a_config_error(
    tmp_path: Path, me: _Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, me, tmp_path / "host.db", [tmp_path / "missing.db"])
    monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(config))
    with pytest.raises(SealConfigError, match="previous_merkle_dbs"):
        load_signing_profile()
    assert not (tmp_path / "missing.db").exists()


def test_previous_merkle_dbs_must_be_a_list_of_strings(
    tmp_path: Path, me: _Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = me.config(tmp_path, tmp_path / "host.db")
    config.write_text(config.read_text() + "previous_merkle_dbs: /one/path.db\n")
    monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(config))
    with pytest.raises(SealConfigError, match="previous_merkle_dbs"):
        load_signing_profile()


def test_open_existing_merkle_log_never_creates_a_log(tmp_path: Path) -> None:
    from novafabric.trust.novaseal.merkle import open_existing_merkle_log

    assert open_existing_merkle_log(None) is None
    assert open_existing_merkle_log("") is None
    missing = tmp_path / "missing.db"
    assert open_existing_merkle_log(missing) is None
    assert not missing.exists()
    _new_log(tmp_path / "real.db", 2)
    opened = open_existing_merkle_log(str(tmp_path / "real.db"))
    assert opened is not None
    assert opened.root_version == 2
    opened.close()
