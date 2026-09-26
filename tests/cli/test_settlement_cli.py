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

"""CLI smoke + failure paths for ``nova settlement chain`` (ADR-0163 P3, NF-315)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from rich.console import Console
from typer.testing import CliRunner

from novafabric.cli import settlement as cli_mod
from novafabric.cli.main import app
from novafabric.cli.settlement import BOUNDARY_LINE

runner = CliRunner()

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "settlement"
VALID = FIXTURES / "valid-p3-facet.json"


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_mod, "console", Console(width=200, soft_wrap=True))


def _invoke(*args: str) -> Any:
    return runner.invoke(app, ["settlement", "chain", *args])


def _squash(text: str) -> str:
    return " ".join(text.split())


def _has_boundary(result: Any) -> bool:
    return _squash(BOUNDARY_LINE) in _squash(result.stderr)


def _capsule(tmp_path: Path, facets: dict[str, Any] | None) -> Path:
    d = tmp_path / "01HXAY7M5JZ8R7K4P9DPBYK2WX"
    d.mkdir()
    manifest: dict[str, Any] = {"run_id": d.name}
    if facets is not None:
        manifest["facets"] = facets
    (d / "capsule.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return d


def test_group_help_lists_chain() -> None:
    result = runner.invoke(app, ["settlement", "--help"])
    assert result.exit_code == 0
    assert "chain" in result.stdout


def test_chain_help() -> None:
    result = _invoke("--help")
    assert result.exit_code == 0
    assert "--depth" in _squash(result.stdout)


def test_valid_facet_walks_ok_json() -> None:
    result = _invoke("--facet", str(VALID), "--json")
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["acyclic"] and payload["no_broken_parent"]
    assert payload["moved_value"] is False
    assert [h["hop_index"] for h in payload["walk"]] == [2, 1, 0]
    assert isinstance(payload["walk"][0]["amount_minor"], int)
    assert _has_boundary(result)


def test_depth_limits_the_walk_but_not_the_verdict() -> None:
    result = _invoke("--facet", str(VALID), "--json", "--depth", "1")
    payload = json.loads(result.stdout)
    assert [h["hop_index"] for h in payload["walk"]] == [2]
    assert payload["hop_count"] == 3 and payload["ok"] is True


def test_depth_must_be_positive() -> None:
    assert _invoke("--facet", str(VALID), "--depth", "0").exit_code == 2


def test_human_output_from_capsule(tmp_path: Path) -> None:
    settlement = json.loads(VALID.read_text())
    capsule = _capsule(tmp_path, {"settlement": settlement})
    result = _invoke("--capsule", str(capsule))
    assert result.exit_code == 0, result.stderr
    out = _squash(result.stdout)
    assert "a2a_payment_chain: ok (3 hops)" in out
    assert "moved_value: false" in out
    assert "#0 agent:acme-buyer -> agent:travel-broker 12000 EUR" in out
    assert _has_boundary(result)


@pytest.mark.parametrize(
    "name,code",
    [
        ("invalid-chain-cycle.json", "forward_parent"),
        ("invalid-chain-fork.json", "fork"),
        ("invalid-chain-currency-mismatch.json", "currency_mismatch"),
    ],
)
def test_broken_chain_exits_1_with_findings(name: str, code: str) -> None:
    result = _invoke("--facet", str(FIXTURES / name))
    assert result.exit_code == 1
    assert code in result.stdout
    assert "BROKEN" in result.stdout
    assert _has_boundary(result)


def test_broken_chain_json_reports_not_ok() -> None:
    result = _invoke("--facet", str(FIXTURES / "invalid-chain-cycle.json"), "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["acyclic"] is False


def test_pan_in_chain_exits_1_without_echoing_it() -> None:
    result = _invoke("--facet", str(FIXTURES / "invalid-chain-pan.json"))
    assert result.exit_code == 1
    assert "4111111111111111" not in result.stdout + result.stderr
    assert "card number" in _squash(result.stderr)


def test_float_amount_exits_1() -> None:
    result = _invoke("--facet", str(FIXTURES / "invalid-chain-float-amount.json"))
    assert result.exit_code == 1
    assert "a2a_payment_chain[1]" in result.stderr


def test_non_digest_reference_exits_1_without_echo(tmp_path: Path) -> None:
    path = tmp_path / "f.json"
    path.write_text(json.dumps({"protocol": "x402", "settlement_ref": "tok_live_SECRETISH"}))
    result = _invoke("--facet", str(path))
    assert result.exit_code == 1
    assert "tok_live_SECRETISH" not in result.stderr


def test_malformed_facet_exits_1_without_echo(tmp_path: Path) -> None:
    path = tmp_path / "f.json"
    path.write_text(json.dumps({"protocol": "paypal-SECRETISH"}))
    result = _invoke("--facet", str(path))
    assert result.exit_code == 1
    assert "malformed facets.settlement" in result.stderr
    assert "paypal-SECRETISH" not in result.stderr


def test_bad_identity_ref_in_chain_exits_1(tmp_path: Path) -> None:
    raw = json.loads(VALID.read_text())
    raw["a2a_payment_chain"][0]["payer_agent_ref"] = "has space"
    path = tmp_path / "f.json"
    path.write_text(json.dumps(raw))
    assert _invoke("--facet", str(path)).exit_code == 1


def test_facet_without_chain_exits_1(tmp_path: Path) -> None:
    path = tmp_path / "f.json"
    path.write_text(json.dumps({"protocol": "x402", "settlement_ref": "sha256:" + "a" * 64}))
    result = _invoke("--facet", str(path))
    assert result.exit_code == 1
    assert "no a2a_payment_chain" in result.stderr


def test_capsule_without_settlement_facet_exits_1(tmp_path: Path) -> None:
    result = _invoke("--capsule", str(_capsule(tmp_path, None)))
    assert result.exit_code == 1
    assert "no facets.settlement" in result.stderr
    assert _has_boundary(result)


def test_capsule_with_non_mapping_facet_exits_1(tmp_path: Path) -> None:
    result = _invoke("--capsule", str(_capsule(tmp_path, {"settlement": ["x"]})))
    assert result.exit_code == 1


def test_both_or_neither_source_is_usage_error() -> None:
    assert _invoke().exit_code == 2
    assert _invoke("--facet", str(VALID), "--capsule", "x").exit_code == 2


def test_unknown_capsule_is_usage_error(tmp_path: Path) -> None:
    result = _invoke("--capsule", str(tmp_path / "nope" / "deeper"))
    assert result.exit_code == 2


def test_unparseable_inputs_are_usage_errors(tmp_path: Path) -> None:
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not json")
    assert _invoke("--facet", str(bad_json)).exit_code == 2
    arr = tmp_path / "arr.json"
    arr.write_text("[]")
    assert _invoke("--facet", str(arr)).exit_code == 2
    assert _invoke("--facet", str(tmp_path / "missing.json")).exit_code == 2
    capsule = _capsule(tmp_path, None)
    (capsule / "capsule.yaml").write_text("a: [unclosed", encoding="utf-8")
    assert _invoke("--capsule", str(capsule)).exit_code == 2


def test_oversized_input_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_mod, "MAX_INPUT_BYTES", 10)
    result = _invoke("--facet", str(VALID))
    assert result.exit_code == 2
    assert "limit" in result.stderr


def test_capsule_is_never_modified(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, {"settlement": json.loads(VALID.read_text())})
    before = (capsule / "capsule.yaml").read_bytes()
    _invoke("--capsule", str(capsule), "--json")
    assert (capsule / "capsule.yaml").read_bytes() == before
