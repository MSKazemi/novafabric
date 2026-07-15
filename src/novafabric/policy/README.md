# `novafabric.policy`

The **policy engine** abstraction: `get_policy_engine()` returns an `OpaEngine`
when the `opa` binary is on PATH, otherwise a `NoopEngine` (allow-all, with a
warning). Holds the engine, models, and OPA/Rego integration
(`_engine.py`, `_models.py`, `_opa_engine.py`, `_noop_engine.py`).

**Not to be confused with [`novafabric.policies`](../policies/) — the policy
*data* (capture-level definitions).** `policy` = the engine that evaluates;
`policies` = the rules/levels it evaluates.
