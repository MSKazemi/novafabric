# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""In-toto binding for consent receipts + withdrawals (ADR-0150 P3 remainder).

Acceptance criteria: the attestation is a DSSE-wrapped in-toto Statement v1 made
by the shared writer; it commits to every receipt digest and withdrawal; every
tamper (signature, payload, receipt edit, withdrawal removed/moved, consent
dropped, wrong key) is ``invalid``; a legitimate later change is ``stale``, not
``invalid``; the capsule is never modified.
"""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.evidence.intoto import DSSE_PAYLOAD_TYPE, INTOTO_STATEMENT_TYPE
from novafabric.evidence.signing import LocalSigner, generate_keypair
from novafabric.hitl.consent import withdraw_recorded_consent
from novafabric.hitl.consent_attestation import (
    CONSENT_ATTESTATION_PREDICATE_TYPE,
    ConsentAttestationError,
    attest_consents,
    build_consent_statement,
    consent_set_digest,
    verify_consent_attestation,
    withdrawal_digest,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "conversation" / "p3-accountability-capsule.json"
CID = "consent-0001"
runner = CliRunner()


@pytest.fixture
def keys(tmp_path: Path) -> tuple[Path, Path]:
    return generate_keypair(tmp_path / "keys")


@pytest.fixture
def other_keys(tmp_path: Path) -> tuple[Path, Path]:
    return generate_keypair(tmp_path / "other-keys")


def _doc() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text())
    return data


def _capsule_dir(tmp_path: Path, data: dict[str, Any]) -> Path:
    cap = tmp_path / "cap"
    cap.mkdir(exist_ok=True)
    (cap / "capsule.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    return cap


def _consents(data: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = data["facets"]["conversation"]["consent"]
    return entries


def _statement(env: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(base64.b64decode(env["payload"]))
    return out


def _resign(statement: dict[str, Any], key: Path) -> dict[str, Any]:
    from novafabric.evidence.intoto import dsse_sign

    env: dict[str, Any] = dsse_sign(statement, LocalSigner(key))
    return env


def _attest(data: dict[str, Any], key: Path, cap: Path | None = None) -> dict[str, Any]:
    return attest_consents(data, LocalSigner(key), capsule_dir=cap, attested_at="2026-10-02T00:00:00Z")


def _verify(env: dict[str, Any], data: dict[str, Any], pub: Path, cap: Path | None = None) -> Any:
    return verify_consent_attestation(env, data, public_pem=pub.read_bytes(), capsule_dir=cap)


def _failed(result: Any) -> set[str]:
    return {c.name for c in result.checks if not c.ok}


# ── building ────────────────────────────────────────────────────────────────


def test_statement_shape_and_subjects() -> None:
    data = _doc()
    st = build_consent_statement(data, attested_at="2026-10-02T00:00:00Z")
    assert st["_type"] == INTOTO_STATEMENT_TYPE
    assert st["predicateType"] == CONSENT_ATTESTATION_PREDICATE_TYPE
    receipts = _consents(data)
    assert [s["name"] for s in st["subject"]] == [f"consent/{r['consent_id']}" for r in receipts]
    for s, r in zip(st["subject"], receipts, strict=True):
        assert "sha256:" + s["digest"]["sha256"] == r["receipt_digest"]
    pred = st["predicate"]
    assert pred["run_id"] == data["run_id"]
    assert pred["consent_set_digest"] == consent_set_digest(pred["consents"])
    assert "capsule_merkle_root" not in pred  # no capsule_dir supplied


def test_envelope_is_dsse_in_toto(keys: tuple[Path, Path]) -> None:
    env = _attest(_doc(), keys[0])
    assert env["payloadType"] == DSSE_PAYLOAD_TYPE
    assert env["signatures"][0]["keyid"] == LocalSigner(keys[0]).keyid


def test_withdrawal_is_bound() -> None:
    data = _doc()
    out = withdraw_recorded_consent(data, CID, withdrawn_at="2026-09-30T12:00:00Z").capsule
    st = build_consent_statement(out)
    entry = next(e for e in st["predicate"]["consents"] if e["consent_id"] == CID)
    assert entry["withdrawn_at"] == "2026-09-30T12:00:00Z"
    assert entry["withdrawal_digest"] == withdrawal_digest(
        CID, entry["receipt_digest"], "2026-09-30T12:00:00Z"
    )
    # Subject digest is unchanged by the withdrawal (receipt digest excludes it).
    before = build_consent_statement(data)["subject"]
    assert st["subject"] == before


def test_build_refuses_no_receipts() -> None:
    data = _doc()
    del data["facets"]["conversation"]["consent"]
    with pytest.raises(ConsentAttestationError, match="nothing to attest"):
        build_consent_statement(data)


def test_build_refuses_digest_failing_receipt() -> None:
    data = _doc()
    _consents(data)[0]["purpose"] = "dpv:Marketing"
    with pytest.raises(ConsentAttestationError, match="digest check"):
        build_consent_statement(data)


def test_build_refuses_malformed_receipt() -> None:
    data = _doc()
    _consents(data)[0]["subject_ref"] = "alice@example.com"
    with pytest.raises(ConsentAttestationError, match="malformed"):
        build_consent_statement(data)


def test_build_refuses_missing_run_id() -> None:
    data = _doc()
    del data["run_id"]
    with pytest.raises(ConsentAttestationError, match="run_id"):
        build_consent_statement(data)


# ── verification: ok ────────────────────────────────────────────────────────


def test_roundtrip_ok(tmp_path: Path, keys: tuple[Path, Path]) -> None:
    data = _doc()
    cap = _capsule_dir(tmp_path, data)
    env = _attest(data, keys[0], cap)
    result = _verify(env, data, keys[1], cap)
    assert result.status == "ok", result.to_dict()
    assert result.capsule_root_matches is True
    assert result.run_id == data["run_id"]


def test_attesting_does_not_modify_capsule(tmp_path: Path, keys: tuple[Path, Path]) -> None:
    data = _doc()
    cap = _capsule_dir(tmp_path, data)
    before = (cap / "capsule.yaml").read_bytes()
    _attest(data, keys[0], cap)
    assert (cap / "capsule.yaml").read_bytes() == before
    assert sorted(p.name for p in cap.iterdir()) == ["capsule.yaml"]


# ── verification: tamper → invalid ──────────────────────────────────────────


def test_wrong_key_is_invalid(keys: tuple[Path, Path], other_keys: tuple[Path, Path]) -> None:
    env = _attest(_doc(), keys[0])
    result = _verify(env, _doc(), other_keys[1])
    assert result.status == "invalid"
    assert "signature" in _failed(result)


def test_payload_tamper_is_invalid(keys: tuple[Path, Path]) -> None:
    data = _doc()
    env = _attest(data, keys[0])
    st = _statement(env)
    st["predicate"]["consents"][0]["receipt_digest"] = "sha256:" + "0" * 64
    env["payload"] = base64.b64encode(json.dumps(st, sort_keys=True).encode()).decode()
    result = _verify(env, data, keys[1])
    assert result.status == "invalid"
    assert "signature" in _failed(result)


def test_signature_bitflip_is_invalid(keys: tuple[Path, Path]) -> None:
    env = _attest(_doc(), keys[0])
    sig = bytearray(base64.b64decode(env["signatures"][0]["sig"]))
    sig[0] ^= 1
    env["signatures"][0]["sig"] = base64.b64encode(bytes(sig)).decode()
    assert _verify(env, _doc(), keys[1]).status == "invalid"


@pytest.mark.parametrize("mutate", [
    lambda e: e.pop("signatures"),
    lambda e: e.update(signatures=[]),
    lambda e: e.update(payload="!!not-base64!!"),
    lambda e: e.update(payloadType="text/plain"),
])
def test_malformed_envelope_is_invalid(keys: tuple[Path, Path], mutate: Any) -> None:
    env = _attest(_doc(), keys[0])
    mutate(env)
    assert _verify(env, _doc(), keys[1]).status == "invalid"


def test_wrong_predicate_type_is_invalid(keys: tuple[Path, Path]) -> None:
    st = build_consent_statement(_doc())
    st["predicateType"] = "https://example.com/other/v1"
    result = _verify(_resign(st, keys[0]), _doc(), keys[1])
    assert result.status == "invalid"
    assert "statement" in _failed(result)


def test_receipt_edited_after_attestation_is_invalid(keys: tuple[Path, Path]) -> None:
    data = _doc()
    env = _attest(data, keys[0])
    tampered = copy.deepcopy(data)
    _consents(tampered)[0]["purpose"] = "dpv:Marketing"  # digest now fails too
    result = _verify(env, tampered, keys[1])
    assert result.status == "invalid"
    assert "receipt" in _failed(result)


def test_receipt_edit_with_recomputed_digest_is_invalid(keys: tuple[Path, Path]) -> None:
    from novafabric.hitl.consent import compute_receipt_digest

    data = _doc()
    env = _attest(data, keys[0])
    tampered = copy.deepcopy(data)
    rec = _consents(tampered)[0]
    rec["purpose"] = "dpv:Marketing"
    rec["receipt_digest"] = compute_receipt_digest(rec)  # self-consistent forgery
    result = _verify(env, tampered, keys[1])
    assert result.status == "invalid"
    assert "receipt" in _failed(result)


def test_consent_dropped_is_invalid(keys: tuple[Path, Path]) -> None:
    data = _doc()
    env = _attest(data, keys[0])
    dropped = copy.deepcopy(data)
    dropped["facets"]["conversation"]["consent"] = _consents(dropped)[1:]
    if not dropped["facets"]["conversation"]["consent"]:
        del dropped["facets"]["conversation"]["consent"]
    result = _verify(env, dropped, keys[1])
    assert result.status == "invalid"
    assert "receipt" in _failed(result)


def test_withdrawal_removed_is_invalid(keys: tuple[Path, Path]) -> None:
    data = _doc()
    withdrawn = withdraw_recorded_consent(data, CID, withdrawn_at="2026-09-30T12:00:00Z").capsule
    env = _attest(withdrawn, keys[0])
    result = _verify(env, data, keys[1])  # capsule without the withdrawal: un-withdrawn
    assert result.status == "invalid"
    assert "withdrawal" in _failed(result)


def test_withdrawal_moved_is_invalid(keys: tuple[Path, Path]) -> None:
    data = _doc()
    withdrawn = withdraw_recorded_consent(data, CID, withdrawn_at="2026-09-30T12:00:00Z").capsule
    env = _attest(withdrawn, keys[0])
    moved = copy.deepcopy(withdrawn)
    next(r for r in _consents(moved) if r["consent_id"] == CID)["withdrawn_at"] = (
        "2026-10-01T00:00:00Z"
    )
    result = _verify(env, moved, keys[1])
    assert result.status == "invalid"
    assert "withdrawal" in _failed(result)


def test_forged_withdrawal_digest_in_signed_predicate_is_invalid(keys: tuple[Path, Path]) -> None:
    st = build_consent_statement(_doc())
    st["predicate"]["consents"][0]["withdrawn_at"] = "2026-09-30T12:00:00Z"
    st["predicate"]["consent_set_digest"] = consent_set_digest(st["predicate"]["consents"])
    result = _verify(_resign(st, keys[0]), _doc(), keys[1])  # signed, but digest missing
    assert result.status == "invalid"
    assert "withdrawal_digest" in _failed(result)


def test_subjects_not_matching_predicate_is_invalid(keys: tuple[Path, Path]) -> None:
    st = build_consent_statement(_doc())
    st["subject"][0]["digest"]["sha256"] = "0" * 64
    result = _verify(_resign(st, keys[0]), _doc(), keys[1])
    assert result.status == "invalid"
    assert "subjects" in _failed(result)


def test_consent_set_digest_mismatch_is_invalid(keys: tuple[Path, Path]) -> None:
    st = build_consent_statement(_doc())
    st["predicate"]["consent_set_digest"] = "sha256:" + "1" * 64
    result = _verify(_resign(st, keys[0]), _doc(), keys[1])
    assert "consent_set_digest" in _failed(result)


def test_other_run_id_is_invalid(keys: tuple[Path, Path]) -> None:
    data = _doc()
    env = _attest(data, keys[0])
    other = copy.deepcopy(data)
    other["run_id"] = "01OTHERRUN"
    result = _verify(env, other, keys[1])
    assert result.status == "invalid"
    assert "run_id" in _failed(result)


# ── verification: stale (legitimate change after attestation) ───────────────


def test_withdrawal_after_attestation_is_stale_not_invalid(
    tmp_path: Path, keys: tuple[Path, Path]
) -> None:
    data = _doc()
    cap = _capsule_dir(tmp_path, data)
    env = _attest(data, keys[0], cap)
    withdrawn = withdraw_recorded_consent(data, CID, withdrawn_at="2026-09-30T12:00:00Z").capsule
    (cap / "capsule.yaml").write_text(yaml.safe_dump(withdrawn, sort_keys=False))
    result = _verify(env, withdrawn, keys[1], cap)
    assert result.status == "stale"
    assert any(CID in r and "withdrawn after attestation" in r for r in result.stale_reasons)
    assert result.capsule_root_matches is False
    # Re-attesting binds the withdrawal and is ok again.
    env2 = _attest(withdrawn, keys[0], cap)
    assert _verify(env2, withdrawn, keys[1], cap).status == "ok"


def test_consent_recorded_after_attestation_is_stale(keys: tuple[Path, Path]) -> None:
    from novafabric.hitl.consent import build_consent_receipt, record_consent

    data = _doc()
    env = _attest(data, keys[0])
    receipt = build_consent_receipt(
        consent_id="consent-late",
        subject_ref="human:fp:9f2c4a1b7e0d5638",
        purpose="dpv:ServiceProvision",
        action=["dpv:Store"],
        given_at="2026-10-01T00:00:00Z",
    )
    out = record_consent(copy.deepcopy(data), receipt)
    assert out.recorded
    result = _verify(env, out.capsule, keys[1])
    assert result.status == "stale"
    assert any("consent-late" in r for r in result.stale_reasons)


def test_tamper_outranks_stale(keys: tuple[Path, Path]) -> None:
    data = _doc()
    env = _attest(data, keys[0])
    changed = copy.deepcopy(data)
    _consents(changed)[0]["purpose"] = "dpv:Marketing"
    assert _verify(env, changed, keys[1]).status == "invalid"


# ── CLI ─────────────────────────────────────────────────────────────────────


def _cli_attest(cap: Path, key: Path, out: Path) -> Any:
    return runner.invoke(
        app, ["consent", "attest", "--capsule", str(cap), "--key", str(key), "-o", str(out)]
    )


def _cli_verify(cap: Path, att: Path, pub: Path, *extra: str) -> Any:
    return runner.invoke(
        app,
        ["consent", "verify-attestation", "--capsule", str(cap), "--attestation", str(att),
         "--public-key", str(pub), *extra],
    )


def test_cli_attest_then_verify_ok(tmp_path: Path, keys: tuple[Path, Path]) -> None:
    cap = _capsule_dir(tmp_path, _doc())
    out = tmp_path / "c.intoto.json"
    res = _cli_attest(cap, keys[0], out)
    assert res.exit_code == 0, res.output
    assert json.loads(out.read_text())["payloadType"] == DSSE_PAYLOAD_TYPE
    ver = _cli_verify(cap, out, keys[1], "--json")
    assert ver.exit_code == 0, ver.output
    assert json.loads(ver.output)["status"] == "ok"


def test_cli_tamper_exit_1_and_stale_exit_3(tmp_path: Path, keys: tuple[Path, Path]) -> None:
    data = _doc()
    cap = _capsule_dir(tmp_path, data)
    out = tmp_path / "c.intoto.json"
    assert _cli_attest(cap, keys[0], out).exit_code == 0
    # stale: legitimate withdrawal through the real command
    w = runner.invoke(
        app,
        ["consent", "withdraw", "--capsule", str(cap), "--consent-id", CID,
         "--withdrawn-at", "2026-09-30T12:00:00Z"],
    )
    assert w.exit_code == 0, w.output
    assert _cli_verify(cap, out, keys[1]).exit_code == 3
    # tamper: edit a receipt on disk
    on_disk = yaml.safe_load((cap / "capsule.yaml").read_text())
    _consents(on_disk)[0]["purpose"] = "dpv:Marketing"
    (cap / "capsule.yaml").write_text(yaml.safe_dump(on_disk, sort_keys=False))
    assert _cli_verify(cap, out, keys[1]).exit_code == 1


def test_cli_wrong_key_exit_1(
    tmp_path: Path, keys: tuple[Path, Path], other_keys: tuple[Path, Path]
) -> None:
    cap = _capsule_dir(tmp_path, _doc())
    out = tmp_path / "c.intoto.json"
    assert _cli_attest(cap, keys[0], out).exit_code == 0
    assert _cli_verify(cap, out, other_keys[1]).exit_code == 1


def test_cli_attest_refuses_without_receipts(tmp_path: Path, keys: tuple[Path, Path]) -> None:
    data = _doc()
    del data["facets"]["conversation"]["consent"]
    cap = _capsule_dir(tmp_path, data)
    out = tmp_path / "c.intoto.json"
    res = _cli_attest(cap, keys[0], out)
    assert res.exit_code == 1
    assert not out.exists()


def test_cli_attest_bad_key_exit_1(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, _doc())
    bad = tmp_path / "bad.pem"
    bad.write_text("not a key")
    assert _cli_attest(cap, bad, tmp_path / "o.json").exit_code == 1


def test_cli_verify_unreadable_attestation_exit_1(
    tmp_path: Path, keys: tuple[Path, Path]
) -> None:
    cap = _capsule_dir(tmp_path, _doc())
    junk = tmp_path / "junk.json"
    junk.write_text("{not json")
    assert _cli_verify(cap, junk, keys[1]).exit_code == 1
    arr = tmp_path / "arr.json"
    arr.write_text("[]")
    assert _cli_verify(cap, arr, keys[1]).exit_code == 1
