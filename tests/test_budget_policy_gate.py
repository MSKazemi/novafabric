"""Tests for the ADR-0136 cost/energy budget gate.

Covers the three shipped pieces:

1. ``PolicyResource.budget`` — additive optional field (mirrors the v0.9
   ``regression_report`` precedent).
2. ``budget_block_from_capsule()`` — the recorded rollup assembler
   (ADR-0136 P1): cost from ``nova.cost`` (ADR-0066), energy kWh from
   ``measured_joules`` (ADR-0093), tokens from ``gen_ai.usage.*``.
   Record-only honesty: absent evidence is ``None`` + ``measured=False``,
   never a fabricated zero.
3. ``budget_gate.rego`` — the reference Rego policy (deny over-budget,
   allow under-budget, allow-with-note on absent data), exercised via
   ``opa`` when the binary is available (skipped otherwise; the Rego
   unit suite also runs via ``nova policy test``).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from novafabric.policy import PolicyInput, PolicyResource, PolicySubject, budget_block_from_capsule

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, records: list[Any]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _model_call(
    *,
    cost: dict[str, Any] | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {"model_call_id": "01J8AY7M7QM4YZ2K7N9DPBYK2W"}
    if cost is not None:
        record["nova.cost"] = cost
    if input_tokens is not None:
        record["gen_ai.usage.input_tokens"] = input_tokens
    if output_tokens is not None:
        record["gen_ai.usage.output_tokens"] = output_tokens
    return record


def _energy_receipt(measured_joules: float | None) -> dict[str, Any]:
    return {"receipt_id": "01J8AY7M7QM4YZ2K7N9DPBYK2X", "measured_joules": measured_joules}


# ---------------------------------------------------------------------------
# PolicyResource additive compatibility (mirrors test_regression_policy_gate)
# ---------------------------------------------------------------------------


def test_policy_resource_accepts_budget_block() -> None:
    """PolicyResource accepts a recorded budget rollup in the budget field."""
    resource = PolicyResource(
        kind="asset",
        ref="my-agent@v3",
        budget={
            "total_cost": {"currency": "USD", "amount": 4.12},
            "cost_per_run": {"currency": "USD", "amount": 4.12},
            "energy_kwh": 0.0091,
            "tokens": {"total_tokens": 512340},
            "measured": {"cost": True, "energy": True, "tokens": True},
        },
    )
    assert resource.budget is not None
    assert resource.budget["total_cost"]["amount"] == 4.12
    assert resource.budget["measured"]["energy"] is True


def test_policy_resource_budget_defaults_to_none() -> None:
    """PolicyResource.budget is None when not provided (additive, optional)."""
    resource = PolicyResource(kind="asset", ref="asset-001")
    assert resource.budget is None


def test_policy_input_roundtrip_with_budget() -> None:
    """PolicyInput with a budget block serialises and re-validates."""
    inp = PolicyInput(
        action="promote",
        subject=PolicySubject(user="alice", roles=["admin"]),
        resource=PolicyResource(
            kind="asset",
            ref="my-agent@v3",
            budget={
                "total_cost": {"currency": "USD", "amount": 0.0},
                "cost_per_run": {"currency": "USD", "amount": 0.0},
                "energy_kwh": None,
                "tokens": None,
                "measured": {"cost": True, "energy": False, "tokens": False},
            },
        ),
        context={"deployment_environment": "production"},
    )
    restored = PolicyInput.model_validate_json(inp.model_dump_json())
    assert restored.resource.budget is not None
    assert restored.resource.budget["measured"]["cost"] is True
    assert restored.resource.budget["energy_kwh"] is None


# ---------------------------------------------------------------------------
# budget_block_from_capsule — recorded rollup assembly (ADR-0136 P1)
# ---------------------------------------------------------------------------


def test_rollup_fully_measured_capsule(tmp_path: Path) -> None:
    """Cost, energy, and tokens roll up from recorded evidence."""
    _write_jsonl(
        tmp_path / "model-calls.jsonl",
        [
            _model_call(
                cost={"currency": "USD", "amount": 1.5}, input_tokens=100, output_tokens=50
            ),
            _model_call(
                cost={"currency": "USD", "amount": 2.5}, input_tokens=200, output_tokens=100
            ),
            _model_call(),  # a call with no cost block and no usage contributes nothing
        ],
    )
    _write_jsonl(
        tmp_path / "energy-receipts.jsonl",
        [_energy_receipt(1800.0), _energy_receipt(1800.0)],
    )
    block = budget_block_from_capsule(tmp_path)
    assert block["total_cost"] == {"currency": "USD", "amount": 4.0}
    assert block["cost_per_run"] == {"currency": "USD", "amount": 4.0}
    assert block["energy_kwh"] == pytest.approx(3600.0 / 3.6e6)
    assert block["tokens"] == {"input_tokens": 300, "output_tokens": 150, "total_tokens": 450}
    assert block["measured"] == {"cost": True, "energy": True, "tokens": True}


def test_rollup_empty_capsule_is_unmeasured(tmp_path: Path) -> None:
    """No recorded files => every dimension is None and unmeasured — never zero."""
    block = budget_block_from_capsule(tmp_path)
    assert block["total_cost"] is None
    assert block["cost_per_run"] is None
    assert block["energy_kwh"] is None
    assert block["tokens"] is None
    assert block["measured"] == {"cost": False, "energy": False, "tokens": False}


def test_rollup_measured_zero_cost_is_distinct_from_unmeasured(tmp_path: Path) -> None:
    """A recorded 0.00 cost (local model) is measured evidence, not 'no data'."""
    _write_jsonl(
        tmp_path / "model-calls.jsonl",
        [_model_call(cost={"currency": "USD", "amount": 0.0})],
    )
    block = budget_block_from_capsule(tmp_path)
    assert block["total_cost"] == {"currency": "USD", "amount": 0.0}
    assert block["measured"]["cost"] is True


def test_rollup_null_cost_amount_is_unmeasured(tmp_path: Path) -> None:
    """nova.cost with amount=null (provider exposes no rates) is not evidence."""
    _write_jsonl(
        tmp_path / "model-calls.jsonl",
        [_model_call(cost={"currency": "USD", "amount": None}, input_tokens=10, output_tokens=5)],
    )
    block = budget_block_from_capsule(tmp_path)
    assert block["total_cost"] is None
    assert block["measured"]["cost"] is False
    # tokens were still recorded on the same call
    assert block["tokens"] == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    assert block["measured"]["tokens"] is True


def test_rollup_mixed_currencies_never_silently_summed(tmp_path: Path) -> None:
    """Cross-currency rollup is out of scope for v0 — reported as unmeasured."""
    _write_jsonl(
        tmp_path / "model-calls.jsonl",
        [
            _model_call(cost={"currency": "USD", "amount": 1.0}),
            _model_call(cost={"currency": "EUR", "amount": 1.0}),
        ],
    )
    block = budget_block_from_capsule(tmp_path)
    assert block["total_cost"] is None
    assert block["cost_per_run"] is None
    assert block["measured"]["cost"] is False


def test_rollup_all_null_joules_is_unmeasured(tmp_path: Path) -> None:
    """Receipts with measured_joules=null (ADR-0093) yield no energy evidence."""
    _write_jsonl(
        tmp_path / "energy-receipts.jsonl",
        [_energy_receipt(None), _energy_receipt(None)],
    )
    block = budget_block_from_capsule(tmp_path)
    assert block["energy_kwh"] is None
    assert block["measured"]["energy"] is False


def test_rollup_partial_energy_sums_only_measured_receipts(tmp_path: Path) -> None:
    """Null receipts contribute nothing; measured receipts still roll up."""
    _write_jsonl(
        tmp_path / "energy-receipts.jsonl",
        [_energy_receipt(None), _energy_receipt(7200.0)],
    )
    block = budget_block_from_capsule(tmp_path)
    assert block["energy_kwh"] == pytest.approx(7200.0 / 3.6e6)
    assert block["measured"]["energy"] is True


def test_rollup_skips_malformed_and_blank_lines(tmp_path: Path) -> None:
    """Unparseable lines carry no usable recorded evidence and are skipped."""
    (tmp_path / "model-calls.jsonl").write_text(
        "not json\n\n"
        + json.dumps(_model_call(cost={"currency": "USD", "amount": 2.0}))
        + "\n[1, 2]\n",
        encoding="utf-8",
    )
    block = budget_block_from_capsule(tmp_path)
    assert block["total_cost"] == {"currency": "USD", "amount": 2.0}


def test_rollup_cost_per_run_is_independent_copy(tmp_path: Path) -> None:
    """cost_per_run equals total_cost for a single-run capsule but never aliases it."""
    _write_jsonl(
        tmp_path / "model-calls.jsonl",
        [_model_call(cost={"currency": "USD", "amount": 3.0})],
    )
    block = budget_block_from_capsule(tmp_path)
    assert block["cost_per_run"] == block["total_cost"]
    assert block["cost_per_run"] is not block["total_cost"]


def test_rollup_feeds_policy_input(tmp_path: Path) -> None:
    """The assembled block is directly usable as PolicyResource.budget."""
    _write_jsonl(
        tmp_path / "model-calls.jsonl",
        [_model_call(cost={"currency": "USD", "amount": 1.0}, input_tokens=5, output_tokens=5)],
    )
    inp = PolicyInput(
        action="promote",
        subject=PolicySubject(user="alice"),
        resource=PolicyResource(
            kind="asset", ref="my-agent@v3", budget=budget_block_from_capsule(tmp_path)
        ),
    )
    restored = PolicyInput.model_validate_json(inp.model_dump_json())
    assert restored.resource.budget is not None
    assert restored.resource.budget["total_cost"]["amount"] == 1.0


# ---------------------------------------------------------------------------
# budget_gate.rego — Rego unit suite (requires the opa binary; skipped
# otherwise — the same suite runs via `nova policy test`)
# ---------------------------------------------------------------------------

_OPA_MISSING = shutil.which("opa") is None
_BUNDLE = Path(__file__).parent.parent / "src" / "novafabric" / "policies"


def test_budget_gate_rego_files_exist() -> None:
    """The reference policy and its Rego unit tests ship in the defaults bundle."""
    defaults = _BUNDLE / "novafabric" / "defaults"
    assert (defaults / "budget_gate.rego").is_file()
    assert (defaults / "budget_gate_test.rego").is_file()


@pytest.mark.skipif(_OPA_MISSING, reason="opa binary not on PATH")
def test_budget_gate_rego_unit_suite_passes() -> None:
    """`opa test` on the built-in bundle passes (includes budget_gate_test.rego)."""
    result = subprocess.run(
        ["opa", "test", str(_BUNDLE), "--verbose"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
