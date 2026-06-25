"""ADR-0094 (A) — multi-stream CheckpointRecord tests.

The checkpoint aggregates every per-stream sidecar-chain head plus the identity
binding, validates against ``schemas/ledger-checkpoint-v1.schema.json``, and is
DSSE-signed (reusing ``dsse_sign`` + the ledger-checkpoint predicate). It may
also be appended to the NovaSeal Merkle log.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from novafabric.evidence.intoto import dsse_verify
from novafabric.evidence.signing import LocalSigner, generate_keypair, verify_with_pem
from novafabric.trust.ledger._chain import write_chain
from novafabric.trust.ledger._checkpoint import (
    CHECKPOINT_PREDICATE_TYPE,
    CheckpointRecord,
    IdentityBinding,
    build_checkpoint,
    sign_checkpoint,
)

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "schemas"
    / "ledger-checkpoint-v1.schema.json"
)


@pytest.fixture(scope="module")
def schema_validator() -> Draft202012Validator:
    schema = json.loads(_SCHEMA_PATH.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.fixture()
def signer(tmp_path: Path) -> LocalSigner:
    priv, _ = generate_keypair(tmp_path / "keys")
    return LocalSigner(priv)


def _capsule(tmp_path: Path) -> Path:
    capsule = tmp_path / "01HXAY7M5JZ8R7K4P9DPBYK2WX"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text("run_id: 01HXAY7M5JZ8R7K4P9DPBYK2WX\n")
    (capsule / "model-calls.jsonl").write_text(
        json.dumps({"model": "m", "n": 0}) + "\n"
        + json.dumps({"model": "m", "n": 1}) + "\n"
    )
    (capsule / "tool-calls.jsonl").write_text(
        json.dumps({"tool": "db", "n": 0}) + "\n"
    )
    write_chain(capsule, "model-calls")
    write_chain(capsule, "tool-calls")
    return capsule


def test_build_checkpoint_aggregates_stream_heads(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    record = build_checkpoint(
        capsule,
        node_id="n1-compute-07",
        epoch=42,
        epoch_pubkey="MCowBQYDK2VwAyEA-fake",
        agent_id="support-agent",
        principal="alice@example.com",
    )
    assert isinstance(record, CheckpointRecord)
    assert record.capsule_id == "01HXAY7M5JZ8R7K4P9DPBYK2WX"
    assert record.kind == "finalize"
    streams = {s.stream: s for s in record.streams}
    assert set(streams) == {"model-calls", "tool-calls"}
    assert streams["model-calls"].seq_high == 1
    assert streams["model-calls"].record_count == 2
    assert streams["tool-calls"].seq_high == 0
    assert streams["tool-calls"].head_link_hash.startswith("sha256:")
    assert record.identity.node_id == "n1-compute-07"
    assert record.identity.epoch == 42
    assert record.identity.agent_id == "support-agent"
    assert record.identity.principal == "alice@example.com"


def test_checkpoint_record_validates_against_schema(
    tmp_path: Path, schema_validator: Draft202012Validator
) -> None:
    capsule = _capsule(tmp_path)
    record = build_checkpoint(
        capsule, node_id="n1", epoch=0, epoch_pubkey="pk"
    )
    schema_validator.validate(record.to_record())


def test_build_checkpoint_requires_at_least_one_chain(tmp_path: Path) -> None:
    capsule = tmp_path / "01HXAY7M5JZ8R7K4P9DPBYK2WY"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text("run_id: x\n")
    with pytest.raises(ValueError, match="no sidecar chains"):
        build_checkpoint(capsule, node_id="n1", epoch=0, epoch_pubkey="pk")


def test_sign_checkpoint_round_trip(tmp_path: Path, signer: LocalSigner) -> None:
    capsule = _capsule(tmp_path)
    record = build_checkpoint(
        capsule, node_id="n1", epoch=7, epoch_pubkey="pk"
    )
    envelope = sign_checkpoint(record, signer)

    def verify_fn(pae: bytes, sig: bytes) -> bool:
        return verify_with_pem(signer.public_pem, pae, sig)

    statement = dsse_verify(envelope, verify_fn)
    assert statement["predicateType"] == CHECKPOINT_PREDICATE_TYPE
    assert statement["predicate"]["capsule_id"] == capsule.name
    assert statement["predicate"]["identity"]["epoch"] == 7


def test_signed_body_excludes_signature_merkle_anchor(
    tmp_path: Path, signer: LocalSigner
) -> None:
    capsule = _capsule(tmp_path)
    record = build_checkpoint(capsule, node_id="n1", epoch=0, epoch_pubkey="pk")
    envelope = sign_checkpoint(record, signer)
    import base64

    payload = json.loads(base64.b64decode(envelope["payload"]).decode())
    predicate = payload["predicate"]
    assert "signature" not in predicate
    assert "merkle" not in predicate
    assert "anchor" not in predicate


def test_identity_binding_optional_fields_default_null(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    record = build_checkpoint(capsule, node_id="n1", epoch=0, epoch_pubkey="pk")
    assert record.identity.agent_id is None
    assert record.identity.principal is None
    assert isinstance(record.identity, IdentityBinding)


def test_checkpoint_appended_to_merkle_log(tmp_path: Path, signer: LocalSigner) -> None:
    from novafabric.trust.novaseal.merkle import MerkleLog

    capsule = _capsule(tmp_path)
    record = build_checkpoint(capsule, node_id="n1", epoch=0, epoch_pubkey="pk")
    log = MerkleLog(tmp_path / "merkle.db")
    updated = sign_checkpoint(record, signer, merkle_log=log)
    import base64

    payload = json.loads(base64.b64decode(updated["payload"]).decode())
    # the persisted checkpoint.json carries the merkle ref
    checkpoint_json = json.loads(
        (capsule / ".ledger" / "checkpoint.json").read_text()
    )
    assert checkpoint_json["merkle"]["root"].startswith("sha256:")
    assert log.tree_size() == 1
    assert payload["predicate"]["capsule_id"] == capsule.name
    log.close()


def test_sign_checkpoint_persists_checkpoint_json(
    tmp_path: Path, signer: LocalSigner
) -> None:
    capsule = _capsule(tmp_path)
    record = build_checkpoint(capsule, node_id="n1", epoch=0, epoch_pubkey="pk")
    sign_checkpoint(record, signer)
    written = json.loads((capsule / ".ledger" / "checkpoint.json").read_text())
    assert written["signature"]["algorithm"] == "ed25519"
    assert written["signature"]["value"]
