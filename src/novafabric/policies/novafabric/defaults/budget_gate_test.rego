# Rego unit tests for the ADR-0136 cost/energy budget gate.
# Run via `nova policy test` (opa test).

package novafabric.defaults.budget_gate_test

import data.novafabric.defaults.budget_gate

measured_budget := {
    "total_cost": {"currency": "USD", "amount": 4.12},
    "cost_per_run": {"currency": "USD", "amount": 0.34},
    "energy_kwh": 0.0091,
    "tokens": {"total_tokens": 512340, "input_tokens": 410220, "output_tokens": 102120},
    "measured": {"cost": true, "energy": true, "tokens": true},
}

full_ceilings := {
    "total_cost": {"currency": "USD", "amount": 5.00},
    "cost_per_run": {"currency": "USD", "amount": 0.50},
    "energy_kwh": 0.02,
    "tokens": {"field": "total_tokens", "limit": 1000000},
}

test_allow_under_budget if {
    budget_gate.allow with input as {
        "action": "promote",
        "resource": {"kind": "asset", "ref": "my-agent@v3", "budget": measured_budget},
        "context": {"budget_ceilings": full_ceilings},
    }
}

test_reason_under_budget if {
    budget_gate.reason == "budget gate: recorded quantities under all declared ceilings" with input as {
        "action": "promote",
        "resource": {"kind": "asset", "ref": "my-agent@v3", "budget": measured_budget},
        "context": {"budget_ceilings": full_ceilings},
    }
}

test_deny_over_total_cost if {
    not budget_gate.allow with input as {
        "action": "promote",
        "resource": {"kind": "asset", "ref": "my-agent@v4", "budget": object.union(measured_budget, {"total_cost": {"currency": "USD", "amount": 7.80}})},
        "context": {"budget_ceilings": full_ceilings},
    }
}

test_reason_over_total_cost_names_quantity_and_ceiling if {
    contains(budget_gate.reason, "total_cost 7.8 USD exceeds ceiling 5 USD") with input as {
        "action": "promote",
        "resource": {"kind": "asset", "ref": "my-agent@v4", "budget": object.union(measured_budget, {"total_cost": {"currency": "USD", "amount": 7.80}})},
        "context": {"budget_ceilings": full_ceilings},
    }
}

test_deny_over_cost_per_run if {
    not budget_gate.allow with input as {
        "action": "promote",
        "resource": {"kind": "asset", "ref": "my-agent@v4", "budget": object.union(measured_budget, {"cost_per_run": {"currency": "USD", "amount": 0.75}})},
        "context": {"budget_ceilings": full_ceilings},
    }
}

test_deny_over_energy if {
    not budget_gate.allow with input as {
        "action": "promote",
        "resource": {"kind": "asset", "ref": "my-agent@v4", "budget": object.union(measured_budget, {"energy_kwh": 0.05})},
        "context": {"budget_ceilings": full_ceilings},
    }
}

test_deny_over_tokens if {
    not budget_gate.allow with input as {
        "action": "promote",
        "resource": {"kind": "asset", "ref": "my-agent@v4", "budget": object.union(measured_budget, {"tokens": {"total_tokens": 2000000}})},
        "context": {"budget_ceilings": full_ceilings},
    }
}

test_deny_currency_mismatch_never_silently_converted if {
    not budget_gate.allow with input as {
        "action": "promote",
        "resource": {"kind": "asset", "ref": "my-agent@v4", "budget": object.union(measured_budget, {"total_cost": {"currency": "EUR", "amount": 1.00}})},
        "context": {"budget_ceilings": full_ceilings},
    }
}

test_allow_measured_zero_cost if {
    budget_gate.allow with input as {
        "action": "promote",
        "resource": {"kind": "asset", "ref": "local-agent@v1", "budget": {
            "total_cost": {"currency": "USD", "amount": 0.0},
            "cost_per_run": {"currency": "USD", "amount": 0.0},
            "energy_kwh": null,
            "tokens": null,
            "measured": {"cost": true, "energy": false, "tokens": false},
        }},
        "context": {"budget_ceilings": {"total_cost": {"currency": "USD", "amount": 5.00}}},
    }
}

test_allow_no_ceilings_with_no_data_note if {
    budget_gate.allow with input as {
        "action": "promote",
        "resource": {"kind": "asset", "ref": "my-agent@v3", "budget": measured_budget},
        "context": {},
    }
}

test_reason_no_ceilings_is_no_data_note if {
    contains(budget_gate.reason, "no data") with input as {
        "action": "promote",
        "resource": {"kind": "asset", "ref": "my-agent@v3", "budget": measured_budget},
        "context": {},
    }
}

test_allow_no_evidence_skip_unmeasured if {
    budget_gate.allow with input as {
        "action": "promote",
        "resource": {"kind": "asset", "ref": "my-agent@v3"},
        "context": {"budget_ceilings": full_ceilings},
    }
}

test_reason_no_evidence_not_certified if {
    contains(budget_gate.reason, "under-budget not certified") with input as {
        "action": "promote",
        "resource": {"kind": "asset", "ref": "my-agent@v3"},
        "context": {"budget_ceilings": full_ceilings},
    }
}

test_allow_partial_evidence_skip_unmeasured_notes_missing_dimension if {
    contains(budget_gate.reason, "no data for energy_kwh") with input as {
        "action": "promote",
        "resource": {"kind": "asset", "ref": "my-agent@v3", "budget": object.union(measured_budget, {"energy_kwh": null, "measured": {"cost": true, "energy": false, "tokens": true}})},
        "context": {"budget_ceilings": full_ceilings},
    }
}

test_deny_unmeasured_require_measured if {
    not budget_gate.allow with input as {
        "action": "promote",
        "resource": {"kind": "asset", "ref": "my-agent@v3", "budget": object.union(measured_budget, {"energy_kwh": null, "measured": {"cost": true, "energy": false, "tokens": true}})},
        "context": {"budget_ceilings": full_ceilings, "missing_evidence": "require_measured"},
    }
}

test_reason_unmeasured_require_measured_is_blocked_unmeasured if {
    contains(budget_gate.reason, "blocked_unmeasured") with input as {
        "action": "promote",
        "resource": {"kind": "asset", "ref": "my-agent@v3"},
        "context": {"budget_ceilings": full_ceilings, "missing_evidence": "require_measured"},
    }
}
