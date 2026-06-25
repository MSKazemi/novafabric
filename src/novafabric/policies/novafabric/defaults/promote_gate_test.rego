package novafabric.defaults.promote_gate_test

import data.novafabric.defaults.promote_gate

test_allow_high_score if {
    promote_gate.allow with input as {
        "action": "promote",
        "resource": {"eval_score": 0.95, "unsafe_skips": 0},
    }
}

test_deny_low_score if {
    not promote_gate.allow with input as {
        "action": "promote",
        "resource": {"eval_score": 0.85, "unsafe_skips": 0},
    }
}

test_deny_unsafe_skips if {
    not promote_gate.allow with input as {
        "action": "promote",
        "resource": {"eval_score": 0.95, "unsafe_skips": 2},
    }
}

test_allow_agent_high_score if {
    promote_gate.allow with input as {
        "action": "promote",
        "resource": {"asset_type": "agent", "eval_score": 0.95, "unsafe_skips": 0},
    }
}

test_deny_agent_low_score if {
    not promote_gate.allow with input as {
        "action": "promote",
        "resource": {"asset_type": "agent", "eval_score": 0.85, "unsafe_skips": 0},
    }
}

test_deny_agent_no_eval if {
    not promote_gate.allow with input as {
        "action": "promote",
        "resource": {"asset_type": "agent", "eval_score": null, "unsafe_skips": 0},
    }
}

test_allow_tool_without_eval if {
    promote_gate.allow with input as {
        "action": "promote",
        "resource": {"asset_type": "tool", "eval_score": null, "unsafe_skips": 0},
    }
}

test_allow_dataset_without_eval if {
    promote_gate.allow with input as {
        "action": "promote",
        "resource": {"asset_type": "dataset", "eval_score": null, "unsafe_skips": 0},
    }
}

test_deny_tool_unsafe_skips if {
    not promote_gate.allow with input as {
        "action": "promote",
        "resource": {"asset_type": "tool", "eval_score": null, "unsafe_skips": 1},
    }
}

test_reason_agent_low_score if {
    promote_gate.reason == "eval score below threshold" with input as {
        "action": "promote",
        "resource": {"asset_type": "agent", "eval_score": 0.5, "unsafe_skips": 0},
    }
}

test_reason_unsafe_skips if {
    promote_gate.reason == "unsafe_skips present" with input as {
        "action": "promote",
        "resource": {"asset_type": "tool", "eval_score": null, "unsafe_skips": 3},
    }
}
