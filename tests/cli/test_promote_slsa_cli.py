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

"""CLI tests for ``nova promote direct --slsa-provenance`` (NF-031, ADR-0096)."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.evidence.intoto import dsse_pae
from novafabric.trust.keyring import ensure_keypair

runner = CliRunner()


@pytest.fixture
def promote_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(tmp_path / "reg.db"))
    monkeypatch.setenv("HOME", str(tmp_path))  # isolate the keyring under a temp HOME
    return tmp_path


def _register_model(fixtures_dir: Path) -> None:
    result = runner.invoke(app, ["register", str(fixtures_dir / "valid_model.yaml")])
    assert result.exit_code == 0, result.output


def test_slsa_provenance_emitted_and_signed(
    promote_env: Path, fixtures_dir: Path
) -> None:
    _register_model(fixtures_dir)
    out = promote_env / "prov.slsa.json"
    result = runner.invoke(
        app,
        [
            "promote", "direct", "fraud-model@1.0.0", "--to", "staging",
            "--slsa-provenance", "--slsa-out", str(out), "--identity", "tester",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "SLSA provenance written" in result.output
    assert out.exists()

    envelope = json.loads(out.read_text())
    assert envelope["payloadType"] == "application/vnd.in-toto+json"
    statement = json.loads(base64.b64decode(envelope["payload"]))
    assert statement["predicateType"] == "https://slsa.dev/provenance/v1"
    assert statement["subject"][0]["name"] == "fraud-model@1.0.0"
    # subject digest is the sha256 of the registered spec_json
    assert len(statement["subject"][0]["digest"]["sha256"]) == 64
    assert statement["predicate"]["runDetails"]["byproducts"][0]["content"] == "promoted"

    # signature verifies against the keyring public key
    priv, _fp = ensure_keypair("tester")
    pae = dsse_pae(envelope["payloadType"], base64.b64decode(envelope["payload"]))
    priv.public_key().verify(base64.b64decode(envelope["signatures"][0]["sig"]), pae)


def test_subject_digest_matches_spec_json(promote_env: Path, fixtures_dir: Path) -> None:
    _register_model(fixtures_dir)
    out = promote_env / "prov.slsa.json"
    runner.invoke(
        app,
        [
            "promote", "direct", "fraud-model@1.0.0", "--to", "staging",
            "--slsa-provenance", "--slsa-out", str(out), "--identity", "tester",
        ],
    )
    from novafabric.registry.store import get_connection, init_schema

    conn = get_connection(promote_env / "reg.db")
    init_schema(conn)
    try:
        row = conn.execute(
            "SELECT spec_json FROM assets WHERE name = ? AND version = ?",
            ("fraud-model", "1.0.0"),
        ).fetchone()
    finally:
        conn.close()
    expected = hashlib.sha256(row["spec_json"].encode("utf-8")).hexdigest()
    statement = json.loads(base64.b64decode(json.loads(out.read_text())["payload"]))
    assert statement["subject"][0]["digest"]["sha256"] == expected


def test_default_no_flag_emits_no_provenance(
    promote_env: Path, fixtures_dir: Path
) -> None:
    _register_model(fixtures_dir)
    result = runner.invoke(
        app, ["promote", "direct", "fraud-model@1.0.0", "--to", "staging"]
    )
    assert result.exit_code == 0, result.output
    assert "SLSA provenance" not in result.output
    assert not (promote_env / "fraud-model-1.0.0.slsa.json").exists()


def test_provenance_verifiable_via_verify_envelope_cli(
    promote_env: Path, fixtures_dir: Path
) -> None:
    _register_model(fixtures_dir)
    out = promote_env / "prov.slsa.json"
    runner.invoke(
        app,
        [
            "promote", "direct", "fraud-model@1.0.0", "--to", "staging",
            "--slsa-provenance", "--slsa-out", str(out), "--identity", "tester",
        ],
    )
    priv, _fp = ensure_keypair("tester")
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    pub_path = promote_env / "pub.pem"
    pub_path.write_bytes(pub_pem)
    result = runner.invoke(
        app, ["verify-envelope", str(out), "--key", str(pub_path)]
    )
    assert result.exit_code == 0, result.output
    assert "verified" in result.output


def test_slsa_ml_profile_emits_promote_ml_and_eval_verdict(
    promote_env: Path, fixtures_dir: Path
) -> None:
    _register_model(fixtures_dir)
    out = promote_env / "ml.slsa.json"
    result = runner.invoke(
        app,
        [
            "promote", "direct", "fraud-model@1.0.0", "--to", "staging",
            "--slsa-provenance", "--slsa-ml-profile", "--slsa-out", str(out),
            "--identity", "tester",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "SLSA-for-ML SLSA provenance written" in result.output
    statement = json.loads(base64.b64decode(json.loads(out.read_text())["payload"]))
    bd = statement["predicate"]["buildDefinition"]
    assert bd["buildType"] == "https://novafabric.dev/promote-ml/v1"
    byproducts = {b["name"]: b for b in statement["predicate"]["runDetails"]["byproducts"]}
    assert "eval-verdict" in byproducts
    assert len(byproducts["eval-verdict"]["digest"]["sha256"]) == 64
