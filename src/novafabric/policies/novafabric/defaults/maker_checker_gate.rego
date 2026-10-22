package novafabric.defaults.maker_checker_gate

# Historical opt-in SoD gate from ADR-0058. NOT EVALUATED: no NovaFabric action maps to
# this package (see policy/_opa_engine.py _ACTION_TO_PACKAGE), so `deny_direct` has
# never blocked anything. Superseded by ADR-0312: the `promote_direct` action
# (promote_direct.rego) gates single-actor promotion into protected stages and denies
# `production` by default. Kept so existing bundles that reference it still load.

default deny_direct := false

deny_direct if {
    input.action == "promote_direct"
    input.resource.to_status == "staging"
}

deny_direct if {
    input.action == "promote_direct"
    input.resource.to_status == "production"
}
