# Cost/energy budget promotion gate (ADR-0136) — reference policy.
#
# Compares the capsule's ALREADY-RECORDED cost/energy/token rollup
# (input.resource.budget — assembled by novafabric.policy.budget_block_from_capsule,
# spec: the private design/spec/budget-gate-v0.md) against declared ceilings
# (input.context.budget_ceilings). A promotion gate, not a live alert:
# it acts on sealed evidence at promote time and never watches running spend.
#
# Record-only honesty (ADR-0136 D3):
#   - Absent ceilings and/or absent recorded evidence => the gate PASSES with
#     an explicit "no data" reason. Missing evidence is never treated as zero.
#   - input.context.missing_evidence == "require_measured" fail-closes: a
#     declared ceiling whose evidence is absent cannot be certified
#     under-budget and blocks. Default is "skip_unmeasured".
#
# Ceilings shape (all optional):
#   {
#     "total_cost":   {"currency": "USD", "amount": 5.00},
#     "cost_per_run": {"currency": "USD", "amount": 0.50},
#     "energy_kwh":   0.02,
#     "tokens":       {"field": "total_tokens", "limit": 1000000}
#   }

package novafabric.defaults.budget_gate

default allow := true

default reason := "budget gate: no data — no budget ceiling declared and/or no recorded budget evidence; gate does not apply (record-only honesty, ADR-0136)"

default mode := "skip_unmeasured"

mode := input.context.missing_evidence

ceilings := input.context.budget_ceilings

budget := input.resource.budget

# --- over-budget violations (measured evidence vs declared ceiling) ---------

violations contains msg if {
    c := ceilings.total_cost
    budget.measured.cost == true
    r := budget.total_cost
    r.currency == c.currency
    r.amount > c.amount
    msg := sprintf("total_cost %v %v exceeds ceiling %v %v", [r.amount, r.currency, c.amount, c.currency])
}

violations contains msg if {
    c := ceilings.cost_per_run
    budget.measured.cost == true
    r := budget.cost_per_run
    r.currency == c.currency
    r.amount > c.amount
    msg := sprintf("cost_per_run %v %v exceeds ceiling %v %v", [r.amount, r.currency, c.amount, c.currency])
}

violations contains msg if {
    c := ceilings.energy_kwh
    budget.measured.energy == true
    budget.energy_kwh > c
    msg := sprintf("energy_kwh %v exceeds ceiling %v", [budget.energy_kwh, c])
}

violations contains msg if {
    c := ceilings.tokens
    budget.measured.tokens == true
    field := object.get(c, "field", "total_tokens")
    recorded := budget.tokens[field]
    recorded > c.limit
    msg := sprintf("tokens.%v %v exceeds ceiling %v", [field, recorded, c.limit])
}

# --- currency mismatch: a policy-authoring error, never silently converted --

violations contains msg if {
    c := ceilings.total_cost
    budget.measured.cost == true
    r := budget.total_cost
    r.currency != c.currency
    msg := sprintf("total_cost currency mismatch: recorded %v vs ceiling %v (ADR-0136 v0 requires same-currency; never silently converted)", [r.currency, c.currency])
}

violations contains msg if {
    c := ceilings.cost_per_run
    budget.measured.cost == true
    r := budget.cost_per_run
    r.currency != c.currency
    msg := sprintf("cost_per_run currency mismatch: recorded %v vs ceiling %v (ADR-0136 v0 requires same-currency; never silently converted)", [r.currency, c.currency])
}

# --- require_measured: declared ceiling without evidence fail-closes --------

violations contains msg if {
    mode == "require_measured"
    ceilings.total_cost
    not budget.measured.cost == true
    msg := "total_cost ceiling declared but cost is unmeasured (require_measured => blocked_unmeasured)"
}

violations contains msg if {
    mode == "require_measured"
    ceilings.cost_per_run
    not budget.measured.cost == true
    msg := "cost_per_run ceiling declared but cost is unmeasured (require_measured => blocked_unmeasured)"
}

violations contains msg if {
    mode == "require_measured"
    ceilings.energy_kwh
    not budget.measured.energy == true
    msg := "energy_kwh ceiling declared but energy is unmeasured (require_measured => blocked_unmeasured)"
}

violations contains msg if {
    mode == "require_measured"
    ceilings.tokens
    not budget.measured.tokens == true
    msg := "tokens ceiling declared but token usage is unmeasured (require_measured => blocked_unmeasured)"
}

# --- bookkeeping: what was actually compared vs skipped for lack of data ----

checked contains "total_cost" if {
    ceilings.total_cost
    budget.measured.cost == true
}

checked contains "cost_per_run" if {
    ceilings.cost_per_run
    budget.measured.cost == true
}

checked contains "energy_kwh" if {
    ceilings.energy_kwh
    budget.measured.energy == true
}

checked contains "tokens" if {
    ceilings.tokens
    budget.measured.tokens == true
}

skipped contains "total_cost" if {
    mode == "skip_unmeasured"
    ceilings.total_cost
    not budget.measured.cost == true
}

skipped contains "cost_per_run" if {
    mode == "skip_unmeasured"
    ceilings.cost_per_run
    not budget.measured.cost == true
}

skipped contains "energy_kwh" if {
    mode == "skip_unmeasured"
    ceilings.energy_kwh
    not budget.measured.energy == true
}

skipped contains "tokens" if {
    mode == "skip_unmeasured"
    ceilings.tokens
    not budget.measured.tokens == true
}

# --- verdict -----------------------------------------------------------------

allow := false if count(violations) > 0

reason := sprintf("budget gate: denied — %s", [concat("; ", sort(violations))]) if {
    count(violations) > 0
}

reason := "budget gate: recorded quantities under all declared ceilings" if {
    count(violations) == 0
    count(checked) > 0
    count(skipped) == 0
}

reason := sprintf("budget gate: under declared ceilings for measured dimensions; no data for %s (skip_unmeasured — under-budget not certified for those)", [concat(", ", sort(skipped))]) if {
    count(violations) == 0
    count(checked) > 0
    count(skipped) > 0
}

reason := sprintf("budget gate: no recorded evidence for declared ceiling(s) %s — gate passes with no-data note (skip_unmeasured); under-budget not certified", [concat(", ", sort(skipped))]) if {
    count(violations) == 0
    count(checked) == 0
    count(skipped) > 0
}
