"""Tests for `nova pricing list|show|add` and `nova cost estimate` (ADR-0133)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.cli.cost import app as cost_app
from novafabric.cli.pricing import app as pricing_app

runner = CliRunner()


def _all_output(result: Any) -> str:
    """stdout + stderr (click >= 8.2 no longer mixes them into .output)."""
    try:
        stderr = result.stderr or ""
    except ValueError:  # stderr not captured separately
        stderr = ""
    return result.output + stderr


@pytest.fixture(autouse=True)
def _isolated_layers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every discovery layer at empty tmp dirs; chdir into a tmp project."""
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    project = tmp_path / "project"
    for directory in (home, xdg, project):
        directory.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.chdir(project)
    return tmp_path


def _catalog(models: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schema_version": "0.1.0", "models": models}


def _selfhosted_models() -> list[dict[str, Any]]:
    return [
        {
            "model_id": "mistral-7b-local",
            "currency": "USD",
            "source": "internal chargeback",
            "pricing": {
                "input": {"amount": 0.1, "unit": "per_1m"},
                "output": {"amount": 0.3, "unit": "per_1m"},
            },
        }
    ]


def _write_project_catalog(models: list[dict[str, Any]]) -> Path:
    path = Path.cwd() / ".novafabric" / "pricing.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(_catalog(models)), encoding="utf-8")
    return path


def _write_capsule(records: list[dict[str, Any]]) -> Path:
    capsule = Path.cwd() / "capsule"
    capsule.mkdir()
    lines = "\n".join(json.dumps(r) for r in records)
    (capsule / "model-calls.jsonl").write_text(lines + "\n", encoding="utf-8")
    return capsule


# ---------------------------------------------------------------------------
# nova pricing list
# ---------------------------------------------------------------------------


class TestPricingList:
    def test_lists_builtin_layer(self) -> None:
        result = runner.invoke(pricing_app, ["list"])
        assert result.exit_code == 0
        assert "gpt-4o" in result.output
        assert "builtin" in result.output
        assert "catalog digest: sha256:" in result.output

    def test_lists_project_layer_with_source(self) -> None:
        _write_project_catalog(_selfhosted_models())
        result = runner.invoke(pricing_app, ["list"])
        assert result.exit_code == 0
        assert "mistral-7b-local" in result.output
        assert "project" in result.output
        assert "internal chargeback" in result.output

    def test_json_output(self) -> None:
        _write_project_catalog(_selfhosted_models())
        result = runner.invoke(pricing_app, ["list", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["pricing_catalog_digest"].startswith("sha256:")
        entries = {e["model_id"]: e for e in payload["entries"]}
        assert entries["mistral-7b-local"]["layer"] == "project"
        assert entries["gpt-4o"]["layer"] == "builtin"

    def test_explicit_catalog_flag(self, tmp_path: Path) -> None:
        explicit = tmp_path / "explicit.yaml"
        explicit.write_text(
            yaml.safe_dump(_catalog(_selfhosted_models())), encoding="utf-8"
        )
        result = runner.invoke(pricing_app, ["list", "--pricing-catalog", str(explicit)])
        assert result.exit_code == 0
        assert "explicit" in result.output

    def test_malformed_explicit_catalog_errors(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("models: [unclosed", encoding="utf-8")
        result = runner.invoke(pricing_app, ["list", "--pricing-catalog", str(bad)])
        assert result.exit_code == 1
        assert "Pricing error" in _all_output(result)


# ---------------------------------------------------------------------------
# nova pricing show
# ---------------------------------------------------------------------------


class TestPricingShow:
    def test_show_builtin_model(self) -> None:
        result = runner.invoke(pricing_app, ["show", "gpt-4o"])
        assert result.exit_code == 0
        assert "builtin" in result.output
        assert "0.0025" in result.output

    def test_show_unknown_model_exits_1(self) -> None:
        result = runner.invoke(pricing_app, ["show", "no-such-model"])
        assert result.exit_code == 1
        assert "not priced" in _all_output(result)

    def test_show_at_date_resolves_price_history(self) -> None:
        _write_project_catalog(
            [
                {
                    "model_id": "m",
                    "effective_from": "2026-01-01",
                    "pricing": {"input": {"amount": 1.0}},
                },
                {
                    "model_id": "m",
                    "effective_from": "2026-07-01",
                    "pricing": {"input": {"amount": 2.0}},
                },
            ]
        )
        early = runner.invoke(pricing_app, ["show", "m", "--at", "2026-03-02", "--json"])
        late = runner.invoke(pricing_app, ["show", "m", "--at", "2026-08-10", "--json"])
        assert early.exit_code == 0 and late.exit_code == 0
        assert json.loads(early.output)["pricing"]["input"]["amount"] == 1.0
        assert json.loads(late.output)["pricing"]["input"]["amount"] == 2.0

    def test_show_bad_at_date_errors(self) -> None:
        result = runner.invoke(pricing_app, ["show", "gpt-4o", "--at", "yesterday"])
        assert result.exit_code == 1
        assert "YYYY-MM-DD" in _all_output(result)

    def test_show_table_includes_date_and_source(self) -> None:
        _write_project_catalog(
            [
                {
                    "model_id": "m",
                    "effective_from": "2026-01-01",
                    "source": "chargeback sheet",
                    "pricing": {"input": {"amount": 1.0}},
                }
            ]
        )
        result = runner.invoke(pricing_app, ["show", "m"])
        assert result.exit_code == 0
        assert "effective_from: 2026-01-01" in result.output
        assert "chargeback sheet" in result.output


# ---------------------------------------------------------------------------
# nova pricing add
# ---------------------------------------------------------------------------


class TestPricingAdd:
    def test_add_creates_project_catalog(self) -> None:
        result = runner.invoke(
            pricing_app,
            [
                "add",
                "my-org/finetune-v3",
                "--input",
                "3.0",
                "--output",
                "15.0",
                "--unit",
                "per_1m",
                "--source",
                "vendor sheet",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "added" in result.output
        path = Path.cwd() / ".novafabric" / "pricing.yaml"
        assert path.is_file()
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert document["schema_version"] == "0.1.0"
        assert document["models"][0]["model_id"] == "my-org/finetune-v3"
        assert document["models"][0]["pricing"]["input"]["unit"] == "per_1m"
        # And the new entry resolves through the normal merged path.
        shown = runner.invoke(pricing_app, ["show", "my-org/finetune-v3"])
        assert shown.exit_code == 0
        assert "project" in shown.output

    def test_add_is_idempotent_per_model_and_date(self) -> None:
        args = ["add", "m", "--input", "1.0", "--effective-from", "2026-07-01"]
        assert runner.invoke(pricing_app, args).exit_code == 0
        update = runner.invoke(
            pricing_app,
            ["add", "m", "--input", "2.0", "--effective-from", "2026-07-01"],
        )
        assert update.exit_code == 0
        assert "updated" in update.output
        document = yaml.safe_load(
            (Path.cwd() / ".novafabric" / "pricing.yaml").read_text(encoding="utf-8")
        )
        assert len(document["models"]) == 1
        assert document["models"][0]["pricing"]["input"]["amount"] == 2.0

    def test_add_different_dates_appends_history(self) -> None:
        runner.invoke(pricing_app, ["add", "m", "--input", "1.0", "--effective-from", "2026-01-01"])
        runner.invoke(pricing_app, ["add", "m", "--input", "2.0", "--effective-from", "2026-07-01"])
        document = yaml.safe_load(
            (Path.cwd() / ".novafabric" / "pricing.yaml").read_text(encoding="utf-8")
        )
        assert len(document["models"]) == 2

    def test_add_user_flag_writes_user_catalog(self, tmp_path: Path) -> None:
        result = runner.invoke(pricing_app, ["add", "m", "--input", "1.0", "--user"])
        assert result.exit_code == 0
        assert (tmp_path / "xdg" / "novafabric" / "pricing.yaml").is_file()

    def test_add_requires_at_least_one_price(self) -> None:
        result = runner.invoke(pricing_app, ["add", "m"])
        assert result.exit_code == 1
        assert "at least one price" in _all_output(result)

    def test_add_rejects_bad_unit_and_currency(self) -> None:
        bad_unit = runner.invoke(pricing_app, ["add", "m", "--input", "1", "--unit", "per_token"])
        assert bad_unit.exit_code == 1
        bad_currency = runner.invoke(
            pricing_app, ["add", "m", "--input", "1", "--currency", "dollars"]
        )
        assert bad_currency.exit_code == 1

    def test_add_explicit_json_catalog_target(self, tmp_path: Path) -> None:
        target = tmp_path / "prices.json"
        result = runner.invoke(
            pricing_app, ["add", "m", "--input", "1.0", "--catalog", str(target)]
        )
        assert result.exit_code == 0
        document = json.loads(target.read_text(encoding="utf-8"))
        assert document["models"][0]["model_id"] == "m"
        # Round-trips through the loader on a second add.
        again = runner.invoke(
            pricing_app, ["add", "m", "--input", "2.0", "--catalog", str(target)]
        )
        assert again.exit_code == 0
        assert "updated" in again.output

    def test_add_rejects_unsupported_target_suffix(self, tmp_path: Path) -> None:
        target = tmp_path / "prices.toml"
        result = runner.invoke(
            pricing_app, ["add", "m", "--input", "1.0", "--catalog", str(target)]
        )
        assert result.exit_code == 1
        assert "unsupported" in _all_output(result)
        assert not target.exists()

    def test_add_refuses_to_clobber_malformed_catalog(self) -> None:
        path = Path.cwd() / ".novafabric" / "pricing.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("models: [unclosed", encoding="utf-8")
        result = runner.invoke(pricing_app, ["add", "m", "--input", "1.0"])
        assert result.exit_code == 1
        assert "malformed" in _all_output(result)
        assert path.read_text(encoding="utf-8") == "models: [unclosed"


# ---------------------------------------------------------------------------
# nova cost estimate (offline)
# ---------------------------------------------------------------------------


class TestCostEstimate:
    def _records(self) -> list[dict[str, Any]]:
        return [
            {  # recorded cost: reported verbatim, never recomputed
                "gen_ai.request.model": "mistral-7b-local",
                "gen_ai.usage.input_tokens": 1_000_000,
                "gen_ai.usage.output_tokens": 500_000,
                "nova.cost": {"currency": "USD", "amount": 0.42},
            },
            {  # no recorded cost: derived from the catalog -> estimated
                "gen_ai.request.model": "mistral-7b-local",
                "nova.usage": {"input_tokens": 1_000_000, "output_tokens": 500_000},
            },
            {  # unknown model: stays unpriced (cost 0.0, as before)
                "gen_ai.request.model": "unknown-model",
                "gen_ai.usage.input_tokens": 10,
                "gen_ai.usage.output_tokens": 10,
            },
        ]

    def test_recorded_vs_estimated_vs_unpriced(self) -> None:
        _write_project_catalog(_selfhosted_models())
        capsule = _write_capsule(self._records())
        result = runner.invoke(cost_app, ["estimate", str(capsule), "--format", "json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["calls"] == 3
        assert payload["pricing_catalog_digest"].startswith("sha256:")
        rows = {(r["model_id"], r["basis"]): r for r in payload["rows"]}
        recorded = rows[("mistral-7b-local", "recorded")]
        assert recorded["amount"] == pytest.approx(0.42)  # verbatim, not recomputed
        estimated = rows[("mistral-7b-local", "estimated")]
        assert estimated["amount"] == pytest.approx(0.25)
        assert estimated["layer"] == "project"
        assert ("unknown-model", "unpriced") in rows
        totals = {(t["basis"], t["currency"]): t["amount"] for t in payload["totals"]}
        assert totals[("recorded", "USD")] == pytest.approx(0.42)
        assert totals[("estimated", "USD")] == pytest.approx(0.25)

    def test_table_output_labels_bases(self) -> None:
        _write_project_catalog(_selfhosted_models())
        capsule = _write_capsule(self._records())
        result = runner.invoke(cost_app, ["estimate", str(capsule)])
        assert result.exit_code == 0, result.output
        assert "recorded" in result.output
        assert "estimated" in result.output
        assert "unpriced" in result.output
        assert "estimates, not billing records" in result.output

    def test_absent_catalog_leaves_self_hosted_unpriced(self) -> None:
        capsule = _write_capsule(self._records())
        result = runner.invoke(cost_app, ["estimate", str(capsule), "--format", "json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        bases = {(r["model_id"], r["basis"]) for r in payload["rows"]}
        # Recorded cost still surfaces; the derivable call is now unpriced.
        assert ("mistral-7b-local", "recorded") in bases
        assert ("mistral-7b-local", "unpriced") in bases
        assert ("mistral-7b-local", "estimated") not in bases

    def test_explicit_catalog_flag(self, tmp_path: Path) -> None:
        explicit = tmp_path / "prices.yaml"
        explicit.write_text(
            yaml.safe_dump(_catalog(_selfhosted_models())), encoding="utf-8"
        )
        capsule = _write_capsule(self._records())
        result = runner.invoke(
            cost_app,
            ["estimate", str(capsule), "--pricing-catalog", str(explicit), "--format", "json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        rows = {(r["model_id"], r["basis"]): r for r in payload["rows"]}
        assert rows[("mistral-7b-local", "estimated")]["layer"] == "explicit"

    def test_at_date_prices_with_history(self) -> None:
        _write_project_catalog(
            [
                {
                    "model_id": "m",
                    "effective_from": "2026-01-01",
                    "pricing": {"input": {"amount": 1.0}},
                },
                {
                    "model_id": "m",
                    "effective_from": "2026-07-01",
                    "pricing": {"input": {"amount": 2.0}},
                },
            ]
        )
        capsule = _write_capsule(
            [{"gen_ai.request.model": "m", "gen_ai.usage.input_tokens": 1000}]
        )
        result = runner.invoke(
            cost_app, ["estimate", str(capsule), "--at", "2026-03-02", "--format", "json"]
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["rows"][0]["amount"] == pytest.approx(1.0)

    def test_missing_capsule_dir_errors(self) -> None:
        result = runner.invoke(cost_app, ["estimate", "no-such-dir"])
        assert result.exit_code == 1
        assert "not found" in _all_output(result)

    def test_empty_capsule_reports_no_calls(self) -> None:
        capsule = Path.cwd() / "capsule"
        capsule.mkdir()
        result = runner.invoke(cost_app, ["estimate", str(capsule)])
        assert result.exit_code == 0
        assert "(no model calls)" in result.output

    def test_malformed_explicit_catalog_errors(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("models: [unclosed", encoding="utf-8")
        capsule = _write_capsule(self._records())
        result = runner.invoke(
            cost_app, ["estimate", str(capsule), "--pricing-catalog", str(bad)]
        )
        assert result.exit_code == 1

    def test_bad_format_and_bad_at_error(self) -> None:
        capsule = _write_capsule([])
        assert runner.invoke(cost_app, ["estimate", str(capsule), "--format", "xml"]).exit_code == 1
        assert runner.invoke(cost_app, ["estimate", str(capsule), "--at", "now"]).exit_code == 1

    def test_junk_lines_are_skipped(self) -> None:
        capsule = Path.cwd() / "capsule"
        capsule.mkdir()
        (capsule / "model-calls.jsonl").write_text(
            '{"gen_ai.request.model": "m", "nova.cost": {"currency": "USD", "amount": 1.0}}\n'
            "\n"  # blank line
            "not-json{{{\n"  # malformed line
            "[1, 2]\n",  # non-object record
            encoding="utf-8",
        )
        result = runner.invoke(cost_app, ["estimate", str(capsule), "--format", "json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["calls"] == 1
        assert payload["rows"][0]["basis"] == "recorded"

    def test_malformed_project_catalog_warns_but_does_not_fail(self) -> None:
        bad = Path.cwd() / ".novafabric" / "pricing.yaml"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("models: [unclosed", encoding="utf-8")
        capsule = _write_capsule(self._records())
        result = runner.invoke(cost_app, ["estimate", str(capsule), "--format", "json"])
        assert result.exit_code == 0
        assert "skipped" in _all_output(result)
        payload = json.loads(result.stdout)  # warning went to stderr, JSON to stdout
        assert payload["calls"] == 3
