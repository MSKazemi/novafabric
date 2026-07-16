# `novafabric.policy`

The **policy engine** abstraction: `get_policy_engine()` returns an `OpaEngine`
when the `opa` binary is on PATH, otherwise a `NoopEngine` (allow-all, with a
warning). Holds the engine, models, and OPA/Rego integration
(`_engine.py`, `_models.py`, `_opa_engine.py`, `_noop_engine.py`), plus
`_budget.py` — `budget_block_from_capsule()`, the recorded cost/energy/token
rollup that feeds `PolicyResource.budget` for the ADR-0136 budget gate
(`policies/novafabric/defaults/budget_gate.rego`).

**Not to be confused with [`novafabric.policies`](../policies/) — the policy
*data* (capture-level definitions).** `policy` = the engine that evaluates;
`policies` = the rules/levels it evaluates.
