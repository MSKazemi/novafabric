# Devcontainer

A ready-to-work NovaFabric environment — Python 3.12, `uv`, Node 22 (for the
dashboard), and the GitHub CLI, with `uv sync --all-extras` already run.

**Use it via GitHub Codespaces:** on the repo page, *Code → Codespaces → Create
codespace on main*.

**Use it locally:** open the repo in VS Code with the Dev Containers extension
and choose *Reopen in Container*.

Once it finishes building:

```bash
make test-fast     # ~90 s
make lint
make typecheck
make check-links
uv run nova --help
```

Why this exists: the most common first-contribution failure on this project is a
plain `uv sync`, which strips the optional extras and makes ~30 unrelated tests
fail with import errors that look like your fault. The devcontainer removes that
failure mode entirely.

Everything here mirrors [CONTRIBUTING.md](../CONTRIBUTING.md) — if the two ever
disagree, CONTRIBUTING.md is correct and the mismatch is a bug worth reporting.
