# AIUC-1 evidence crosswalk

Which NovaFabric records may be relevant to which AIUC-1 control IDs. The table names each
ID, the file or command that produces the record, how direct the record is, and what it
leaves out. An IDs-only catalog,
[`examples/catalogs/aiuc-1-ids-only.yaml`](../examples/catalogs/aiuc-1-ids-only.yaml), lets
you check your own runs with `nova audit map` once you have filled it in from your licensed
copy.

> **Not affiliated, and not a certification.** NovaFabric is not affiliated with, endorsed
> by, or certified by AIUC, the publisher of AIUC-1. AIUC-1 is a
> standard published by its owner, and its name may be a trademark of that owner; this page
> uses the name only to identify the standard. This page reproduces no AIUC-1 text: it cites
> control identifiers only. The requirement wording, and everything an auditor will check
> against, is in your licensed copy of the standard, so read that. The mapping below is
> the NovaFabric project's own opinion of which records are relevant. It is not audit, legal or
> certification advice, and only an auditor decides whether a record meets a requirement.
>
> NovaFabric is not affiliated with, endorsed by, or certified by the publishers of these frameworks. A mapping shows which recorded evidence exists; it does not certify that a control is met.

---

## How to read the table

- **The IDs** refer to the Q4-2026 release of AIUC-1 (15 October 2026). The standard is
  revised quarterly. IDs can move, be added or be retired, so check each one against the
  release you are being audited on. NovaFabric does not track AIUC-1 releases.
- **Status in the standard** gives how that release groups the evidence item, as we read
  it: *core* or *supplemental*. A dash means we have not confirmed it; read your copy.
- **Area** is our own one-line label for the subject, so the row can be scanned. It is not
  the standard's title for the item.
- **Strength** is our judgement:
  - **Strong:** a NovaFabric record is itself a record of the kind the item is about.
  - **Partial:** NovaFabric supplies supporting records, but the item is mainly about a
    policy, a human process, or a system NovaFabric is not, or the record or catalog check
    shows less than the item's wording suggests.
- **Maturity** follows the project's labels. Commands marked *experimental* are implemented
  but their interface may change.

## The crosswalk

| ID | Area (our label) | Status in the standard | NovaFabric record or command | Strength | What it does not give you |
|---|---|---|---|---|---|
| **E015.1** | What agent activity is recorded | core | The `capsule.yaml` manifest and the stream files it references: `model-calls.jsonl`, `tool-calls.jsonl` and `trace.jsonl`. `capture-health.json` appears when events were dropped. `nova validate <capsule-dir>` checks the capsule against its schema | Strong | NovaFabric records only what its hooks see: model calls on the hooked SDK and HTTP paths, and tool calls made through MCP, `nova mcp-proxy` or a framework adapter. A call made on a path it does not hook leaves no record, and the capsule cannot show that the call is missing |
| **E015.2** | A record per agent execution | supplemental | One Run Capsule per captured run (`capsule.yaml`), whose model calls are in `model-calls.jsonl`. When the run was traced, its execution spans (including the root span) are in `trace.jsonl`. The catalog check for this ID asks only for at least one recorded model call | Partial | A covered result shows that the run recorded a model call: the same check as E015.1. The catalog does not check `trace.jsonl`, so it shows nothing about spans, sub-agent handoffs, approvals or reasoning; read those files yourself. One capsule covers one captured process. Work handed to another process is a separate capsule unless you capture and link it too (see [Multi-agent capture](tutorials/multi-agent-capture.md)) |
| **E015.3** | Where records are kept and who can read them | core | Every capture runs the secret scanner and writes `redaction-proof.json`. `nova retention plan` / `apply` (experimental) apply a retention policy and append a hash-chained entry for every decision. Server mode adds Postgres row-level security for shared access | Partial | A local capsule directory is protected only by file-system permissions. Retention is experimental. Prompts and responses are stored in full by default (see A006.1) |
| **E015.4** | Integrity of the records | supplemental | A NovaSeal seal over the capsule (`.seal/manifest.dsse` plus a Merkle log entry), checked offline by `nova verify <capsule>` (experimental). `nova export-evidence` builds a signed Evidence Bundle | Partial | **A seal that verifies is not enough for the log.** A seal made before evidence digests signs the manifest only: it verifies, but it does not bind `model-calls.jsonl`, `tool-calls.jsonl` or `trace.jsonl`, so it cannot show that one of them was edited. `nova audit map` still scores it as seal evidence, adding only a non-blocking `seal_unbound` notice. A seal that fails verification is a `seal_invalid` finding, and `nova audit map` exits 1 on it. Run `nova verify <capsule>` and check that the evidence-binding line passed; it prints NOT PRESENT for such a seal. Only a digest-bearing seal supports a claim about the logs. Sealing is opt-in: it needs `novaseal.yaml` and your key. A seal makes later edits **detectable**. It does not stop anyone deleting or rewriting files, so if the item calls for prevention, add write-once storage. The Merkle tree head is not independently witnessed. A seal made with a local key is self-asserted unless you pin a CA |
| **D003.3** | Records of tool calls | core | `tool-calls.jsonl`: one record per call, with fields for tool name, version, provider, transport, arguments, start and finish timestamps and status, plus an `mcp` block for MCP calls (`schemas/tool-call.schema.json`). A field is null when the source does not report it | Strong | NovaFabric records calls. It raises no alert when a call falls outside an allowed scope. Recording is fail-open: a record that cannot be written is dropped and counted in `capture-health.json`, not retried |
| **D003.4** | Human sign-off on tool actions | core (moved from supplemental in Q4-2026) | The `novafabric.hitl` library (experimental) writes into the capsule what a human was shown, the decision code, and any override. You read them with `nova hitl context show` and `nova hitl override list` ([Human-oversight evidence](human-oversight.md)) | Partial | Nothing is recorded automatically: your approval UI or agent harness must call the library. NovaFabric does not make a tool wait for approval. The records hold digests, not the content the human saw |
| **C002.1** | Test results before release | core | Run your test or evaluation suite under `nova capture` so the run becomes a capsule. Seal it, and hand over a signed Evidence Bundle as the record of that run | Partial | NovaFabric records the test run. It does not design the tests, rate the severity of findings, or record who signed off. Those stay in your own documents |
| **C002.2** | Automated test gate in CI | supplemental | `nova diff <baseline> <candidate> --assert-no-regressions` exits 1 on any recorded difference, so it can fail a pipeline | Strong | The gate fails on *any* difference. Deciding which differences are safety regressions is up to you. It compares recorded runs, so it covers only what your suite exercises |
| **C002.4** | Regression check across a model change | supplemental | Capture the same suite on the old model and on the new one, then `nova diff` the two capsules. `model-calls.jsonl` records which model each call reported | Partial | In the Q4-2026 release this item covers **open-weight** model changes only. NovaFabric records the model identifier a call reports, not a hash of the weights. The documented test approach is yours |
| **A006.1** | Personal data in records | — | `nova capture --pii-subject ID` (experimental) replaces detected email addresses, phone numbers and person names with encrypted tokens, each one listed in `redaction_manifest.json`. `nova pii erase ID` then makes them unreadable. The secret scanner runs on every capture | Partial: **a risk** | The catalog check `pii_scan` shows only that a redaction manifest exists (a parseable `redaction_manifest.json`), not that inputs, outputs or logs are filtered. **By default a capsule stores full prompts and responses** in `model-calls.jsonl`, at every capture level. Without `--pii-subject` nothing is encrypted. The detector is regex-based and finds three kinds of personal data; everything else stays in plaintext. Binary artefacts, media and in-process (adapter or decorator) capsules are not covered. An auditor may flag the default, so decide on it before you capture production traffic |
| **B001** | Testing by an outside party | — | Capture the outside tester's runs as capsules, seal them, and hand over Evidence Bundles. `nova diff` compares one test round with the next | Partial | NovaFabric does not perform the testing and is not a testing provider. The tester's report is the evidence; the capsules back it up |

Controls not in this table may still have relevant records. The table lists the IDs where we
checked a specific NovaFabric file or command against the item.

---

## Check your runs with the IDs-only catalog

[`examples/catalogs/aiuc-1-ids-only.yaml`](../examples/catalogs/aiuc-1-ids-only.yaml) is a
`nova audit map` profile that holds **control IDs only**. Every `title` and `description` is
empty, for you to fill in from your licensed copy, in your own words if the file will be
shared or committed. The evidence requirements are NovaFabric's own: each one says which
capsule file is checked.

It lists only the IDs that a capsule check can test: E015.1, E015.2, E015.4, D003.3 and
A006.1. The other rows in the table above depend on a process, a document or a person, so
assess them separately. A control with no evidence requirement is refused when the catalog
loads, because with nothing to check it would be scored as covered.

```bash
cp examples/catalogs/aiuc-1-ids-only.yaml my-aiuc-1.yaml
# fill in title and description for each control, then:
nova audit map --profile my-aiuc-1.yaml --capsule <capsule-id>
```

For a run that recorded model calls and tool calls but was neither sealed nor captured with
`--pii-subject`, E015.1, E015.2 and D003.3 come back **covered** and E015.4 and A006.1
**missing**. The command exits 1, so it can gate a CI job. Add `--json` for machine-readable
output. To close the two gaps, configure sealing (see
[Prove a run to an auditor](tutorials/prove-a-run-to-an-auditor.md)) and capture with
`--pii-subject`. Read a covered result for what its check tests: E015.2 passes on one
recorded model call, as E015.1 does; E015.4 passes on any seal that verifies, including one
made before evidence digests, which does not bind the logs (the report adds a non-blocking
`seal_unbound` notice for it, and a seal that fails verification is a `seal_invalid` finding
that makes the command exit 1); A006.1 passes on a parseable
redaction manifest.

The same IDs work in the JSON catalog of
`nova export-compliance iso42001 --catalog aiuc-1.json --evidence <kinds> --capsule <dir> --standard "AIUC-1"`,
which binds each evidenced control to a digest of the capsule. There, a control is
`evidenced` because its `evidence_kind` is one of the kinds **you** list with `--evidence`;
the command does not inspect the capsule for that kind, so the result is your assertion
bound to a capsule digest, not a check. It refuses (exit 2, nothing written) a capsule whose
seal fails verification or was made before evidence digests. The step-by-step walkthrough
is [Map NovaFabric evidence to your own control framework](tutorials/map-evidence-to-your-control-framework.md).

---

## What this page does not tell you

- **Whether a requirement is met.** A row says that a record exists and may be relevant.
  AIUC-1 is published by AIUC; this page does not claim or imply certification against it,
  and only your auditor decides whether a record meets a requirement.
- **Anything outside a capture.** Policies, training, vendor management and incident plans
  produce no capsule. For those items NovaFabric can at most supply an attachment.
- **Completeness.** `model_calls` is met by one recorded call. It does not prove that every
  call was recorded.
