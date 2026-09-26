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
| Consent receipt, ISO/IEC TS 27560-shaped (`facets.conversation.consent`) | NF-183 | **experimental** — library + `nova consent record` / `show` / `verify` |
| Accountability handoff receipt (`facets.conversation.handoff`) | NF-189 | **experimental** — library + `nova hitl handoff list` (offline signature check) |
| Acted-on-behalf binding to an NF-084 delegation hop (`facets.conversation.acted_on_behalf`) | NF-186 | **experimental** — library + `nova hitl acted-as` (by reference; NF-084 state surfaced, never re-derived) |
| Feedback labels | NF-184 | **future design** |
| Dispute bundle, right-to-explanation export | NF-185/190 | **future design** |
| Linking a receipt to an NF-086 approval record | NF-182 | **planned** — the `nf086_approval_ref` field is carried as an opaque digest; NF-086 itself is not built |

Nothing captures these records automatically yet: your review UI or agent harness calls the library
to record them (`nova consent record` is the one CLI writer).

## The boundary — record-only

Every `nova hitl` and `nova consent` output prints it: NovaFabric **records** what was shown,
decided, overridden, surfaced, handed off, acted under and consented to. It does **not** adjudicate
the decision, assert that a consent was legally valid, grant or deny any right, judge whether the agent's
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

## Consent, handoff, and acted-on-behalf (P3)

**Consent receipt (NF-183).** Fields follow the ISO/IEC TS 27560 / W3C DPV consent record:
`consent_id`, `subject_ref` (a `human:` ref), `purpose` and `action` (DPV concept codes such as
`dpv:ServiceProvision`, `dpv:Store` — no prose), `given_at`, optional `expiry`, `withdrawable`,
optional `withdrawn_at`, and `receipt_digest`. The digest covers every field the receipt was given
with, including extension fields, but not `withdrawn_at`: withdrawing a consent does not change which
consent was withdrawn. A receipt may be recorded on a capsule with no conversation turns; `turn_ref`
is optional.

```bash
nova consent record --capsule <dir|run-id> --subject human:fp:9f2c4a1b7e0d5638 \
    --purpose dpv:ServiceProvision --scope dpv:Store --expiry 2027-07-15T00:00:00Z
nova consent verify --capsule <dir|run-id>      # 0 intact · 1 defective · 2 nothing to check
```

`consent record` rewrites `capsule.yaml` atomically, which changes the capsule Merkle root: re-issue
any seal or signature made over the capsule before it.

**Handoff receipt (NF-189).** Records that responsibility for a `scope` (short codes) passed from
`from_party` to `to_party` at a turn. The two parties must differ, compared after Unicode
normalisation and case-folding, so a self-handoff cannot hide behind a case change. Two
fingerprint refs are the same party when one is a prefix of the other (`human:fp:<16 hex>` and
`human:fp:<32 hex>` of the same key), and a `fp:` ref must carry 16 to 64 hex characters. `sig` is either
an `ed25519:` signature over the canonical record — produced by `novafabric.hitl.sign_handoff` with
the same keyring primitive maker-checker approvals use — or a `sha256:` reference to a signature
envelope held elsewhere. `nova hitl handoff list` verifies the first kind offline; if the signer is a
fingerprint ref (`human:fp:<hex>`) it also checks the key against the fingerprint. The second kind is
shown as `reference_only`.

**Acted-on-behalf binding (NF-186).** Joins a turn to the NF-084 delegation hop the agent acted
under: `turn_ref`, `delegation_hop_ref` (the hop's `grant_ref` digest) and `principal_ref`.
`nova hitl acted-as --turn t1 --delegation nf084.json` looks the hop up and shows what the NF-084
document says its verifier recorded — `established`, or `broken` with its `broken_hop` index, or
`unverified` / `absent` / `ambiguous` / `malformed`. The binding's `principal_ref` must also be
that hop's granter or the chain root's granter (`user:did:…` in the document matches
`human:did:…`); otherwise the state is `principal_mismatch` and the command exits 1. It does not
re-verify the chain: `established` is labelled "as recorded by the NF-084 document, not
re-verified".

## Honest limitations

- The records are stored as lists inside `facets.conversation`, not as the separate top-level
  facets ADR-0150 D3 names, because the run-capsule facet registry is closed. Moving them later is
  a key move, not a format change.
- "Re-performable" means the root re-derives from the stored digests. Re-checking that a digest
  matches what was actually on screen needs the rendered content, which NovaFabric does not keep.
- Only consent has a CLI writer; the other records are written by the library.
- A handoff signature proves which key signed the record. It binds that key to a party only when
  the party's ref is a key fingerprint; for a DID or SPIFFE ref the binding is reported `unbound`.
  A `sha256:` sig is a reference and is not verified here.
- NF-084 delegation documents do not yet live in the capsule (the facet registry does not list
  `delegation`), so `acted-as` usually reads one passed with `--delegation`. Its answer is only as
  good as the NF-084 verification recorded in that document, which is not authenticated: the v0
  document carries no public keys or trust anchors to re-verify it with.
- Consent `verify` checks the receipt is intact. It does not say the consent is still current, and
  never that it was valid.
- NovaFabric does not verify that the human was really the person named by the pseudonymous ref;
  that is the job of the identity layer that issued it.
