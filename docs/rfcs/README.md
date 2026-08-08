# RFCs

This directory holds NovaFabric's public **requests for comment** — the
deliberative record for non-trivial changes.

- **How the process works:** [RFC process](../governance/rfc-process.md)
- **Template:** [`_template.md`](_template.md)
- **Accepted architectural decisions:** [decisions index](../decisions.md)

## Do I need an RFC?

| Your change | Channel |
|---|---|
| Bug fix, test, docs, refactor that preserves behavior | Pull request |
| New optional adapter, no core changes | Pull request |
| New CLI command or a changed default flag | **RFC** |
| Schema change (Run Capsule, Evidence Bundle, Asset Spec) | **RFC** |
| New runtime dependency | **RFC** |
| Storage format or backend change | **RFC** |
| Anything touching the security posture | **RFC** |
| Not sure | [Ask in Discussions](https://github.com/MSKazemi/novafabric/discussions) |

Asking is always cheaper than writing the wrong document. A maintainer will
answer within a week.

## Index

| RFC | Title | Status | Window |
|---|---|---|---|
| [0001](RFC-0001-runs-partition-key-vs-tenant-idempotency.md) | `runs` partition key vs. tenant idempotency | **Accepted** (option C) | closed 2026-08-06 |

RFC-0001 is the worked example: a real defect, four options with no free one,
accepted as option C and implemented the same day. Read it before writing your
first RFC — it shows the level of "what does this cost?" the process expects.
