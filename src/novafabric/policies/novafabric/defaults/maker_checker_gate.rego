package novafabric.defaults.maker_checker_gate

# Opt-in SoD gate (ADR-0058).
# Load this policy file to block single-actor `nova promote direct`
# for promotions targeting staging or production.
# Teams that do not load this file are unaffected.

default deny_direct := false

deny_direct if {
    input.action == "promote_direct"
    input.resource.to_status == "staging"
}

deny_direct if {
    input.action == "promote_direct"
    input.resource.to_status == "production"
}
