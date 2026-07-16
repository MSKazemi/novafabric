"""Tests for the local model-pricing catalog (ADR-0133, model-pricing-catalog-v0).

Covers: catalog file loading and validation (valid + invalid, YAML and JSON),
the golden fixture corpus against the graduated JSON Schema, layer merge
precedence (builtin < user < project < explicit, per-entry replacement),
effective-dated resolution, per-usage-type pricing math and units, the
recorded-cost-is-never-overwritten invariant with estimated labeling, the
catalog digest, and the absent-catalog = today's-behavior guarantee on
``CostInterceptor``.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from novafabric.cost.interceptor import CostInterceptor
from novafabric.cost.pricing_catalog import (
    PRICING_USAGE_TYPES,
    MergedCatalog,
    PricingCatalogError,
    builtin_entries,
    cost_for_model_call_record,
    load_catalog_file,
    load_merged_catalog,
    price_usage,
    resolve_entry,
    usage_counts_from_block,
)

REPO_ROOT = Path(__file__).parents[2]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "model-pricing-catalog"
SCHEMA_PATH = REPO_ROOT / "schemas" / "pricing-catalog.schema.json"


# ---------------------------------------------------------------------------
# Isolation: no real user/project catalog may leak into any test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_layers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Point every discovery layer at empty tmp dirs; chdir into a tmp project."""
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    project = tmp_path / "project"
    for directory in (home, xdg, project):
        directory.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.chdir(project)
    return {"home": home, "xdg": xdg, "project": project}


def _write_user_catalog(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "xdg" / "novafabric" / "pricing.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def _write_project_catalog(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "project" / ".novafabric" / "pricing.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def _catalog(models: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schema_version": "0.1.0", "models": models}


def _entry(
    model_id: str,
    input_amount: float,
    output_amount: float | None = None,
    unit: str = "per_1k",
    **extra: Any,
) -> dict[str, Any]:
    pricing: dict[str, Any] = {"input": {"amount": input_amount, "unit": unit}}
    if output_amount is not None:
        pricing["output"] = {"amount": output_amount, "unit": unit}
    return {"model_id": model_id, "pricing": pricing, **extra}


# ---------------------------------------------------------------------------
# Golden fixtures: JSON Schema and the runtime loader must agree
# ---------------------------------------------------------------------------


VALID_FIXTURES = sorted(p.name for p in FIXTURES_DIR.glob("valid-*.json"))
INVALID_FIXTURES = sorted(p.name for p in FIXTURES_DIR.glob("invalid-*.json"))


class TestGoldenFixtures:
    def test_corpus_is_complete(self) -> None:
        assert len(VALID_FIXTURES) == 6
        assert len(INVALID_FIXTURES) == 9

    @pytest.fixture()
    def validator(self) -> jsonschema.Draft202012Validator:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        return jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.FormatChecker()
        )

    @pytest.mark.parametrize("name", VALID_FIXTURES)
    def test_valid_fixture_passes_schema_and_loader(
        self, name: str, validator: jsonschema.Draft202012Validator
    ) -> None:
        path = FIXTURES_DIR / name
        validator.validate(json.loads(path.read_text(encoding="utf-8")))
        catalog = load_catalog_file(path)
        assert catalog.schema_version == "0.1.0"

    @pytest.mark.parametrize("name", INVALID_FIXTURES)
    def test_invalid_fixture_fails_schema_and_loader(
        self, name: str, validator: jsonschema.Draft202012Validator
    ) -> None:
        path = FIXTURES_DIR / name
        assert not validator.is_valid(json.loads(path.read_text(encoding="utf-8")))
        with pytest.raises(PricingCatalogError):
            load_catalog_file(path)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


class TestLoader:
    def test_loads_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "pricing.yaml"
        path.write_text(
            yaml.safe_dump(_catalog([_entry("m", 1.0, 2.0, unit="per_1m")])),
            encoding="utf-8",
        )
        catalog = load_catalog_file(path)
        assert catalog.models[0].model_id == "m"
        assert catalog.models[0].pricing.input is not None
        assert catalog.models[0].pricing.input.unit == "per_1m"

    def test_loads_yml_and_json_suffixes(self, tmp_path: Path) -> None:
        payload = _catalog([_entry("m", 1.0)])
        yml = tmp_path / "pricing.yml"
        yml.write_text(yaml.safe_dump(payload), encoding="utf-8")
        js = tmp_path / "pricing.json"
        js.write_text(json.dumps(payload), encoding="utf-8")
        assert load_catalog_file(yml).models[0].model_id == "m"
        assert load_catalog_file(js).models[0].model_id == "m"

    def test_yaml_unquoted_date_is_accepted(self, tmp_path: Path) -> None:
        path = tmp_path / "pricing.yaml"
        path.write_text(
            "schema_version: '0.1.0'\n"
            "models:\n"
            "  - model_id: m\n"
            "    effective_from: 2026-07-01\n"  # YAML parses this to datetime.date
            "    pricing:\n"
            "      input: {amount: 1.0}\n",
            encoding="utf-8",
        )
        catalog = load_catalog_file(path)
        assert catalog.models[0].effective_from == date(2026, 7, 1)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(PricingCatalogError, match="cannot read"):
            load_catalog_file(tmp_path / "nope.yaml")

    def test_malformed_yaml_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "pricing.yaml"
        path.write_text("models: [unclosed", encoding="utf-8")
        with pytest.raises(PricingCatalogError, match="malformed"):
            load_catalog_file(path)

    def test_non_mapping_top_level_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "pricing.json"
        path.write_text("[1, 2]", encoding="utf-8")
        with pytest.raises(PricingCatalogError, match="top level"):
            load_catalog_file(path)

    def test_unsupported_suffix_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "pricing.toml"
        path.write_text("x = 1", encoding="utf-8")
        with pytest.raises(PricingCatalogError, match="unsupported"):
            load_catalog_file(path)

    def test_wrong_schema_version_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "pricing.json"
        path.write_text(json.dumps({"schema_version": "9.9.9", "models": []}))
        with pytest.raises(PricingCatalogError, match="invalid"):
            load_catalog_file(path)

    def test_bad_effective_from_string_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "pricing.json"
        payload = _catalog([_entry("m", 1.0, effective_from="01-07-2026")])
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(PricingCatalogError, match="invalid"):
            load_catalog_file(path)

    def test_impossible_calendar_date_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "pricing.json"
        payload = _catalog([_entry("m", 1.0, effective_from="2026-13-99")])
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(PricingCatalogError, match="invalid"):
            load_catalog_file(path)


# ---------------------------------------------------------------------------
# Merge precedence (D1)
# ---------------------------------------------------------------------------


class TestMergePrecedence:
    def test_builtin_only_when_no_catalog_files(self) -> None:
        merged = load_merged_catalog()
        assert merged.warnings == ()
        assert set(merged.layers.values()) == {"builtin"}
        gpt = merged.entries["gpt-4o"]
        assert len(gpt) == 1
        assert gpt[0].pricing.input is not None
        assert gpt[0].pricing.input.amount == pytest.approx(0.0025)

    def test_builtin_matches_price_table(self) -> None:
        entries = builtin_entries()
        assert {e.model_id for e in entries} == set(CostInterceptor.PRICE_TABLE)
        for entry in entries:
            expected = CostInterceptor.PRICE_TABLE[entry.model_id]
            assert entry.pricing.input is not None
            assert entry.pricing.output is not None
            assert (entry.pricing.input.amount, entry.pricing.output.amount) == expected
            assert entry.currency == "USD"

    def test_user_layer_adds_and_overrides(self, tmp_path: Path) -> None:
        _write_user_catalog(
            tmp_path,
            _catalog(
                [
                    _entry("mistral-7b-local", 0.1, 0.3, unit="per_1m"),
                    _entry("gpt-4o", 9.0, 9.0),
                ]
            ),
        )
        merged = load_merged_catalog()
        assert merged.layers["mistral-7b-local"] == "user"
        assert merged.layers["gpt-4o"] == "user"
        assert merged.layers["gpt-4o-mini"] == "builtin"
        gpt = merged.entries["gpt-4o"][0]
        assert gpt.pricing.input is not None
        assert gpt.pricing.input.amount == 9.0

    def test_project_overrides_user_and_explicit_overrides_project(
        self, tmp_path: Path
    ) -> None:
        _write_user_catalog(tmp_path, _catalog([_entry("m", 1.0)]))
        _write_project_catalog(tmp_path, _catalog([_entry("m", 2.0)]))
        merged = load_merged_catalog()
        assert merged.layers["m"] == "project"
        entry = merged.entries["m"][0]
        assert entry.pricing.input is not None
        assert entry.pricing.input.amount == 2.0

        explicit = tmp_path / "explicit.yaml"
        explicit.write_text(yaml.safe_dump(_catalog([_entry("m", 3.0)])), encoding="utf-8")
        merged = load_merged_catalog(explicit=explicit)
        assert merged.layers["m"] == "explicit"
        entry = merged.entries["m"][0]
        assert entry.pricing.input is not None
        assert entry.pricing.input.amount == 3.0

    def test_higher_layer_replaces_whole_price_history(self, tmp_path: Path) -> None:
        _write_user_catalog(
            tmp_path,
            _catalog(
                [
                    _entry("m", 1.0, effective_from="2026-01-01"),
                    _entry("m", 2.0, effective_from="2026-06-01"),
                ]
            ),
        )
        _write_project_catalog(tmp_path, _catalog([_entry("m", 5.0)]))
        merged = load_merged_catalog()
        # Per-entry replacement: the user layer's dated history is gone.
        assert len(merged.entries["m"]) == 1
        assert merged.entries["m"][0].effective_from is None

    def test_within_file_duplicate_last_wins(self, tmp_path: Path) -> None:
        _write_project_catalog(
            tmp_path,
            _catalog([_entry("m", 1.0), _entry("m", 7.0)]),
        )
        merged = load_merged_catalog()
        assert len(merged.entries["m"]) == 1
        entry = merged.entries["m"][0]
        assert entry.pricing.input is not None
        assert entry.pricing.input.amount == 7.0

    def test_malformed_layer_is_skipped_with_warning(self, tmp_path: Path) -> None:
        _write_user_catalog(tmp_path, _catalog([_entry("m", 1.0)]))
        bad = tmp_path / "project" / ".novafabric" / "pricing.yaml"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("models: [unclosed", encoding="utf-8")
        merged = load_merged_catalog()
        assert len(merged.warnings) == 1
        assert "project" in merged.warnings[0]
        # The user layer still applies (falls back, never fails).
        assert merged.layers["m"] == "user"

    def test_pricing_yml_and_json_discovered(self, tmp_path: Path) -> None:
        directory = tmp_path / "project" / ".novafabric"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "pricing.json").write_text(
            json.dumps(_catalog([_entry("m", 4.0)])), encoding="utf-8"
        )
        merged = load_merged_catalog()
        assert merged.layers["m"] == "project"


# ---------------------------------------------------------------------------
# Resolution (D3) and effective dating (P4)
# ---------------------------------------------------------------------------


def _merged_with(models: list[dict[str, Any]], tmp_path: Path) -> MergedCatalog:
    _write_project_catalog(tmp_path, _catalog(models))
    return load_merged_catalog()


class TestResolution:
    def test_unknown_model_resolves_none(self) -> None:
        merged = load_merged_catalog()
        assert resolve_entry(merged, "no-such-model") is None

    def test_undated_entry_always_in_force(self, tmp_path: Path) -> None:
        merged = _merged_with([_entry("m", 1.0)], tmp_path)
        resolved = resolve_entry(merged, "m", at=date(1999, 1, 1))
        assert resolved is not None
        assert resolved.layer == "project"

    def test_latest_dated_entry_at_or_before_t_wins(self, tmp_path: Path) -> None:
        merged = _merged_with(
            [
                _entry("m", 1.0, effective_from="2026-01-01"),
                _entry("m", 2.0, effective_from="2026-07-01"),
            ],
            tmp_path,
        )
        early = resolve_entry(merged, "m", at=date(2026, 3, 2))
        late = resolve_entry(merged, "m", at=date(2026, 8, 10))
        boundary = resolve_entry(merged, "m", at=date(2026, 7, 1))
        assert early is not None and late is not None and boundary is not None
        assert early.entry.pricing.input is not None
        assert early.entry.pricing.input.amount == 1.0
        assert late.entry.pricing.input is not None
        assert late.entry.pricing.input.amount == 2.0
        assert boundary.entry.pricing.input is not None
        assert boundary.entry.pricing.input.amount == 2.0  # <= T is eligible

    def test_undated_loses_to_any_eligible_dated_entry(self, tmp_path: Path) -> None:
        merged = _merged_with(
            [
                _entry("m", 9.0),  # undated
                _entry("m", 1.0, effective_from="2026-01-01"),
            ],
            tmp_path,
        )
        resolved = resolve_entry(merged, "m", at=date(2026, 2, 1))
        assert resolved is not None
        assert resolved.entry.pricing.input is not None
        assert resolved.entry.pricing.input.amount == 1.0

    def test_future_dated_ineligible_falls_back_to_undated(self, tmp_path: Path) -> None:
        merged = _merged_with(
            [
                _entry("m", 9.0),  # undated fallback
                _entry("m", 1.0, effective_from="2027-01-01"),
            ],
            tmp_path,
        )
        resolved = resolve_entry(merged, "m", at=date(2026, 2, 1))
        assert resolved is not None
        assert resolved.entry.pricing.input is not None
        assert resolved.entry.pricing.input.amount == 9.0

    def test_only_future_dated_and_no_undated_resolves_none(self, tmp_path: Path) -> None:
        merged = _merged_with(
            [_entry("m", 1.0, effective_from="2027-01-01")], tmp_path
        )
        assert resolve_entry(merged, "m", at=date(2026, 2, 1)) is None


# ---------------------------------------------------------------------------
# Pricing math (per usage type, per unit)
# ---------------------------------------------------------------------------


class TestPricingMath:
    def _entry_obj(self, pricing: dict[str, Any]) -> Any:
        from novafabric.cost.pricing_catalog import PricingEntry

        return PricingEntry.model_validate({"model_id": "m", "pricing": pricing})

    def test_per_1k_math(self) -> None:
        entry = self._entry_obj(
            {"input": {"amount": 0.003, "unit": "per_1k"}, "output": {"amount": 0.015}}
        )
        cost = price_usage(entry, {"input": 2000, "output": 500})
        assert cost == pytest.approx(2.0 * 0.003 + 0.5 * 0.015)

    def test_per_1m_math(self) -> None:
        entry = self._entry_obj(
            {
                "input": {"amount": 0.10, "unit": "per_1m"},
                "output": {"amount": 0.30, "unit": "per_1m"},
            }
        )
        cost = price_usage(entry, {"input": 1_000_000, "output": 500_000})
        assert cost == pytest.approx(0.10 + 0.15)

    def test_per_image_math(self) -> None:
        entry = self._entry_obj({"image": {"amount": 0.04, "unit": "per_image"}})
        assert price_usage(entry, {"image": 3}) == pytest.approx(0.12)

    def test_all_usage_types_summed(self) -> None:
        entry = self._entry_obj(
            {
                "input": {"amount": 3.0, "unit": "per_1m"},
                "output": {"amount": 15.0, "unit": "per_1m"},
                "cached": {"amount": 0.3, "unit": "per_1m"},
                "reasoning": {"amount": 15.0, "unit": "per_1m"},
            }
        )
        usage = {"input": 10_000, "output": 2_000, "cached": 40_000, "reasoning": 6_000}
        expected = (
            (10_000 / 1e6) * 3.0
            + (2_000 / 1e6) * 15.0
            + (40_000 / 1e6) * 0.3
            + (6_000 / 1e6) * 15.0
        )
        assert price_usage(entry, usage) == pytest.approx(expected)

    def test_unpriced_usage_type_contributes_zero(self) -> None:
        entry = self._entry_obj({"input": {"amount": 1.0, "unit": "per_1k"}})
        # reasoning is reported but not priced: contributes 0.0, no error.
        assert price_usage(entry, {"input": 1000, "reasoning": 999_999}) == pytest.approx(1.0)

    def test_zero_negative_and_bogus_counts_ignored(self) -> None:
        entry = self._entry_obj(
            {"input": {"amount": 1.0, "unit": "per_1k"}, "output": {"amount": 1.0}}
        )
        assert price_usage(entry, {"input": 0, "output": -5}) == 0.0
        assert price_usage(entry, {"input": True, "output": "many"}) == 0.0  # type: ignore[dict-item]

    def test_usage_counts_from_block_mapping(self) -> None:
        block = {
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 30,
            "cache_write_tokens": 999,  # not priceable in v0 — skipped
            "reasoning_tokens": 20,
            "audio_input_tokens": 5,
            "audio_output_tokens": 7,
            "image_input_tokens": 2,
            "image_output_tokens": 1,
            "total_tokens": 150,  # never priced
            "extra": {"mystery_tokens": 3},  # never priced
        }
        counts = usage_counts_from_block(block)
        assert counts == {
            "input": 100,
            "output": 50,
            "cached": 30,
            "reasoning": 20,
            "audio": 12,
            "image": 3,
        }
        assert set(counts) <= set(PRICING_USAGE_TYPES)

    def test_usage_counts_rejects_bogus_values(self) -> None:
        assert usage_counts_from_block(
            {"input_tokens": -1, "output_tokens": True, "cached_tokens": "3"}
        ) == {}


# ---------------------------------------------------------------------------
# Record costing: recorded wins, derived is labeled estimated
# ---------------------------------------------------------------------------


class TestRecordCosting:
    def test_recorded_cost_is_never_overwritten(self, tmp_path: Path) -> None:
        # Catalog prices the model at a very different rate...
        merged = _merged_with([_entry("m", 100.0, 100.0)], tmp_path)
        record = {
            "gen_ai.request.model": "m",
            "gen_ai.usage.input_tokens": 1000,
            "gen_ai.usage.output_tokens": 1000,
            "nova.cost": {"currency": "EUR", "amount": 0.42},
        }
        cost = cost_for_model_call_record(record, merged)
        assert cost == {"currency": "EUR", "amount": 0.42, "basis": "recorded"}
        # ...and the record itself was not mutated.
        assert record["nova.cost"] == {"currency": "EUR", "amount": 0.42}

    def test_derived_cost_is_labeled_estimated(self, tmp_path: Path) -> None:
        merged = _merged_with(
            [_entry("mistral-7b-local", 0.1, 0.3, unit="per_1m")], tmp_path
        )
        record = {
            "gen_ai.request.model": "mistral-7b-local",
            "nova.usage": {"input_tokens": 1_000_000, "output_tokens": 500_000},
        }
        cost = cost_for_model_call_record(record, merged)
        assert cost is not None
        assert cost["basis"] == "estimated"
        assert cost["amount"] == pytest.approx(0.10 + 0.15)
        assert cost["currency"] == "USD"
        assert cost["pricing_source_layer"] == "project"
        assert cost["pricing_catalog_digest"] == merged.digest

    def test_legacy_scalars_used_when_no_usage_block(self, tmp_path: Path) -> None:
        merged = _merged_with([_entry("m", 1.0, 2.0)], tmp_path)
        record = {
            "gen_ai.response.model": "m",
            "gen_ai.usage.input_tokens": 1000,
            "gen_ai.usage.output_tokens": 500,
        }
        cost = cost_for_model_call_record(record, merged)
        assert cost is not None
        assert cost["amount"] == pytest.approx(1.0 + 1.0)

    def test_unknown_model_stays_unpriced(self) -> None:
        merged = load_merged_catalog()
        record = {
            "gen_ai.request.model": "no-such-model",
            "gen_ai.usage.input_tokens": 1000,
            "gen_ai.usage.output_tokens": 500,
        }
        assert cost_for_model_call_record(record, merged) is None

    def test_no_usage_evidence_stays_unpriced(self, tmp_path: Path) -> None:
        merged = _merged_with([_entry("m", 1.0)], tmp_path)
        assert cost_for_model_call_record({"gen_ai.request.model": "m"}, merged) is None

    def test_no_model_id_stays_unpriced(self) -> None:
        merged = load_merged_catalog()
        assert (
            cost_for_model_call_record({"gen_ai.usage.input_tokens": 5}, merged) is None
        )

    def test_effective_dating_applies_at_capture_time(self, tmp_path: Path) -> None:
        merged = _merged_with(
            [
                _entry("m", 1.0, effective_from="2026-01-01"),
                _entry("m", 2.0, effective_from="2026-07-01"),
            ],
            tmp_path,
        )
        record = {"gen_ai.request.model": "m", "gen_ai.usage.input_tokens": 1000}
        early = cost_for_model_call_record(record, merged, at=date(2026, 3, 2))
        late = cost_for_model_call_record(record, merged, at=date(2026, 8, 10))
        assert early is not None and late is not None
        assert early["amount"] == pytest.approx(1.0)
        assert late["amount"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Digest (D4)
# ---------------------------------------------------------------------------


class TestDigest:
    def test_digest_is_deterministic(self) -> None:
        first = load_merged_catalog()
        second = load_merged_catalog()
        assert first.digest == second.digest
        assert first.digest.startswith("sha256:")

    def test_digest_changes_when_catalog_changes(self, tmp_path: Path) -> None:
        baseline = load_merged_catalog().digest
        _write_project_catalog(tmp_path, _catalog([_entry("m", 1.0)]))
        changed = load_merged_catalog().digest
        assert changed != baseline
        _write_project_catalog(tmp_path, _catalog([_entry("m", 1.5)]))
        assert load_merged_catalog().digest != changed


# ---------------------------------------------------------------------------
# CostInterceptor integration: absent catalog == today's behavior
# ---------------------------------------------------------------------------


class TestInterceptorIntegration:
    def test_absent_catalog_reproduces_legacy_figures(self) -> None:
        # No catalog file exists in any isolated layer: every PRICE_TABLE
        # model must cost exactly the legacy formula, bit-for-bit.
        for model, (inp, out) in CostInterceptor.PRICE_TABLE.items():
            expected = (1234 / 1000.0) * inp + (567 / 1000.0) * out
            assert CostInterceptor._estimate_cost(model, 1234, 567) == expected

    def test_absent_catalog_unknown_model_is_zero(self) -> None:
        assert CostInterceptor._estimate_cost("mistral-7b-local", 1000, 1000) == 0.0

    def test_project_catalog_prices_self_hosted_model(self, tmp_path: Path) -> None:
        _write_project_catalog(
            tmp_path,
            _catalog([_entry("mistral-7b-local", 0.1, 0.3, unit="per_1m")]),
        )
        cost = CostInterceptor._estimate_cost("mistral-7b-local", 1_000_000, 500_000)
        assert cost == pytest.approx(0.10 + 0.15)

    def test_project_catalog_overrides_builtin_price(self, tmp_path: Path) -> None:
        _write_project_catalog(tmp_path, _catalog([_entry("gpt-4o", 1.0, 1.0)]))
        cost = CostInterceptor._estimate_cost("gpt-4o", 1000, 1000)
        assert cost == pytest.approx(2.0)

    def test_extract_from_openai_response_uses_catalog(self, tmp_path: Path) -> None:
        _write_project_catalog(
            tmp_path,
            _catalog([_entry("mistral-7b-local", 0.1, 0.3, unit="per_1m")]),
        )
        response = {"usage": {"prompt_tokens": 1_000_000, "completion_tokens": 500_000}}
        facet = CostInterceptor.extract_from_openai_response(
            response, "mistral-7b-local"
        )
        assert facet.cost_usd_estimated == pytest.approx(0.25)

    def test_catalog_cached_price_is_added_when_defined(self, tmp_path: Path) -> None:
        _write_project_catalog(
            tmp_path,
            _catalog(
                [
                    {
                        "model_id": "m",
                        "pricing": {
                            "input": {"amount": 1.0, "unit": "per_1k"},
                            "output": {"amount": 2.0, "unit": "per_1k"},
                            "cached": {"amount": 0.1, "unit": "per_1k"},
                        },
                    }
                ]
            ),
        )
        cost = CostInterceptor._estimate_cost("m", 1000, 1000, cached_tokens=1000)
        assert cost == pytest.approx(1.0 + 2.0 + 0.1)
