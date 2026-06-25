package novafabric.defaults.dataset_license_test

import data.novafabric.defaults.dataset_license

test_allow_no_expiry if {
    dataset_license.allow with input as {
        "resource": {"dataset_expires_at": null},
        "context": {"timestamp": "2026-05-10T00:00:00Z"},
    }
}

test_deny_expired if {
    not dataset_license.allow with input as {
        "resource": {"dataset_expires_at": "2026-01-01T00:00:00Z"},
        "context": {"timestamp": "2026-05-10T00:00:00Z"},
    }
}
