package novafabric.defaults.promote_direct_test

import data.novafabric.defaults.promote_direct

test_production_denied_by_default if {
	not promote_direct.allow with input as {"action": "promote_direct", "resource": {"to_status": "production"}}
}

test_staging_is_not_protected if {
	promote_direct.allow with input as {"action": "promote_direct", "resource": {"to_status": "staging"}}
}
