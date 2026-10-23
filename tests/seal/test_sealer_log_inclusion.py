"""Verifying a seal against the sealer's own Merkle log.

Two defects, both on the sealer's host:

* **Re-sealing an identical manifest failed.** A seal entry is
  ``{capsule_id, keyid, has_tsr}``, so sealing the same manifest twice with the same key
  appends a byte-identical leaf. The check looked the leaf up with
  ``find_leaf_index`` (the *lowest* index holding it) and compared that with the recorded
  index, so every seal after the first failed with "the local log holds this entry at
  leaf_index=0".
* **An entry missing from the sealer's log still passed.** With a carried inclusion
  proof (every seal since v0.103), an entry the local log did not contain was accepted
  silently. A carried proof binds the entry only to a tree head the capsule itself
  records; the sealer's log is the authority on what that sealer sealed. A log that
  holds other seals by the same signer is the sealer's log, and absence from it now
  fails. Another signer's log, a fresh log, or no log keeps the carried-proof result
  (ADR-0297). The signer's log of a *newer* root version (the ADR-0313 migration) keeps
  it too, but reports ``sealer_log: unverifiable-other-version`` — see
  ``test_sealer_log_root_version.py`` (ADR-0297 Amendment 2).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.trust.novaseal import (
    LOG_INCLUSION_FAILED,
    LOG_INCLUSION_LOCAL,
    LOG_INCLUSION_NOT_CHECKED,
    LOG_INCLUSION_PROOF,
    KeyConfig,
    NovaSeal,
    SealBundle,
    verify_seal_dir,
)
from novafabric.trust.novaseal.merkle import MerkleLog

from ._x509_pki import make_pki

runner = CliRunner()


class _Identity:
    """A signing key + certificate on disk, and NovaSeal instances over any log."""

    def __init__(self, tmp_path: Path, name: str) -> None:
        pki = make_pki(name)
        self.key_path = tmp_path / f"{name}.key"
        self.cert_path = tmp_path / f"{name}.crt"
        self.key_path.write_bytes(pki.leaf.key_pem)
        self.cert_path.write_bytes(pki.leaf.cert_pem)

    def sealer(self, db: Path) -> NovaSeal:
        return NovaSeal(
            config=KeyConfig(
                profile="local", key_path=str(self.key_path), cert_path=str(self.cert_path)
            ),
            tsa_url="",
            db_path=str(db),
        )

    def config(self, tmp_path: Path, db: Path) -> Path:
        path = tmp_path / f"novaseal-{db.stem}.yaml"
        path.write_text(
            f"profile: local\nkey_path: {self.key_path}\ncert_path: {self.cert_path}\n"
            f"tsa_url: \nmerkle_db: {db}\n"
        )
        return path


def _write_seal(bundle: SealBundle, capsule_dir: Path) -> Path:
    seal_dir = capsule_dir / ".seal"
    seal_dir.mkdir(parents=True)
    (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
    (seal_dir / "log-entry.json").write_text(json.dumps(bundle.log_entry), encoding="utf-8")
    return seal_dir


def _new_log(path: Path, root_version: int) -> Path:
    MerkleLog(path, root_version=root_version).close()
    return path


@pytest.fixture()
def me(tmp_path: Path) -> _Identity:
    return _Identity(tmp_path, "sealer")


MANIFEST = {"run_id": "run-dup", "status": "success"}


# ---------------------------------------------------------------------------
# Defect: re-sealing an identical manifest
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("root_version", [1, 2])
def test_resealing_an_identical_manifest_verifies_on_the_sealer_host(
    tmp_path: Path, me: _Identity, root_version: int
) -> None:
    db = _new_log(tmp_path / "merkle.db", root_version)
    seal = me.sealer(db)
    first, second = seal.seal(MANIFEST), seal.seal(MANIFEST)
    assert first.log_entry["leaf_hash"] == second.log_entry["leaf_hash"]
    assert (first.log_entry["leaf_index"], second.log_entry["leaf_index"]) == (0, 1)

    for name, bundle in (("first", first), ("second", second)):
        seal_dir = _write_seal(bundle, tmp_path / name)
        result = seal.verify("", str(seal_dir))
        assert result.log_inclusion == LOG_INCLUSION_LOCAL, (name, result.errors)
        assert result.valid, (name, result.errors)


def test_every_copy_of_a_thrice_sealed_manifest_verifies(tmp_path: Path, me: _Identity) -> None:
    seal = me.sealer(tmp_path / "merkle.db")
    seal.seal({"run_id": "before"})
    bundles = [seal.seal(MANIFEST) for _ in range(3)]
    seal.seal({"run_id": "after"})
    assert [b.log_entry["leaf_index"] for b in bundles] == [1, 2, 3]
    for i, bundle in enumerate(bundles):
        result = seal.verify("", str(_write_seal(bundle, tmp_path / f"copy{i}")))
        assert result.log_inclusion == LOG_INCLUSION_LOCAL, (i, result.errors)


def test_nova_verify_accepts_a_resealed_capsule(tmp_path: Path, me: _Identity) -> None:
    db = tmp_path / "merkle.db"
    seal = me.sealer(db)
    seal.seal(MANIFEST)
    capsule = tmp_path / "resealed"
    _write_seal(seal.seal(MANIFEST), capsule)
    result = runner.invoke(
        app, ["verify", str(capsule), "--seal-config", str(me.config(tmp_path, db)), "--json"]
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["log_inclusion"] == LOG_INCLUSION_LOCAL


def test_recorded_index_holding_a_different_leaf_still_fails(
    tmp_path: Path, me: _Identity
) -> None:
    """The fix asks about the recorded position; it must not accept any position."""
    seal = me.sealer(tmp_path / "merkle.db")
    target = seal.seal(MANIFEST)  # index 0
    seal.seal({"run_id": "other"})  # index 1
    record = dict(target.log_entry)
    record["leaf_index"] = 1
    for key in ("inclusion_proof", "root_hash", "tree_size"):
        record.pop(key)
    seal_dir = _write_seal(target, tmp_path / "moved")
    (seal_dir / "log-entry.json").write_text(json.dumps(record), encoding="utf-8")
    result = seal.verify("", str(seal_dir))
    assert result.log_inclusion == LOG_INCLUSION_FAILED
    assert any("leaf_index=0, not at 1" in e for e in result.errors), result.errors


# ---------------------------------------------------------------------------
# Defect: an entry missing from the sealer's own log
# ---------------------------------------------------------------------------


def _sealed_outside(tmp_path: Path, me: _Identity, name: str = "outside") -> Path:
    """Seal with *me*'s key into a throwaway log the verifier never sees."""
    side = me.sealer(tmp_path / f"{name}-side.db")
    side.seal({"run_id": f"{name}-padding"})
    return _write_seal(side.seal({"run_id": name}), tmp_path / name)


def test_entry_missing_from_the_sealer_log_fails(tmp_path: Path, me: _Identity) -> None:
    seal = me.sealer(tmp_path / "merkle.db")
    seal.seal({"run_id": "really-sealed"})
    seal_dir = _sealed_outside(tmp_path, me)
    # The carried proof is self-consistent: alone it verifies.
    assert verify_seal_dir(seal_dir).log_inclusion == LOG_INCLUSION_PROOF

    result = seal.verify("", str(seal_dir))
    assert result.log_inclusion == LOG_INCLUSION_FAILED
    assert result.log_integrity_ok is False
    assert result.valid is False
    assert any("not in the local Merkle log" in e for e in result.errors), result.errors


def test_entry_missing_after_the_log_was_rolled_back_fails(
    tmp_path: Path, me: _Identity
) -> None:
    """The sealer's log restored from a backup taken before the capsule was sealed."""
    db = tmp_path / "merkle.db"
    seal = me.sealer(db)
    seal.seal({"run_id": "earlier"})
    seal._merkle.close()
    backup = tmp_path / "backup.db"
    shutil.copyfile(db, backup)
    seal = me.sealer(db)
    seal_dir = _write_seal(seal.seal({"run_id": "later"}), tmp_path / "later")
    assert seal.verify("", str(seal_dir)).log_inclusion == LOG_INCLUSION_LOCAL

    rolled_back = MerkleLog(backup)
    try:
        result = verify_seal_dir(seal_dir, merkle_log=rolled_back)
    finally:
        rolled_back.close()
    assert result.log_inclusion == LOG_INCLUSION_FAILED
    assert result.valid is False


def test_nova_verify_fails_an_entry_missing_from_the_sealer_log(
    tmp_path: Path, me: _Identity
) -> None:
    db = tmp_path / "merkle.db"
    me.sealer(db).seal({"run_id": "really-sealed"})
    capsule = _sealed_outside(tmp_path, me).parent
    result = runner.invoke(
        app, ["verify", str(capsule), "--seal-config", str(me.config(tmp_path, db)), "--json"]
    )
    assert result.exit_code == 1, result.output
    report = json.loads(result.stdout)
    assert report["log_inclusion"] == LOG_INCLUSION_FAILED
    assert report["valid"] is False


def test_record_without_a_proof_missing_from_the_sealer_log_fails(
    tmp_path: Path, me: _Identity
) -> None:
    """A v0.102-style record (no proof) used to read as merely 'not-checked'."""
    seal = me.sealer(tmp_path / "merkle.db")
    seal.seal({"run_id": "really-sealed"})
    seal_dir = _sealed_outside(tmp_path, me)
    record = json.loads((seal_dir / "log-entry.json").read_text())
    for key in ("inclusion_proof", "root_hash", "tree_size", "root_version"):
        record.pop(key)
    (seal_dir / "log-entry.json").write_text(json.dumps(record), encoding="utf-8")
    assert verify_seal_dir(seal_dir).log_inclusion == LOG_INCLUSION_NOT_CHECKED

    # This record claims root v1 (no root_version), so the sealer's log must be v1 too.
    v1_db = _new_log(tmp_path / "v1.db", 1)
    v1_seal = me.sealer(v1_db)
    v1_seal.seal({"run_id": "really-sealed"})
    result = v1_seal.verify("", str(seal_dir))
    assert result.log_inclusion == LOG_INCLUSION_FAILED, result.log_notes


def test_rewritten_entry_keyid_does_not_evade_the_sealer_log(
    tmp_path: Path, me: _Identity
) -> None:
    """The signer's certificate fingerprint is checked too, not only the entry's keyid."""
    from novafabric.trust.novaseal.merkle import _leaf_hash

    seal = me.sealer(tmp_path / "merkle.db")
    seal.seal({"run_id": "really-sealed"})
    seal_dir = _sealed_outside(tmp_path, me)
    record = json.loads((seal_dir / "log-entry.json").read_text())
    record["entry"]["keyid"] = "0" * 64
    record["leaf_hash"] = _leaf_hash(
        json.dumps(record["entry"], sort_keys=True, separators=(",", ":")).encode()
    )
    for key in ("inclusion_proof", "root_hash", "tree_size"):
        record.pop(key)
    (seal_dir / "log-entry.json").write_text(json.dumps(record), encoding="utf-8")
    result = seal.verify("", str(seal_dir))
    assert result.log_inclusion == LOG_INCLUSION_FAILED, result.log_notes


# ---------------------------------------------------------------------------
# Not the sealer's log: carried-proof semantics are kept (ADR-0297)
# ---------------------------------------------------------------------------


def test_third_party_without_a_log_keeps_carried_proof(tmp_path: Path, me: _Identity) -> None:
    seal_dir = _sealed_outside(tmp_path, me)
    result = verify_seal_dir(seal_dir)
    assert result.log_inclusion == LOG_INCLUSION_PROOF
    assert result.valid


def test_another_signers_log_keeps_carried_proof(tmp_path: Path, me: _Identity) -> None:
    other = _Identity(tmp_path, "auditor")
    auditor = other.sealer(tmp_path / "auditor.db")
    auditor.seal({"run_id": "auditors-own"})
    seal_dir = _sealed_outside(tmp_path, me)
    result = auditor.verify("", str(seal_dir))
    assert result.log_inclusion == LOG_INCLUSION_PROOF, result.errors
    assert result.valid
    assert any("not in the local Merkle log" in n for n in result.log_notes)


def test_a_fresh_log_keeps_carried_proof(tmp_path: Path, me: _Identity) -> None:
    fresh = MerkleLog(tmp_path / "fresh.db")
    try:
        result = verify_seal_dir(_sealed_outside(tmp_path, me), merkle_log=fresh)
    finally:
        fresh.close()
    assert result.log_inclusion == LOG_INCLUSION_PROOF
    assert result.valid


def test_v1_capsule_against_the_hosts_new_v2_log_keeps_carried_proof(
    tmp_path: Path, me: _Identity
) -> None:
    """ADR-0313 migration: start a new (v2) log; v1-era capsules stay verifiable."""
    old = me.sealer(_new_log(tmp_path / "old-v1.db", 1))
    seal_dir = _write_seal(old.seal({"run_id": "v1-era"}), tmp_path / "v1-era")
    new = me.sealer(tmp_path / "new-v2.db")
    new.seal({"run_id": "v2-era"})
    result = new.verify("", str(seal_dir))
    assert result.log_inclusion == LOG_INCLUSION_PROOF, result.errors
    assert result.valid
    # ...but not silently: without the retired v1 log this cannot be told apart
    # from a forged v1 record (ADR-0297 Amendment 2).
    assert result.sealer_log == "unverifiable-other-version"


def test_has_entries_for_keyid_matches_only_a_whole_hex_keyid(tmp_path: Path) -> None:
    log = MerkleLog(tmp_path / "k.db")
    try:
        keyid = "ab" * 32
        log.append({"capsule_id": "c", "has_tsr": False, "keyid": keyid})
        log.append({"event": "key_rotation", "new_keyid": "cd" * 32, "old_keyid": keyid})
        assert log.has_entries_for_keyid(keyid)
        assert not log.has_entries_for_keyid("cd" * 32)  # only a rotation names it
        assert not log.has_entries_for_keyid("ab" * 31)
        assert not log.has_entries_for_keyid("%")
        assert not log.has_entries_for_keyid("AB" * 32)
    finally:
        log.close()
