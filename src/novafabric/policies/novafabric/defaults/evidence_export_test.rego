package novafabric.defaults.evidence_export_test

import data.novafabric.defaults.evidence_export

test_allow_clean if {
    evidence_export.allow with input as {
        "resource": {"redaction_proof_present": true, "unsafe_skips": 0},
    }
}

test_deny_missing_proof if {
    not evidence_export.allow with input as {
        "resource": {"redaction_proof_present": false, "unsafe_skips": 0},
    }
}
