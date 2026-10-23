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

`_opa_sandbox.py` — every `opa` subprocess NovaFabric starts (`OpaEngine`, `nova policy
test`, `nova serve`'s `/api/policy/check` and `/api/policy/test`) goes through `prepare()` +
`run_opa()`. The process gets **no inherited environment** (only a fixed `PATH`) and runs with
`--capabilities` pointing at a file derived from the installed binary's
`opa capabilities --current`, minus `opa.runtime`, `http.send` and `net.lookup_ip_addr`, with
`allow_net: []`. Rego that names a removed built-in fails to compile, so the decision is a deny.
If the capabilities cannot be derived, `OpaSandboxError` is raised and nothing is evaluated
(fail closed). Who may submit Rego at all is the caller's decision: `nova serve` allows a
request's own Rego (`policy_source`, `bundle_path`) only to `admin`.

**Not to be confused with [`novafabric.policies`](../policies/) — the policy
*data* (capture-level definitions).** `policy` = the engine that evaluates;
`policies` = the rules/levels it evaluates.
