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

"""Tests for the in-toto capsule Statement (NF-030, ADR-0096)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from novafabric.envelopes.intoto import (
    CAPSULE_MAPPING_VERSION,
    CAPSULE_PREDICATE_TYPE,
    SubjectDigestMismatch,
    capsule_statement,
    capsule_subjects,
)
from novafabric.evidence.intoto import INTOTO_STATEMENT_TYPE, dsse_sign, dsse_verify

_RUN_ID = "01HXAY7M5JZ8R7K4P9DPBYK2WX"


def _capsule(tmp_path: Path) -> Path:
    cap = tmp_path / "capsule"
    cap.mkdir()
    (cap / "capsule.yaml").write_text(f"run_id: {_RUN_ID}\n")
    (cap / "trace.jsonl").write_text('{"event": "start"}\n')
    return cap


class _Signer:
    def __init__(self, key: Ed25519PrivateKey, keyid: str) -> None:
        self._key = key
        self.keyid = keyid

    def sign(self, data: bytes) -> bytes:
        return self._key.sign(data)


def test_subjects_are_per_file_sha256(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    subjects = capsule_subjects(cap)
    names = {s["name"] for s in subjects}
    assert names == {"capsule.yaml", "trace.jsonl"}
    trace = next(s for s in subjects if s["name"] == "trace.jsonl")
    assert trace["digest"]["sha256"] == hashlib.sha256((cap / "trace.jsonl").read_bytes()).hexdigest()


def test_statement_shape(tmp_path: Path) -> None:
    stmt = capsule_statement(_capsule(tmp_path), novafabric_version="0.9.0")
    assert stmt["_type"] == INTOTO_STATEMENT_TYPE
    assert stmt["predicateType"] == CAPSULE_PREDICATE_TYPE
    assert stmt["predicate"]["capsuleId"] == _RUN_ID
    assert stmt["predicate"]["runId"] == _RUN_ID
    assert stmt["predicate"]["mappingVersion"] == CAPSULE_MAPPING_VERSION
    assert stmt["predicate"]["novafabricVersion"] == "0.9.0"


def test_explicit_run_id_and_created_at_and_extra(tmp_path: Path) -> None:
    stmt = capsule_statement(
        _capsule(tmp_path),
        run_id="override",
        created_at="2026-07-02T00:00:00Z",
        extra_predicate={"promotionGate": "regression-gate/v1"},
    )
    assert stmt["predicate"]["capsuleId"] == "override"
    assert stmt["predicate"]["createdAt"] == "2026-07-02T00:00:00Z"
    assert stmt["predicate"]["promotionGate"] == "regression-gate/v1"


def test_expected_digests_match(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    good = {s["name"]: s["digest"]["sha256"] for s in capsule_subjects(cap)}
    stmt = capsule_statement(cap, expected_digests=good)  # no raise
    assert len(stmt["subject"]) == 2


def test_expected_digest_mismatch_raises(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    with pytest.raises(SubjectDigestMismatch, match="trace.jsonl"):
        capsule_statement(cap, expected_digests={"trace.jsonl": "sha256:" + "0" * 64})


def test_expected_digest_accepts_prefix(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    subjects = capsule_subjects(cap)
    prefixed = {s["name"]: "sha256:" + s["digest"]["sha256"] for s in subjects}
    stmt = capsule_statement(cap, expected_digests=prefixed)
    assert len(stmt["subject"]) == 2


def test_missing_run_id_yields_empty(tmp_path: Path) -> None:
    cap = tmp_path / "c"
    cap.mkdir()
    (cap / "trace.jsonl").write_text("{}\n")
    assert capsule_statement(cap)["predicate"]["capsuleId"] == ""


def test_statement_is_dsse_payload_roundtrip(tmp_path: Path) -> None:
    # req 6: the Statement is itself the DSSE payload (reuse the single DSSE writer).
    key = Ed25519PrivateKey.generate()
    stmt = capsule_statement(_capsule(tmp_path))
    env = dsse_sign(stmt, _Signer(key, "k"))
    pub = key.public_key()

    def _verify(pae: bytes, sig: bytes) -> bool:
        try:
            pub.verify(sig, pae)
            return True
        except Exception:
            return False

    assert dsse_verify(env, _verify) == stmt
