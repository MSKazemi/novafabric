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

"""CLI tests for ``nova verify-envelope`` (NF-029/030/031, ADR-0096)."""

from __future__ import annotations

import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.envelopes.dsse import wrap_bundle

runner = CliRunner()

_BUNDLE = b'{"bundle_id":"01HXAY7M5JZ8R7K4P9DPBYK2WX","artifacts":[]}'


class _Signer:
    def __init__(self, key: Ed25519PrivateKey, keyid: str) -> None:
        self._key = key
        self.keyid = keyid

    def sign(self, data: bytes) -> bytes:
        return self._key.sign(data)


def _public_pem(key: Ed25519PrivateKey, path: Path) -> Path:
    pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    path.write_bytes(pem)
    return path


def _private_pem(key: Ed25519PrivateKey, path: Path) -> Path:
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.write_bytes(pem)
    return path


def _write_env(tmp_path: Path, key: Ed25519PrivateKey) -> Path:
    env = wrap_bundle(_BUNDLE, _Signer(key, "novaseal:test"))
    path = tmp_path / "bundle.dsse.json"
    path.write_text(json.dumps(env))
    return path


def test_verify_ok(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    env = _write_env(tmp_path, key)
    pub = _public_pem(key, tmp_path / "pub.pem")
    result = runner.invoke(app, ["verify-envelope", str(env), "--key", str(pub)])
    assert result.exit_code == 0, result.output
    assert "verified" in result.output


def test_verify_accepts_private_key_pem(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    env = _write_env(tmp_path, key)
    priv = _private_pem(key, tmp_path / "priv.pem")
    result = runner.invoke(app, ["verify-envelope", str(env), "--key", str(priv)])
    assert result.exit_code == 0, result.output


def test_verify_wrong_key_fails(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    other = Ed25519PrivateKey.generate()
    env = _write_env(tmp_path, key)
    pub = _public_pem(other, tmp_path / "other.pem")
    result = runner.invoke(app, ["verify-envelope", str(env), "--key", str(pub)])
    assert result.exit_code == 1
    assert "FAILED" in result.output


def test_verify_tampered_payload_fails(tmp_path: Path) -> None:
    import base64

    key = Ed25519PrivateKey.generate()
    env_dict = wrap_bundle(_BUNDLE, _Signer(key, "k"))
    env_dict["payload"] = base64.b64encode(b'{"bundle_id":"EVIL"}').decode()
    env_path = tmp_path / "e.json"
    env_path.write_text(json.dumps(env_dict))
    pub = _public_pem(key, tmp_path / "pub.pem")
    result = runner.invoke(app, ["verify-envelope", str(env_path), "--key", str(pub)])
    assert result.exit_code == 1


def test_non_ed25519_key_rejected(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives.asymmetric import ec

    key = Ed25519PrivateKey.generate()
    env = _write_env(tmp_path, key)
    ec_key = ec.generate_private_key(ec.SECP256R1())
    ec_pem = ec_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    ec_path = tmp_path / "ec.pem"
    ec_path.write_bytes(ec_pem)
    result = runner.invoke(app, ["verify-envelope", str(env), "--key", str(ec_path)])
    assert result.exit_code != 0
    assert "Ed25519" in result.output


def test_help_smoke() -> None:
    result = runner.invoke(app, ["verify-envelope", "--help"])
    assert result.exit_code == 0
    assert "--key" in result.output
