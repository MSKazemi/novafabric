"""Recorded budget rollup for the cost/energy budget gate (ADR-0136 P1).

Assembles the ``resource.budget`` block of the promotion policy input from a
captured capsule's **already-recorded** evidence — never an estimate, never a
running workload:

- **cost** — ``nova.cost`` blocks on ``model-calls.jsonl`` records (ADR-0066);
- **energy** — ``measured_joules`` on ``energy-receipts.jsonl`` records
  (ADR-0093), rolled up to kWh;
- **tokens** — ``gen_ai.usage.input_tokens`` / ``gen_ai.usage.output_tokens``
  on ``model-calls.jsonl`` records.

Record-only honesty (ADR-0136 D3): a dimension with no recorded evidence is
``None`` with ``measured.<dim> = False`` — **never a fabricated zero**. A
recorded ``0.00`` cost (e.g. a local Ollama run) is measured evidence and is
kept as such. Mixed recorded currencies are never silently summed
(cross-currency normalization is out of scope for v0): the cost dimension is
then reported unmeasured.

The returned block is the spec shape defined in ``design/spec/budget-gate-v0.md``
and is consumed by the reference policy
``policies/novafabric/defaults/budget_gate.rego``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

MODEL_CALLS_FILE = "model-calls.jsonl"
ENERGY_RECEIPTS_FILE = "energy-receipts.jsonl"

_JOULES_PER_KWH = 3.6e6


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield JSON objects from a JSONL file, skipping unusable lines.

    A blank or unparseable line carries no recorded evidence; skipping it can
    only make the rollup *smaller*, never fabricate a quantity.
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def _number(value: Any) -> float | None:
    """Return *value* as a float when it is a real number (bools excluded)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _rollup_cost(model_calls: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, bool]:
    """Sum recorded ``nova.cost`` amounts; ``(None, False)`` without evidence."""
    amounts: list[float] = []
    currencies: set[str] = set()
    for record in model_calls:
        cost = record.get("nova.cost")
        if not isinstance(cost, dict):
            continue
        amount = _number(cost.get("amount"))
        currency = cost.get("currency")
        if amount is None or not isinstance(currency, str) or not currency:
            continue  # amount=null ⇒ provider exposed no rates ⇒ not evidence
        amounts.append(amount)
        currencies.add(currency)
    if not amounts:
        return None, False
    if len(currencies) > 1:
        # Cross-currency rollup is out of scope for v0 (ADR-0136): never
        # silently sum across currencies — report the dimension unmeasured.
        return None, False
    return {"currency": next(iter(currencies)), "amount": sum(amounts)}, True


def _rollup_energy(receipts: list[dict[str, Any]]) -> tuple[float | None, bool]:
    """Sum non-null ``measured_joules`` to kWh; ``(None, False)`` without evidence."""
    joules = [
        value for r in receipts if (value := _number(r.get("measured_joules"))) is not None
    ]
    if not joules:
        return None, False
    return sum(joules) / _JOULES_PER_KWH, True


def _rollup_tokens(model_calls: list[dict[str, Any]]) -> tuple[dict[str, int] | None, bool]:
    """Sum recorded ``gen_ai.usage.*`` token counts; ``(None, False)`` without evidence."""
    input_tokens = 0
    output_tokens = 0
    measured = False
    for record in model_calls:
        for key, bucket in (
            ("gen_ai.usage.input_tokens", "input"),
            ("gen_ai.usage.output_tokens", "output"),
        ):
            value = record.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                measured = True
                if bucket == "input":
                    input_tokens += value
                else:
                    output_tokens += value
    if not measured:
        return None, False
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }, True


def budget_block_from_capsule(capsule_dir: Path) -> dict[str, Any]:
    """Assemble the recorded ``budget`` rollup block for the policy input.

    Reads a captured capsule directory and returns the ADR-0136 ``budget``
    block (see ``design/spec/budget-gate-v0.md``)::

        {
          "total_cost":   {"currency": ..., "amount": ...} | None,
          "cost_per_run": {"currency": ..., "amount": ...} | None,
          "energy_kwh":   float | None,
          "tokens":       {"input_tokens", "output_tokens", "total_tokens"} | None,
          "measured":     {"cost": bool, "energy": bool, "tokens": bool},
        }

    For a single-run capsule ``cost_per_run`` equals ``total_cost`` (per the
    v0 spec); the value is an independent copy, never an alias.
    """
    model_calls = list(_iter_jsonl(capsule_dir / MODEL_CALLS_FILE))
    receipts = list(_iter_jsonl(capsule_dir / ENERGY_RECEIPTS_FILE))

    total_cost, cost_measured = _rollup_cost(model_calls)
    energy_kwh, energy_measured = _rollup_energy(receipts)
    tokens, tokens_measured = _rollup_tokens(model_calls)

    return {
        "total_cost": total_cost,
        "cost_per_run": dict(total_cost) if total_cost is not None else None,
        "energy_kwh": energy_kwh,
        "tokens": tokens,
        "measured": {
            "cost": cost_measured,
            "energy": energy_measured,
            "tokens": tokens_measured,
        },
    }
