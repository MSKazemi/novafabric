# Map NovaFabric evidence to your own control framework

**~10 minutes · works today.** By the end you will have checked a recorded agent run against a
control framework you chose, written as a small YAML file, and you will know which controls the
run's records support and which they do not.

Use this when an auditor, a customer questionnaire or an internal policy names controls that
NovaFabric does not ship a profile for — an internal AI policy, a sector checklist, or a
proprietary certification framework.

---

## Why you bring the catalog

NovaFabric ships built-in profiles for a few frameworks (`nist-ai-rmf`, `eu-ai-act-high-risk`,
`gdpr`, `soc2-type2`, `iso42001`, `scientific-reproducibility`). Many frameworks, though, are
copyrighted by their publishers — ISO/IEC standards, the AICPA Trust Services Criteria behind
SOC 2, and AI-agent certification frameworks such as AIUC-1 among them. NovaFabric does not
reproduce their text: its built-in profiles cite control identifiers and describe each control in
NovaFabric's own words.

So for any framework, the same pattern applies: **you write the catalog**, using the control IDs
from your licensed copy and descriptions in your own words, and NovaFabric checks your capsules
against it.

> NovaFabric is not affiliated with, endorsed by, or certified by the publishers of these frameworks. A mapping shows which recorded evidence exists; it does not certify that a control is met.

---

## 1. Write the catalog

The example below uses a **fictional** framework, `EXAMPLE-AI-1`, with three controls. Replace
the IDs and wording with your own.

```yaml
id: example-ai-1
name: EXAMPLE-AI-1 (fictional framework for this tutorial)
framework: EXAMPLE-AI-1
version: "1.0"
controls:
  - id: EX-LOG-1
    title: Agent activity is logged
    description: Each run keeps a record of its model calls.
    evidence_requirements:
      - evidence_type: model_calls
        description: model-calls.jsonl is present and non-empty
  - id: EX-TOOL-1
    title: Tool use is logged
    description: Each run keeps a record of the tools the agent called.
    evidence_requirements:
      - evidence_type: tool_calls
        description: tool-calls.jsonl records at least one call
  - id: EX-SEAL-1
    title: Records are tamper-evident
    description: The run's records carry a seal that makes later edits detectable.
    evidence_requirements:
      - evidence_type: seal_signature
        description: a NovaSeal seal over the capsule that verifies
```

Save it as `example-ai-1.yaml`. Each control needs `id`, `title`, `description` and at least one
evidence requirement; each requirement needs `evidence_type` and `description`, and may set
`required: false` (the default is `true`). The [developer guide](../developer-guide.md#adding-a-compliance-audit-profile) lists
every field.

### What each evidence type checks

A requirement is met by what is actually in the capsule, not by what the YAML says:

| `evidence_type` | Met when the capsule has… |
|---|---|
| `model_calls` | a non-empty `model-calls.jsonl` |
| `tool_calls` | a non-empty `tool-calls.jsonl` (every capture creates it, empty, so an empty file does not count) |
| `seal_signature` | a NovaSeal seal that **verifies** against the capsule's files |
| `pii_scan` | a `redaction_manifest.json` that parses — written by `nova capture --pii-subject` |
| `lineage` | a non-empty `lineage.jsonl` |
| `tool_permissions`, `policy_events` | a `tool-permission-events.jsonl` (`policy_events` also needs a deny or policy-decision record). `nova capture` does not write this file today, so these requirements are reported "not captured by this version" rather than failed |

A misspelt `evidence_type` is refused when the file loads, with the list of valid ones, so a typo
cannot silently leave a control uncovered.

---

## 2. Check a run against it

```bash
nova audit map --profile ./example-ai-1.yaml --capsule <capsule-id>
```

`--capsule` names one capsule under the capsules directory (by default, where `nova capture`
writes; pass `--data-dir <root>` to read `<root>/capsules/`). Leave it out to score every capsule
there.

For a run that recorded model calls and tool calls but was not sealed, the table reads:

```text
  Capsules : 1
  Controls : 3 total  2 covered  0 partial  1 missing
  Score    : 66.7%

┃ Control   ┃ Title                      ┃ Status  ┃ Score ┃ Capsules ┃
│ EX-LOG-1  │ Agent activity is logged   │ covered │  100% │        1 │
│ EX-TOOL-1 │ Tool use is logged         │ covered │  100% │        1 │
│ EX-SEAL-1 │ Records are tamper-evident │ missing │    0% │        0 │
```

The command exits **1** because a required control is missing, so it can gate a CI job. It exits
**2** for a malformed catalog or a capsule ID that does not exist. Add `--json` for
machine-readable output. To close the `EX-SEAL-1` gap, seal runs as they are captured — see
[Prove a run to an auditor](prove-a-run-to-an-auditor.md).

---

## 3. Using it for a proprietary framework

For a framework such as AIUC-1, ISO/IEC 42001 or SOC 2, build the catalog from **your own
licensed copy**:

- copy the control **identifiers** exactly, so your report lines up with the auditor's checklist;
- write each `title` and `description` in your own words — do not paste the publisher's text into a
  file you will share or commit;
- map each control only to evidence types whose check (the table above) genuinely bears on it;
- leave controls that no evidence type fits **out of the catalog** and assess them separately. A
  control with no evidence requirements is refused when the catalog loads, because with nothing to
  check it would otherwise be scored as covered.

For AIUC-1 there is a ready-made starting point: the
[AIUC-1 evidence crosswalk](../aiuc-1-evidence-crosswalk.md) rates which NovaFabric records
may be relevant to which control IDs, and ships an IDs-only catalog whose titles and
descriptions you fill in from your copy.

If you need a JSON report that carries a digest of the capsule for each evidenced control, the
same catalog idea works with `nova export-compliance iso42001 --catalog controls.json --capsule
<dir> --standard "<your framework>"` ([CLI reference](../cli-reference.md)).

---

## What this does *not* tell you

- **It does not say a control is met.** It says which NovaFabric records exist for a run. Whether
  those records satisfy a control is a judgement for you and your auditor.
- **It only sees what was captured.** A control about people, process or policy documents will
  usually have no capsule evidence at all; that is expected, and such controls stay outside the
  catalog.
- **Presence is not quality.** `model_calls` is met by one recorded call; it does not check that
  every call was recorded or that the records are complete.
