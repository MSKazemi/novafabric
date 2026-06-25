package novafabric.judge

import rego.v1

# Deny promotion if inter-judge kappa is below threshold
deny contains reason if {
    input.judgment.inter_judge_agreement != null
    input.judgment.inter_judge_agreement < 0.6
    reason := sprintf(
        "inter-judge agreement (kappa=%.2f) below threshold 0.6",
        [input.judgment.inter_judge_agreement]
    )
}

# Deny if consensus verdict is fail
deny contains reason if {
    input.judgment.consensus_verdict == "fail"
    reason := sprintf(
        "judge consensus: fail (score=%.2f)",
        [input.judgment.consensus_score]
    )
}
