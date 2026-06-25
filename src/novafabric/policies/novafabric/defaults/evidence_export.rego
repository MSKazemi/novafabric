package novafabric.defaults.evidence_export

default allow := false

allow if {
    input.resource.redaction_proof_present == true
    input.resource.unsafe_skips == 0
}
