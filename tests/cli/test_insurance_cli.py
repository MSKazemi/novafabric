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

"""CLI: ``nova insurance liability show | sla verify | coverage check`` (ADR-0170 P2).

Covers every subcommand, the exit-code contract (0 recorded / 2 input error),
the in-mission-boundary line on every output (rich and ``--json``), ``--write``
persistence, and ``--help`` smoke.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from rich.console import Console
from typer.testing import CliRunner

import novafabric.cli.insurance as insurance_cli
from novafabric.cli.main import app

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "risk-transfer"
BOUNDARY = "never determines, an insurance or legal outcome"


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(insurance_cli, "console", Console(width=200))


@pytest.fixture
def capsule(tmp_path: Path) -> Path:
    d = tmp_path / "run_1"
    d.mkdir()
    (d / "capsule.yaml").write_text(
        yaml.safe_dump({"run_id": "run_1", "status": "success", "facets": {"other": {"k": 1}}}),
        encoding="utf-8",
    )
    return d


def _invoke(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, list(args))
    return result.exit_code, result.output


def _flat(text: str) -> str:
    return " ".join(text.split())


def _json(*args: str) -> dict[str, Any]:
    result = runner.invoke(app, [*args, "--json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert BOUNDARY in body["in_mission_boundary"]
    return body


def _manifest(capsule: Path) -> dict[str, Any]:
    return yaml.safe_load((capsule / "capsule.yaml").read_text())


@pytest.mark.parametrize(
    "cmd",
    [
        ["insurance", "--help"],
        ["insurance", "liability", "show", "--help"],
        ["insurance", "sla", "verify", "--help"],
        ["insurance", "coverage", "check", "--help"],
    ],
)
def test_help_smoke(cmd: list[str]) -> None:
    code, out = _invoke(*cmd)
    assert code == 0 and "Usage" in out


# ── liability show ─────────────────────────────────────────────────────────


def test_liability_show_from_declared_chain(capsule: Path) -> None:
    before = (capsule / "capsule.yaml").read_bytes()
    code, out = _invoke(
        "insurance",
        "liability",
        "show",
        "--capsule",
        str(capsule),
        "--chain",
        str(FIXTURES / "liability-chain-valid.json"),
    )
    assert code == 0, out
    flat = _flat(out)
    assert "not a fault finding" in flat and BOUNDARY in flat
    assert "model_provider" in flat and "disputed" in flat
    assert (capsule / "capsule.yaml").read_bytes() == before


def test_liability_show_absent_chain_is_not_recorded(capsule: Path) -> None:
    code, out = _invoke("insurance", "liability", "show", "--capsule", str(capsule))
    assert code == 0
    assert "No liability chain recorded" in _flat(out) and BOUNDARY in _flat(out)
    body = _json("insurance", "liability", "show", "--capsule", str(capsule))
    assert body["liability_chain"] is None


def test_liability_write_then_show_reads_the_capsule(capsule: Path) -> None:
    chain = str(FIXTURES / "liability-chain-valid.json")
    code, out = _invoke(
        "insurance", "liability", "show", "--capsule", str(capsule), "--chain", chain, "--write"
    )
    assert code == 0 and "Wrote" in out
    manifest = _manifest(capsule)
    assert manifest["facets"]["other"] == {"k": 1}
    assert len(manifest["facets"]["risk_transfer"]["liability_chain"]) == 4
    body = _json("insurance", "liability", "show", "--capsule", str(capsule))
    assert [e["role"] for e in body["liability_chain"]][0] == "principal"


@pytest.mark.parametrize(
    "fixture",
    [
        "liability-chain-invalid-fault-key.json",
        "liability-chain-invalid-dangling.json",
        "liability-chain-invalid-unsourced.json",
    ],
)
def test_liability_invalid_fixtures_exit_2_and_do_not_write(capsule: Path, fixture: str) -> None:
    before = (capsule / "capsule.yaml").read_bytes()
    code, out = _invoke(
        "insurance",
        "liability",
        "show",
        "--capsule",
        str(capsule),
        "--chain",
        str(FIXTURES / fixture),
        "--write",
    )
    assert code == 2 and "Invalid liability chain" in out
    assert (capsule / "capsule.yaml").read_bytes() == before


def test_liability_write_requires_chain(capsule: Path) -> None:
    code, _ = _invoke("insurance", "liability", "show", "--capsule", str(capsule), "--write")
    assert code == 2


def test_liability_chain_must_be_list(capsule: Path, tmp_path: Path) -> None:
    doc = tmp_path / "c.json"
    doc.write_text(json.dumps({"liability_chain": {"role": "agent"}}))
    code, _ = _invoke(
        "insurance", "liability", "show", "--capsule", str(capsule), "--chain", str(doc)
    )
    assert code == 2


def test_unknown_capsule_exits_2(tmp_path: Path) -> None:
    code, _ = _invoke("insurance", "liability", "show", "--capsule", str(tmp_path / "nope/x"))
    assert code == 2


def test_oversize_and_malformed_inputs_exit_2(
    capsule: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    code, _ = _invoke(
        "insurance", "liability", "show", "--capsule", str(capsule), "--chain", str(bad)
    )
    assert code == 2
    code, _ = _invoke(
        "insurance",
        "liability",
        "show",
        "--capsule",
        str(capsule),
        "--chain",
        str(tmp_path / "missing.json"),
    )
    assert code == 2
    monkeypatch.setattr(insurance_cli, "MAX_INPUT_BYTES", 10)
    code, out = _invoke(
        "insurance",
        "liability",
        "show",
        "--capsule",
        str(capsule),
        "--chain",
        str(FIXTURES / "liability-chain-valid.json"),
    )
    assert code == 2 and "cap" in out


def test_non_mapping_manifest_exits_2(capsule: Path) -> None:
    (capsule / "capsule.yaml").write_text("- a\n- b\n")
    code, _ = _invoke("insurance", "liability", "show", "--capsule", str(capsule))
    assert code == 2
    (capsule / "capsule.yaml").write_text("a: [unclosed\n")
    code, _ = _invoke("insurance", "liability", "show", "--capsule", str(capsule))
    assert code == 2


def test_read_race_beyond_cap_exits_2(capsule: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file that grows between stat() and read() is still capped."""
    monkeypatch.setattr(insurance_cli, "MAX_MANIFEST_BYTES", 5)
    real_stat = Path.stat

    def fake_stat(self: Path, *a: Any, **kw: Any) -> Any:
        st = real_stat(self, *a, **kw)

        class _S:
            st_size = 0
            st_mode = st.st_mode

        return _S() if self.name == "capsule.yaml" else st

    monkeypatch.setattr(Path, "stat", fake_stat)
    code, _ = _invoke("insurance", "liability", "show", "--capsule", str(capsule))
    assert code == 2


# ── sla verify ─────────────────────────────────────────────────────────────


def _sla(capsule: Path, *extra: str, terms: str = "sla-terms-valid.json") -> tuple[int, str]:
    return _invoke(
        "insurance",
        "sla",
        "verify",
        "--capsule",
        str(capsule),
        "--sla",
        str(FIXTURES / terms),
        *extra,
    )


def test_sla_verify_records_breach_exit_0(capsule: Path) -> None:
    code, out = _sla(capsule, "--observed", "99.2")
    assert code == 0, out
    flat = _flat(out)
    assert "breach: true" in flat and "no remedy" in flat and BOUNDARY in flat


def test_sla_verify_json_and_write(capsule: Path) -> None:
    ev = "sha256:" + "b" * 64
    body = _json(
        "insurance",
        "sla",
        "verify",
        "--capsule",
        str(capsule),
        "--sla",
        str(FIXTURES / "sla-terms-valid.json"),
        "--observed",
        "99.95",
        "--evidence-ref",
        ev,
        "--write",
    )
    rec = body["sla_breach"]
    assert rec["breach"] is False and rec["threshold"] == "99.9"
    assert rec["evidence_refs"] == [ev]
    assert rec["sla_ref"].startswith("sha256:")
    stored = _manifest(capsule)["facets"]["risk_transfer"]["sla_breach"]
    assert stored["observed_value"] == "99.95"


def test_sla_json_number_threshold_is_parsed_exactly(capsule: Path, tmp_path: Path) -> None:
    terms = tmp_path / "t.json"
    terms.write_text('{"metric":"availability","operator":"gte","threshold":99.9,"window":"m"}')
    body = _json(
        "insurance",
        "sla",
        "verify",
        "--capsule",
        str(capsule),
        "--sla",
        str(terms),
        "--observed",
        "99.9",
    )
    assert body["sla_breach"]["threshold"] == "99.9" and body["sla_breach"]["breach"] is False


@pytest.mark.parametrize("observed", ["NaN", "abc", "inf"])
def test_sla_bad_observed_exits_2(capsule: Path, observed: str) -> None:
    code, out = _sla(capsule, "--observed", observed)
    assert code == 2 and "Invalid SLA input" in out


def test_sla_nan_threshold_fixture_exits_2(capsule: Path) -> None:
    code, _ = _sla(capsule, "--observed", "1", terms="sla-terms-invalid-nan.json")
    assert code == 2


def test_sla_terms_must_be_object(capsule: Path, tmp_path: Path) -> None:
    t = tmp_path / "t.json"
    t.write_text("[1]")
    code, _ = _invoke(
        "insurance", "sla", "verify", "--capsule", str(capsule), "--sla", str(t), "--observed", "1"
    )
    assert code == 2


def test_sla_bad_evidence_ref_exits_2(capsule: Path) -> None:
    code, _ = _sla(capsule, "--observed", "1", "--evidence-ref", "probe.log")
    assert code == 2


# ── coverage check ─────────────────────────────────────────────────────────


def _cov(capsule: Path, *extra: str, facts: str | None = None) -> list[str]:
    return [
        "insurance",
        "coverage",
        "check",
        "--capsule",
        str(capsule),
        "--exclusions",
        str(FIXTURES / "coverage-exclusions-cg4047.json"),
        "--facts",
        facts or str(FIXTURES / "coverage-facts-valid.json"),
        "--event-kind",
        "erroneous_output",
        *extra,
    ]


def test_coverage_check_records_facts_not_coverage(capsule: Path) -> None:
    code, out = _invoke(*_cov(capsule))
    assert code == 0, out
    flat = _flat(out)
    assert "CG 40 47 (01 26)" in flat and "condition_met=true" in flat
    assert "not observed" in flat and BOUNDARY in flat


def test_coverage_check_json_and_write(capsule: Path) -> None:
    body = _json(*_cov(capsule, "--write"))
    rec = body["coverage_trigger"]
    assert rec["matched_exclusions"][0]["exclusion_id"] == "CG 40 47 (01 26)"
    assert rec["parametric_conditions"][0]["observed_value"] == "0.07"
    stored = _manifest(capsule)["facets"]["risk_transfer"]["coverage_trigger"]
    assert stored["covered_event_kind"] == "erroneous_output"


def test_coverage_no_match_says_it_does_not_mean_policy_responds(
    capsule: Path, tmp_path: Path
) -> None:
    facts = tmp_path / "f.json"
    facts.write_text(json.dumps({"trigger_facts": []}))
    code, out = _invoke(*_cov(capsule, facts=str(facts)))
    assert code == 0
    assert "does not mean the policy responds" in _flat(out)


@pytest.mark.parametrize(
    "facts_doc",
    [
        [1, 2],
        {"trigger_facts": {"a": 1}},
        {"trigger_facts": [], "observations": [1]},
        {"trigger_facts": [{"fact_ref": "raw span text", "marker": "x"}]},
        {"trigger_facts": [], "observations": {"error_rate": "NaN"}},
    ],
)
def test_coverage_bad_facts_exit_2(capsule: Path, tmp_path: Path, facts_doc: Any) -> None:
    facts = tmp_path / "f.json"
    facts.write_text(json.dumps(facts_doc))
    code, out = _invoke(*_cov(capsule, facts=str(facts)))
    assert code == 2 and "Invalid coverage input" in out


def test_write_merges_with_existing_facet_and_revalidates(capsule: Path) -> None:
    assert _invoke(*_cov(capsule, "--write"))[0] == 0
    code, _ = _sla(capsule, "--observed", "99.2", "--write")
    assert code == 0
    rt = _manifest(capsule)["facets"]["risk_transfer"]
    assert {"coverage_trigger", "sla_breach"} <= set(rt)


def test_write_refuses_when_existing_facet_is_tainted(capsule: Path) -> None:
    manifest = _manifest(capsule)
    manifest["facets"]["risk_transfer"] = {"payout_amount": "100"}
    (capsule / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    before = (capsule / "capsule.yaml").read_bytes()
    code, _ = _sla(capsule, "--observed", "99.2", "--write")
    assert code == 2
    assert (capsule / "capsule.yaml").read_bytes() == before


def test_run_id_resolution(capsule: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(capsule.parent))
    code, _ = _invoke("insurance", "liability", "show", "--capsule", "run_1")
    assert code == 0
