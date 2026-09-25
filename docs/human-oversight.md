# Human-Oversight Evidence

When a human reviews, approves, or corrects what an AI agent does, an auditor later wants to know
three things: *what was the human shown when they decided*, *did they correct the agent*, and *what
reason did the agent give them*. EU AI Act Art. 14 (human oversight) and Art. 86 (explanation) both
point at exactly those facts.

NovaFabric records them as sealed capsule evidence anchored to specific turns of the
human-agent conversation ([ADR-0150](./decisions.md)).

**Status: experimental.** What works today, and what does not:

| Object | NF-id | Status |
|---|---|---|
| Conversation-thread provenance (`facets.conversation.turns`) | NF-181 | **experimental** — library + `nova hitl thread show` |
| Decision-context receipt (`facets.conversation.decision_context`) | NF-182 | **experimental** — library + `nova hitl context show` / `verify` |
| Human-override record (`facets.conversation.override`) | NF-187 | **experimental** — library + `nova hitl override list` |
| Agent rationale surfaced to the human (`facets.conversation.rationale`) | NF-188 | **experimental** — library + `nova hitl rationale show` |
| Consent receipt, feedback labels, handoff receipt, acted-on-behalf binding | NF-183/184/189/186 | **future design** |
| Dispute bundle, right-to-explanation export | NF-185/190 | **future design** |
| Linking a receipt to an NF-086 approval record | NF-182 | **planned** — the `nf086_approval_ref` field is carried as an opaque digest; NF-086 itself is not built |

Nothing captures these records automatically yet: your review UI or agent harness calls the library
to record them.

## The boundary — record-only

Every `nova hitl` output prints it: NovaFabric **records** what was shown, decided, overridden and
surfaced. It does **not** adjudicate the decision, grant or deny any right, judge whether the agent's
stated reason was true, or certify that oversight was adequate or lawful.

## What a record holds — and what it never holds

- **Digests, not content.** Every shown item, overridden action, and stated reason is bound by a
  `sha256:` digest. The capsule never holds the text; whoever ran the review surface keeps it, and an
  auditor re-checks it against the digest offline.
- **Codes, not prose.** `decision` is a short code (`approve`, `reject`). `reason` is a short code
  (`risk_within_tolerance`) or the `sha256:` digest of a prose reason. A sentence is refused, because
  prose is where names and other personal data travel.
- **Pseudonyms, not people.** The decider and the overrider are `human:` identity refs
  (`human:did:…`, `human:fp:<hex>`); an email-shaped value is refused.
- **Payload-shaped extension fields are refused.** A record may carry extra bounded scalar fields,
  but a field called `prompt`, `reason_text`, `payload` (and similar), or one holding a nested or
  long value, is rejected.

## Recording (fail-open)

```python
from novafabric.hitl import (
    build_decision_context, shown_item, record_decision_context,
    record_override, record_rationale, digest_turn,
)

items = [
    shown_item("tool_output", content=rendered_tool_output, rendered_at="2026-07-15T10:00:29Z"),
    shown_item("warning", content=rendered_warning, rendered_at="2026-07-15T10:00:30Z"),
]
receipt = build_decision_context(
    "t2", items, decision="approve", reason="risk_within_tolerance",
    decided_by="human:fp:9f2c4a1b7e0d5638",
)
outcome = record_decision_context(capsule, receipt)   # never raises
capsule = outcome.capsule                             # unchanged if outcome.recorded is False
```

`content=` is hashed on the spot and discarded. The `record_*` functions never raise into your
workload: a record whose `turn_ref` does not resolve to a turn in `facets.conversation`, or that is
malformed, is simply not attached, and `outcome.reason` says why. At most one decision-context
receipt is kept per turn; several overrides or rationales may share a turn.

## Checking (fail-closed)

```bash
nova hitl thread show    --capsule <dir|run-id>             # turns in order, pseudonymous authors
nova hitl context show   --capsule <dir|run-id> --turn t2   # what the human saw + root re-check
nova hitl context verify --capsule <dir|run-id>             # 0 ok · 1 defective · 2 nothing to check
nova hitl override list  --capsule <dir|run-id>
nova hitl rationale show --capsule <dir|run-id> --turn t1
```

Each receipt carries a `context_root`: a SHA-256 Merkle root over the ordered shown items, built the
same way as the capsule's own Merkle root. `context verify` recomputes it, so reordering, dropping,
or swapping a shown item is caught. The receipt lives in `capsule.yaml`, which the capsule seal
covers, so the root is bound into the sealed capsule. A dangling `turn_ref`, two receipts for one
turn, or a malformed stored record is a defect (exit 1), never skipped. All commands are read-only.

## Honest limitations

- The records are stored as lists inside `facets.conversation`, not as the separate top-level
  facets ADR-0150 D3 names, because the run-capsule facet registry is closed. Moving them later is
  a key move, not a format change.
- "Re-performable" means the root re-derives from the stored digests. Re-checking that a digest
  matches what was actually on screen needs the rendered content, which NovaFabric does not keep.
- There is no CLI to *write* records, and no handoff signing (NF-189) yet.
- NovaFabric does not verify that the human was really the person named by the pseudonymous ref;
  that is the job of the identity layer that issued it.
