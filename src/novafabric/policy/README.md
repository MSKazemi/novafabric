# `novafabric.policy`

The **policy engine** abstraction: `get_policy_engine()` returns an `OpaEngine`
when the `opa` binary is on PATH, otherwise a `NoopEngine` (allow-all, with a
warning). Holds the engine, models, and OPA/Rego integration
(`_engine.py`, `_models.py`, `_opa_engine.py`, `_noop_engine.py`), plus
`_budget.py` — `budget_block_from_capsule()`, the recorded cost/energy/token
rollup that feeds `PolicyResource.budget` for the ADR-0136 budget gate
(`policies/novafabric/defaults/budget_gate.rego`).

`_environment.py` — `deployment_environment_from_capsule()`, the capsule's recorded
ADR-0126 deployment environment (verbatim, `None` when absent) that feeds
`PolicyResource.deployment_environment` / `input.resource.deployment_environment` for
environment-conditioned gates (experimental; wired into the evidence-export gate).

**Not to be confused with [`novafabric.policies`](../policies/) — the policy
*data* (capture-level definitions).** `policy` = the engine that evaluates;
`policies` = the rules/levels it evaluates.
