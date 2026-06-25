package novafabric.defaults.replay_attestation

# Release gate for the deterministic replay attestation (ADR-0094 B).
# Modeled on regression_gate.rego (ADR-0080 sibling).
#
# Deny-by-default when the replay attestation's determinism_class is
# NON_DETERMINISTIC, UNLESS an explicit signed maker-checker bypass is present.
# A BIT_EXACT certificate passes; BOUNDED_EQUIVALENT passes under the default
# policy (a stricter policy can require BIT_EXACT by overriding this file).

default allow := false

default reason := "replay attestation missing or non-deterministic"

# BIT_EXACT always passes.
allow if {
	input.resource.replay_attestation.determinism_class == "BIT_EXACT"
}

# BOUNDED_EQUIVALENT passes under the default policy.
allow if {
	input.resource.replay_attestation.determinism_class == "BOUNDED_EQUIVALENT"
}

# NON_DETERMINISTIC passes ONLY with an explicit signed maker-checker bypass.
allow if {
	input.resource.replay_attestation.determinism_class == "NON_DETERMINISTIC"
	valid_bypass
}

# A bypass is a maker-checker record signed by a checker distinct from the maker.
valid_bypass if {
	bypass := input.resource.bypass
	bypass.signed == true
	bypass.maker != bypass.checker
}

reason := "non-deterministic replay denied (no signed bypass)" if {
	input.resource.replay_attestation.determinism_class == "NON_DETERMINISTIC"
	not valid_bypass
}

reason := "bit-exact replay attestation" if {
	input.resource.replay_attestation.determinism_class == "BIT_EXACT"
}

reason := "bounded-equivalent replay attestation" if {
	input.resource.replay_attestation.determinism_class == "BOUNDED_EQUIVALENT"
}

reason := "non-deterministic replay allowed via signed maker-checker bypass" if {
	input.resource.replay_attestation.determinism_class == "NON_DETERMINISTIC"
	valid_bypass
}

reason := "replay_attestation not present in policy input" if {
	not input.resource.replay_attestation
}
