# Frontier-Safety Evidence

Frontier-safety frameworks — Anthropic's Responsible Scaling Policy (ASL levels), OpenAI's
Preparedness Framework, Google DeepMind's Frontier Safety Framework — make commitments that have to
be honoured *at runtime*: this threshold eval runs before deployment; if this indicator fires, these
safeguards apply. AI-control protocols (trusted monitoring, Ctrl-Z resampling, defer-to-human) make
a decision about each action a model takes.

NovaFabric records those events as sealed capsule evidence in the optional
`facets.frontier_safety` block ([ADR-0167](./decisions.md)).

**Status: experimental.** What works today, and what does not:

| Object | NF-id | Status |
|---|---|---|
| Threshold-eval binding (`threshold_eval`) | NF-351 | **experimental** — library/facet API only, no CLI yet |
| Framework-commitment binding (`commitment_binding`) | NF-353 | **experimental** — library/facet API only, no CLI yet |
| AI-control-protocol decision (`control_decision[s]`) | NF-352 | **experimental** — library + `nova safety control show` |
| Tripwire trigger (`tripwire_trigger[s]`) | NF-357 | **experimental** — library + `nova safety tripwire list` |
| Deception / scheming / eval-awareness signal (`deception_signal[s]`) | NF-354 | **experimental** — library + `nova safety signal list` |
| Sandbagging / under-elicitation record (`sandbagging_record[s]`) | NF-355 | **experimental** — library + `nova safety signal list` |
| Sandbox-escape / exfiltration / replication attempt (`autonomy_attempt[s]`) | NF-356 | **experimental** — library + `nova safety signal list` |
| Elicitation-during-deployment record (`elicitation_record[s]`) | NF-358 | **experimental** — library + `nova safety signal list` |
| Deployment-gate decision, safety-case leaves | NF-359/360 | **future design** |
| Sealing the facet into the in-toto attestation, `nova safety verify` | — | **future design** |

Nothing captures these objects automatically yet: an integration (your control-protocol harness,
eval pipeline or framework adapter) calls the library to record them.

## The boundary — record-only, never enforcing

Every `nova safety` output prints it, and the code enforces it:

- **NovaFabric never runs** a control protocol, a monitor, a dangerous-capability eval, a
  scheming detector, a sandbox-escape test, or a capability elicitation — and never computes a
  capability ceiling (an inlined `ceiling`-style field raises `ComputedVerdictError`).
- **NovaFabric never computes a verdict.** A verdict is `null` or quotes a named external
  source by reference (`verdict_ref` + `verdict_source`); an unattributed verdict — even a bare
  `false` — is rejected with `ComputedVerdictError`.
- **NovaFabric never blocks.** A fired tripwire is reported, not acted on; `nova safety tripwire
  list` exits `0` when triggers are present. Recording is **fail-open**: invalid material is
  dropped with a warning (error type and field names only, never values) and the run continues.
- **No payloads.** Prompts (including monitor prompts), transcripts, exploit payloads, weights and
  activations are rejected (`PayloadCaptureError`); only `sha256:` digests and references are kept.
  An autonomy attempt is **counts plus a report digest** — exploit steps, commands and exfiltrated
  data are rejected too, and its string extras must be identifier-shaped labels or digests.
  Field names are matched case- and separator-insensitively by substring (`exploitSteps`,
  `shell-command` and `ceilingValue` are all caught); a `*_ref` / `*_digest` field holding a
  `sha256:` digest is the sanctioned way to point at such an artifact. Free-form strings on the
  alignment-signal objects are capped at 512 characters.
- **No duplication of guardrail decisions.** When a control-protocol monitor also acted as an
  input/output guardrail, reference that `facets.safety` decision by digest via
  `guardrail_decision_ref`; inlining its fields raises `GuardrailDuplicationError`.

## Recording from an integration

```python
from novafabric.frontier_safety import (
    ControlDecision, TripwireTrigger, digest_ref,
    record_control_decision, record_tripwire_trigger,
)

capsule = record_control_decision(capsule, ControlDecision(
    protocol="resample",            # trusted_monitor | untrusted_monitor | resample | defer_to_trusted
    decision="resample",            #   | defer_to_human | interrogate | other
    governed_action_ref=digest_ref(action_json),   # the action the protocol governed
    monitor_ref=digest_ref(monitor_identity),      # model/config identity, never its prompt
    verdict_ref=digest_ref(protocol_log_entry),    # the protocol's own decision record
))

capsule = record_tripwire_trigger(capsule, {
    "indicator_id": "fsf.ccl.cyber.autonomous-uplift",
    "framework": "deepmind_fsf",
    "commitment_ref": digest_ref(published_commitment_text),   # the NF-353 commitment implicated
    "triggered_by_ref": digest_ref(observing_eval_record),
})
```

Alignment-risk evidence (P3) follows the same pattern — each object is an external finding by
reference:

```python
from novafabric.frontier_safety import (
    record_deception_signal, record_sandbagging_record,
    record_autonomy_attempt, record_elicitation_record,
)

capsule = record_deception_signal(capsule, {
    "signal_type": "in_context_scheming",   # alignment_faking | eval_awareness | strategic_deception
    "detector_ref": digest_ref(detector_identity),          #   | reward_hacking | other
    "evidence_digest": digest_ref(evidence_bundle),         # the evidence, by digest only
    "verdict_ref": digest_ref(eval_verdict_document),       # verdict_source is fixed: scheming_eval
})
capsule = record_sandbagging_record(capsule, {
    "elicitation_regime": "best_effort",    # typical | best_effort | fine_tuned | adversarial
    "declared_ceiling_ref": digest_ref(declared_ceiling),
    "observed_vs_declared": "underperformed",   # consistent | underperformed | unknown
    "verdict_ref": digest_ref(evaluator_report),
    "verdict_source": "red_team",           # required unless observed_vs_declared is "unknown"
})
capsule = record_autonomy_attempt(capsule, {
    "attempt_type": "sandbox_escape",       # self_exfiltration | autonomous_replication
    "outcome": "detected",                  #   | weight_exfiltration | undermining_safeguards
    "attempt_count": 3,                     # 1..1,000,000; zero attempts = no record
    "report_ref": digest_ref(evaluator_report),
    "verdict_ref": digest_ref(evaluator_verdict),
})
capsule = record_elicitation_record(capsule, {
    "elicitation_effort": "tool_augmented", # typical | scaffolded | tool_augmented | best_effort
    "elicitation_ref": digest_ref(elicitation_report),
    # no_ceiling_computed is always true; false is rejected
})
```

Every helper returns a new capsule dict and never raises for bad safety material. Offline checks:
`verify_governed_action(decision, action_json)`, `verify_tripwire_commitment(trigger, binding)`, and
`verify_guardrail_link(record, guardrail_decision)`.

## Reading

```bash
nova safety control show --capsule <run_id> [--action sha256:…] [--json]
nova safety tripwire list --capsule <run_id> [--framework deepmind_fsf] [--json]
nova safety signal list   --capsule <run_id> [--type deception|sandbagging|autonomy|elicitation] [--json]
```

`--capsule` takes a capsule directory or a bare run id. Exit `0` whenever the capsule was read —
including when nothing is recorded, which means *not evaluated*, never "safe", and when a
tripwire fired or a successful sandbox escape was reported. Exit `2` for a missing capsule, a
malformed facet, or an unknown option value. See the [CLI reference](cli-reference.md#nova-safety-control-show).

`nova safety` is unrelated to `nova safety-case` (the ADR-0095 Claims-Arguments-Evidence builder).
