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

"""CLI smoke + exit-code contract for ``nova trust-path show|verify`` (NF-363).

Exit codes: 0 pass/read, 1 walk failed (fail closed), 2 usage/input error.
Every output carries the in-mission-boundary line.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from rich.console import Console
from typer.testing import CliRunner

import novafabric.cli.trust_path as cli_mod
from novafabric.cli.main import app
from novafabric.federation.trust_path import IN_MISSION_BOUNDARY, key_digest

from ._trust_path_fixtures import EVIL, FIXTURE_DIR, A, B, valid_path

runner = CliRunner()


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_mod, "console", Console(width=250))
    monkeypatch.setattr(cli_mod, "err_console", Console(width=250, stderr=True))


def _squash(text: str) -> str:
    return " ".join(text.split())


def _capsule(tmp_path: Path, trust_path: Any = None, *, name: str = "cap") -> Path:
    d = tmp_path / name
    d.mkdir()
    manifest: dict[str, Any] = {"run_id": name}
    if trust_path is not None:
        manifest["facets"] = {"federation": {"schema_version": "0.1.0", "trust_path": trust_path}}
    (d / "capsule.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return d


def _pem(tmp_path: Path, key: Ed25519PrivateKey, name: str = "anchor.pem") -> Path:
    p = tmp_path / name
    p.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    return p


def _verify(*args: str) -> Any:
    return runner.invoke(app, ["trust-path", "verify", *args])


def test_help_smoke() -> None:
    for argv in (
        ["trust-path", "--help"],
        ["trust-path", "show", "--help"],
        ["trust-path", "verify", "--help"],
    ):
        result = runner.invoke(app, argv)
        assert result.exit_code == 0, result.output
    out = _squash(runner.invoke(app, ["trust-path", "verify", "--help"]).output)
    for flag in ("--capsule", "--anchor", "--max-depth", "--revoked", "--strict-depth", "--json"):
        assert flag in out


def test_verify_valid_path_passes(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, valid_path())
    result = _verify("--capsule", str(cap), "--anchor", f"orgA={_pem(tmp_path, A)}")
    assert result.exit_code == 0, result.output
    out = _squash(result.output)
    assert _squash(IN_MISSION_BOUNDARY) in out
    assert "PASS trust path (2 hop(s))" in out
    assert "leaf: orgC" in out


def test_verify_json(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, valid_path(max0=0))
    result = _verify("--capsule", str(cap), "--anchor", f"orgA={_pem(tmp_path, A)}", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["boundary"] == IN_MISSION_BOUNDARY
    assert data["path_walk_ok"] and data["acyclic"] and data["terminates_at_anchor"]
    assert data["delegation_depth_exceeded"] is True and data["depth_flags"] == [0]


def test_verify_depth_policy_flag_does_not_change_exit(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, valid_path())
    result = _verify(
        "--capsule", str(cap), "--anchor", f"orgA={_pem(tmp_path, A)}", "--max-depth", "1"
    )
    assert result.exit_code == 0, result.output
    assert "flagged, not fatal" in _squash(result.output)
    assert "verifier --max-depth policy exceeded" in _squash(result.output)


def test_verify_signed_overrun_lenient_by_default(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, valid_path(max0=0))
    result = _verify("--capsule", str(cap), "--anchor", f"orgA={_pem(tmp_path, A)}")
    assert result.exit_code == 0, result.output
    out = _squash(result.output)
    assert "signed max_path_length exceeded at hop(s): [0] (flagged, not fatal)" in out
    assert "--max-depth policy" not in out


def test_verify_strict_depth_signed_overrun_exits_1(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, valid_path(max0=0))
    pem = f"orgA={_pem(tmp_path, A)}"
    result = _verify("--capsule", str(cap), "--anchor", pem, "--strict-depth")
    assert result.exit_code == 1, result.output
    out = _squash(result.output)
    assert "FAIL trust path" in out and "signed_depth_exceeded" in out
    assert "(fatal, --strict-depth)" in out
    js = _verify("--capsule", str(cap), "--anchor", pem, "--strict-depth", "--json")
    assert js.exit_code == 1
    data = json.loads(js.output)
    assert data["reason"] == "signed_depth_exceeded" and data["strict_depth"] is True
    assert data["depth_violation_hop"] == 1 and data["signed_depth_flags"] == [0]
    assert data["policy_depth_exceeded"] is False


def test_verify_strict_depth_policy_overrun_still_exits_0(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, valid_path())
    result = _verify(
        "--capsule", str(cap), "--anchor", f"orgA={_pem(tmp_path, A)}",
        "--max-depth", "1", "--strict-depth", "--json",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["path_walk_ok"] and data["policy_depth_exceeded"]


@pytest.mark.parametrize(
    "fixture",
    [
        "invalid-wrong-key.json",
        "invalid-reordered.json",
        "invalid-cycle.json",
        "invalid-unpinned-anchor.json",
        "invalid-anchor-substituted.json",
        "invalid-malformed-signature.json",
    ],
)
def test_verify_invalid_fixtures_exit_1_naming_reason(tmp_path: Path, fixture: str) -> None:
    case = json.loads((FIXTURE_DIR / fixture).read_text(encoding="utf-8"))
    cap = _capsule(tmp_path, case["trust_path"])
    result = _verify("--capsule", str(cap), "--anchor", f"orgA={_pem(tmp_path, A)}", "--json")
    assert result.exit_code == 1, result.output
    data = json.loads(result.output)
    assert data["path_walk_ok"] is False
    assert data["reason"] == case["expect"]["reason"]
    assert data["broken_hop"] == case["expect"]["broken_hop"]


def test_verify_text_failure_names_broken_hop(tmp_path: Path) -> None:
    case = json.loads((FIXTURE_DIR / "invalid-wrong-key.json").read_text("utf-8"))
    cap = _capsule(tmp_path, case["trust_path"])
    result = _verify("--capsule", str(cap), "--anchor", f"orgA={_pem(tmp_path, A)}")
    assert result.exit_code == 1
    out = _squash(result.output)
    assert "FAIL trust path" in out and "bad_signature at hop 1" in out
    assert "no_broken_hop: false" in out


def test_verify_against_wrong_pin_fails(tmp_path: Path) -> None:
    """Pinning a different key under orgA's name must not verify orgA's path."""
    cap = _capsule(tmp_path, valid_path())
    result = _verify("--capsule", str(cap), "--anchor", f"orgA={_pem(tmp_path, EVIL)}")
    assert result.exit_code == 1


def test_verify_revoked_subject_fails(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, valid_path())
    result = _verify(
        "--capsule",
        str(cap),
        "--anchor",
        f"orgA={_pem(tmp_path, A)}",
        "--revoked",
        key_digest(B.public_key()),
    )
    assert result.exit_code == 1
    out = _squash(result.output)
    assert "path_touches_revoked: true" in out and "revoked subjects transited" in out


def test_verify_without_path_fails_closed(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    result = _verify("--capsule", str(cap), "--anchor", f"orgA={_pem(tmp_path, A)}")
    assert result.exit_code == 1
    assert "no_trust_path" in result.output


def test_verify_malformed_path_fails_closed(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, [valid_path()[0]] * 17)
    result = _verify("--capsule", str(cap), "--anchor", f"orgA={_pem(tmp_path, A)}", "--json")
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["reason"] == "malformed_path" and data["path_walk_ok"] is False


def test_verify_requires_an_anchor(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, valid_path())
    assert _verify("--capsule", str(cap)).exit_code == 2


@pytest.mark.parametrize(
    "spec_factory",
    [
        lambda d: "orgA",
        lambda d: "=x.pem",
        lambda d: f"orgA={d / 'missing.pem'}",
        lambda d: f"bad org={_pem(d, A)}",
    ],
)
def test_verify_bad_anchor_spec_is_input_error(tmp_path: Path, spec_factory: Any) -> None:
    cap = _capsule(tmp_path, valid_path())
    result = _verify("--capsule", str(cap), "--anchor", spec_factory(tmp_path))
    assert result.exit_code == 2


def test_verify_private_key_anchor_is_refused(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, valid_path())
    key_file = tmp_path / "priv.pem"
    key_file.write_bytes(
        A.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    result = _verify("--capsule", str(cap), "--anchor", f"orgA={key_file}")
    assert result.exit_code == 2
    assert "private key" in _squash(result.output)


def test_verify_oversize_anchor_file_is_refused(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, valid_path())
    big = tmp_path / "big.pem"
    big.write_bytes(b"x" * (64 * 1024 + 1))
    assert _verify("--capsule", str(cap), "--anchor", f"orgA={big}").exit_code == 2


def test_verify_caps_anchor_and_revoked_counts(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, valid_path())
    pem = _pem(tmp_path, A)
    many = [a for _ in range(33) for a in ("--anchor", f"orgA={pem}")]
    assert _verify("--capsule", str(cap), *many).exit_code == 2
    rev = [a for i in range(257) for a in ("--revoked", f"s{i}")]
    assert _verify("--capsule", str(cap), "--anchor", f"orgA={pem}", *rev).exit_code == 2
    long_rev = ["--revoked", "r" * 300]
    assert _verify("--capsule", str(cap), "--anchor", f"orgA={pem}", *long_rev).exit_code == 2


def test_missing_capsule_is_input_error(tmp_path: Path) -> None:
    result = _verify(
        "--capsule", str(tmp_path / "nope" / "x"), "--anchor", f"orgA={_pem(tmp_path, A)}"
    )
    assert result.exit_code == 2
    assert result.stdout == "" or IN_MISSION_BOUNDARY[:20] in _squash(result.output)


def test_non_mapping_and_unreadable_manifest(tmp_path: Path) -> None:
    d = tmp_path / "cap"
    d.mkdir()
    (d / "capsule.yaml").write_text("- a\n- b\n", encoding="utf-8")
    assert runner.invoke(app, ["trust-path", "show", "--capsule", str(d)]).exit_code == 2
    (d / "capsule.yaml").write_text("a: [unclosed\n", encoding="utf-8")
    assert runner.invoke(app, ["trust-path", "show", "--capsule", str(d)]).exit_code == 2


def test_oversize_manifest_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _capsule(tmp_path, valid_path())
    monkeypatch.setattr(cli_mod, "_MAX_MANIFEST_BYTES", 10)
    assert runner.invoke(app, ["trust-path", "show", "--capsule", str(cap)]).exit_code == 2


def test_show_lists_hops_unverified(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, valid_path(max0=1))
    result = runner.invoke(app, ["trust-path", "show", "--capsule", str(cap)])
    assert result.exit_code == 0, result.output
    out = _squash(result.output)
    assert _squash(IN_MISSION_BOUNDARY) in out
    assert "UNVERIFIED" in out and "orgA" in out and "orgC" in out


def test_show_json(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, valid_path())
    result = runner.invoke(app, ["trust-path", "show", "--capsule", str(cap), "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["verified"] is False and len(data["trust_path"]) == 2
    assert data["boundary"] == IN_MISSION_BOUNDARY


def test_show_without_path(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    result = runner.invoke(app, ["trust-path", "show", "--capsule", str(cap)])
    assert result.exit_code == 0
    assert "No trust path recorded" in result.output


def test_show_malformed_path_is_input_error(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, "not-a-list")
    result = runner.invoke(app, ["trust-path", "show", "--capsule", str(cap)])
    assert result.exit_code == 2
