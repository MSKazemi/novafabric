# NovaSeal golden fixtures — root version 2 (ADR-0313)

Generated on 2026-10-22 with the ADR-0313 sealing code. They pin the v2 log root
(RFC 6962 / RFC 9162 Merkle Tree Hash, which commits to the tree size). Every file
here must keep verifying, or keep failing, with every later NovaFabric version.

| File | What it is |
|---|---|
| `capsule/.seal/manifest.dsse` | Capsule DSSE envelope (DSSE v1 PAE) |
| `capsule/.seal/manifest.dsse.tsr` | Empty — sealed without a timestamp |
| `capsule/.seal/log-entry.json` | Leaf 2 of a 3-leaf **v2** log (`root_version: 2`) with a carried RFC 9162 inclusion proof |
| `sealer-log-entries.json` | The sealer's four entries in order. Entry 3 is the same capsule sealed again, so leaf 3 duplicates leaf 2 — the `[a, b, c]` / `[a, b, c, c]` shape whose **v1** roots coincide |
| `vectors.json` | v1 and v2 roots for sizes 1..8 (leaf `i` = `sha256(0x00 ‖ "e-i")`) and the duplicated-last-leaf pair |
| `invalid/*.json` | `log-entry.json` variants that must fail verification |

Invalid variants:

| File | Why it must fail |
|---|---|
| `tree-size-inflated.json` | `tree_size` 4 with the size-3 root and proof |
| `root-version-absent.json` | v2 root and proof labelled v1 (no `root_version`) |
| `root-version-unsupported.json` | `root_version: 3` |
| `root-without-tree-size.json` | a root with no `tree_size` |
| `root-of-the-duplicated-tree.json` | the size-4 root (leaf 3 = leaf 2) claimed for size 3 — the case a v1 root cannot tell apart |

The signing key was ephemeral and discarded. Do not regenerate these files;
regenerating them would replace the pinned format and test nothing.
