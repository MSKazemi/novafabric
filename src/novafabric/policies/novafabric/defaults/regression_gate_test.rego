package novafabric.defaults.regression_gate_test

import data.novafabric.defaults.regression_gate

test_allow_no_regression if {
    regression_gate.allow with input as {
        "action": "promote",
        "resource": {
            "regression_report": {
                "regression_detected": false,
                "suite_id": "gaia-v1"
            }
        }
    }
}

test_deny_regression_detected if {
    not regression_gate.allow with input as {
        "action": "promote",
        "resource": {
            "regression_report": {
                "regression_detected": true,
                "suite_id": "gaia-v1"
            }
        }
    }
}

test_deny_missing_report if {
    not regression_gate.allow with input as {
        "action": "promote",
        "resource": {}
    }
}

test_reason_regression_detected if {
    regression_gate.reason == "regression detected in eval comparison" with input as {
        "action": "promote",
        "resource": {
            "regression_report": {
                "regression_detected": true
            }
        }
    }
}

test_reason_missing_report if {
    regression_gate.reason == "regression_report not present in policy input" with input as {
        "action": "promote",
        "resource": {}
    }
}
