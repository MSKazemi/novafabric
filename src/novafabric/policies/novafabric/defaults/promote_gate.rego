package novafabric.defaults.promote_gate

default allow := false
default reason := "promotion denied by default policy"

# Only agents carry an eval-score requirement (eval-gated promotion, v0.1).
# Tools, datasets, and prompts pass the eval check unconditionally.
eval_ok if input.resource.asset_type != "agent"

eval_ok if input.resource.eval_score >= 0.90

skips_ok if input.resource.unsafe_skips == 0

allow if {
	eval_ok
	skips_ok
}

reason := "eval score below threshold" if {
	not eval_ok
	skips_ok
}

reason := "unsafe_skips present" if {
	eval_ok
	not skips_ok
}

reason := "eval score below threshold and unsafe_skips present" if {
	not eval_ok
	not skips_ok
}
