# Example ADR-0126 P3 policy: condition an evidence-export gate on the capsule's
# recorded deployment environment (input.resource.deployment_environment).
#
# Production exports must carry a redaction proof AND report zero unsafe skips;
# any other environment -- including an unrecorded one (null) -- only needs the
# redaction proof. Illustrative, not part of the built-in bundle.
package novafabric.examples.production_export

default allow := false

allow if {
    input.resource.deployment_environment == "production"
    input.resource.redaction_proof_present == true
    input.resource.unsafe_skips == 0
}

allow if {
    input.resource.deployment_environment != "production"
    input.resource.redaction_proof_present == true
}
