package novafabric.examples.production_export_test

import data.novafabric.examples.production_export

_res(env, proof, skips) := {"resource": {
    "kind": "capsule",
    "ref": "run-1",
    "deployment_environment": env,
    "redaction_proof_present": proof,
    "unsafe_skips": skips,
}}

test_production_clean_allowed if {
    production_export.allow with input as _res("production", true, 0)
}

test_production_with_unsafe_skips_denied if {
    not production_export.allow with input as _res("production", true, 2)
}

test_staging_with_unsafe_skips_allowed if {
    production_export.allow with input as _res("staging", true, 2)
}

test_unrecorded_environment_treated_as_non_production if {
    production_export.allow with input as _res(null, true, 2)
}

test_missing_redaction_proof_denied_everywhere if {
    not production_export.allow with input as _res("staging", false, 0)
    not production_export.allow with input as _res("production", false, 0)
}
