package novafabric.defaults.replay_attestation_test

import data.novafabric.defaults.replay_attestation

test_allow_bit_exact if {
	replay_attestation.allow with input as {
		"action": "promote",
		"resource": {"replay_attestation": {"determinism_class": "BIT_EXACT"}},
	}
}

test_allow_bounded_equivalent if {
	replay_attestation.allow with input as {
		"action": "promote",
		"resource": {"replay_attestation": {"determinism_class": "BOUNDED_EQUIVALENT"}},
	}
}

test_deny_non_deterministic_by_default if {
	not replay_attestation.allow with input as {
		"action": "promote",
		"resource": {"replay_attestation": {"determinism_class": "NON_DETERMINISTIC"}},
	}
}

test_deny_missing_attestation if {
	not replay_attestation.allow with input as {
		"action": "promote",
		"resource": {},
	}
}

test_allow_non_deterministic_with_signed_bypass if {
	replay_attestation.allow with input as {
		"action": "promote",
		"resource": {
			"replay_attestation": {"determinism_class": "NON_DETERMINISTIC"},
			"bypass": {"signed": true, "maker": "alice", "checker": "bob"},
		},
	}
}

test_deny_non_deterministic_with_self_bypass if {
	not replay_attestation.allow with input as {
		"action": "promote",
		"resource": {
			"replay_attestation": {"determinism_class": "NON_DETERMINISTIC"},
			"bypass": {"signed": true, "maker": "alice", "checker": "alice"},
		},
	}
}

test_deny_non_deterministic_with_unsigned_bypass if {
	not replay_attestation.allow with input as {
		"action": "promote",
		"resource": {
			"replay_attestation": {"determinism_class": "NON_DETERMINISTIC"},
			"bypass": {"signed": false, "maker": "alice", "checker": "bob"},
		},
	}
}

test_reason_non_deterministic_denied if {
	replay_attestation.reason == "non-deterministic replay denied (no signed bypass)" with input as {
		"action": "promote",
		"resource": {"replay_attestation": {"determinism_class": "NON_DETERMINISTIC"}},
	}
}

test_reason_bypass_recorded if {
	replay_attestation.reason == "non-deterministic replay allowed via signed maker-checker bypass" with input as {
		"action": "promote",
		"resource": {
			"replay_attestation": {"determinism_class": "NON_DETERMINISTIC"},
			"bypass": {"signed": true, "maker": "alice", "checker": "bob"},
		},
	}
}

test_reason_missing_attestation if {
	replay_attestation.reason == "replay_attestation not present in policy input" with input as {
		"action": "promote",
		"resource": {},
	}
}
