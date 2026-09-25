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
| Scheming / sandbagging / autonomy-attempt / elicitation records | NF-354/355/356/358 | **future design** |
| Deployment-gate decision, safety-case leaves | NF-359/360 | **future design** |
| Sealing the facet into the in-toto attestation, `nova safety verify` | — | **future design** |

Nothing captures these objects automatically yet: an integration (your control-protocol harness,
eval pipeline or framework adapter) calls the library to record them.

## The boundary — record-only, never enforcing

Every `nova safety` output prints it, and the code enforces it:

- **NovaFabric never runs** a control protocol, a monitor, or a dangerous-capability eval.
- **NovaFabric never computes a verdict.** A verdict is `null` or quotes a named external
  source by reference (`verdict_ref` + `verdict_source`); an unattributed verdict — even a bare
  `false` — is rejected with `ComputedVerdictError`.
- **NovaFabric never blocks.** A fired tripwire is reported, not acted on; `nova safety tripwire
  list` exits `0` when triggers are present. Recording is **fail-open**: invalid material is
  dropped with a warning (error type and field names only, never values) and the run continues.
- **No payloads.** Prompts (including monitor prompts), transcripts, exploit payloads, weights and
  activations are rejected (`PayloadCaptureError`); only `sha256:` digests and references are kept.
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

Both helpers return a new capsule dict and never raise for bad safety material. Offline checks:
`verify_governed_action(decision, action_json)`, `verify_tripwire_commitment(trigger, binding)`, and
`verify_guardrail_link(record, guardrail_decision)`.

## Reading

```bash
nova safety control show --capsule <run_id> [--action sha256:…] [--json]
nova safety tripwire list --capsule <run_id> [--framework deepmind_fsf] [--json]
```

`--capsule` takes a capsule directory or a bare run id. Exit `0` whenever the capsule was read —
including when nothing is recorded, which means *not evaluated*, never "safe". Exit `2` for a
missing capsule or a malformed facet. See the [CLI reference](cli-reference.md#nova-safety-control-show).

`nova safety` is unrelated to `nova safety-case` (the ADR-0095 Claims-Arguments-Evidence builder).
