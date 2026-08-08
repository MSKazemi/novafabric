# CLA signatures

Storage branch for the CLA Assistant workflow (`.github/workflows/cla.yml`).

Signatures are recorded in `signatures/version1/cla.json`, which the action
creates and appends to when a contributor comments the signing phrase on a
pull request. Nothing here is edited by hand.

This branch is deliberately kept off `main` so signature commits never appear
in the project history. It must NOT be branch-protected — the action needs to
commit to it, and a protection rule makes every CLA check fail with
"Branch cla-signatures not found. Make sure the branch where signatures are
stored is NOT protected."

The agreement itself is [CLA.md](https://github.com/MSKazemi/novafabric/blob/main/CLA.md) on `main`.
