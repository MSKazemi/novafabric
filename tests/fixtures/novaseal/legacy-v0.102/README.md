# NovaSeal golden fixtures — format as released through v0.102.1

Generated on 2026-10-01 with the **released** v0.102.1 sealing code, before the
2026-10-01 sealing/verification correctness fixes. They pin backward compatibility:
every file here must keep verifying with every later NovaFabric version.

| File | What it is |
|---|---|
| `capsule/.seal/manifest.dsse` | Capsule DSSE envelope signed over the **legacy pre-spec PAE** (`"DSSEv1"` + 8-byte little-endian lengths, no spaces) |
| `capsule/.seal/manifest.dsse.tsr` | Empty — sealed without a timestamp |
| `capsule/.seal/log-entry.json` | Merkle log entry, leaf 2 of a 3-leaf log, **no carried inclusion proof** |
| `sealer-log-entries.json` | The three entries of the sealer's log, in order, so a test can rebuild the sealer's own log |
| `promote-proposal.dsse` | Promote proposal envelope, also over the legacy PAE |

The signing key was ephemeral and discarded; only the self-signed certificate (inside
each envelope) survives. Do not regenerate these files — regenerating them would
replace the released format with the current one and test nothing.
