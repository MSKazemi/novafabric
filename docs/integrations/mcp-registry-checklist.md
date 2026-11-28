# MCP Registry submission checklist

**Status: planned.** The [MCP evidence server](mcp-evidence-server.md) is **not** listed in the
MCP Registry or any other server directory, and nothing in this repository submits it. This page
lists what must be true before a listing is made. Each item says whether it is met today. A
submission is a manual, owner-approved step; no workflow publishes registry metadata
automatically, and none should.

The registry's own requirements are in its
[quickstart](https://modelcontextprotocol.io/registry/quickstart) and
[package types](https://modelcontextprotocol.io/registry/package-types) pages. Re-read them
before submitting, because they change; the items below were checked against them on 2026-11-28.

## 1. A release a registry client can install and start

- [ ] **The server is in a published release.** *Not met.* It is on `main` only. The newest PyPI
  release, v0.109.0, does not contain `novafabric.mcp.evidence_server`.
- [ ] **It can be started from the installed package alone.** *Not met.* Today it starts only as
  `python -m novafabric.mcp.evidence_server --scope DIR`; there is no console script and no `nova`
  sub-command for it (the [server reference](mcp-evidence-server.md#not-in-this-slice) lists the
  CLI entry point as not implemented). A registry package entry describes how a client launches
  the package, so this has to exist first.
- [ ] **The launch arguments in the metadata match `--help` of that release**, including that
  `--scope` is required and has no default. The metadata must not invent a default scope.
- [ ] **The status label is decided.** Either the server is no longer experimental, or the listing
  says it is.

## 2. Metadata claims, each checked against the code

Every claim in the listing's `title` and `description` (the registry caps `description` at 100
characters) must be true of the release being listed. Check each before submitting:

- [ ] Read-only: three tools, all annotated `readOnlyHint: true` and `openWorldHint: false` (see
  `TOOLS` in `src/novafabric/mcp/evidence_server.py`).
- [ ] Offline: no network use, no subprocess, no writes. The
  [offline walkthrough](mcp-client-setup.md#offline-check-a-sealed-and-a-tampered-capsule) runs with
  networking disabled.
- [ ] No content: no prompts, responses, tool arguments or tool results are returned.
- [ ] Transport: stdio only. Do not list a remote endpoint. The Streamable HTTP transport is not
  implemented, and the SEP-1649 Server Card that `nova serve` publishes describes the HTTP
  surface, not this server.
- [ ] Verification wording: "verifies capsule seals offline" with the scope the tool itself
  states (`verification_level`). Do not say it proves who sealed a capsule; with a local key the
  identity is self-asserted.
- [ ] Clients: name only clients with a **verified** row in
  [the client guide](mcp-client-setup.md#what-was-verified-and-with-what), with what was verified.

## 3. Name, namespace and ownership

- [ ] **The server name is in a namespace the publisher can prove.** With GitHub authentication
  the registry requires names under `io.github.<owner>/`, where `<owner>` is the account that
  publishes. Use the owner of the public repository, the one the docs and package metadata
  already name.
- [ ] **PyPI ownership marker.** For a `pypi` package the registry looks for the string
  `mcp-name: <server name>` in the package's README on PyPI. NovaFabric's PyPI long description is
  `README.md`, rewritten at build time by `scripts/pypi_readme_hook.py`. Check that the marker
  survives that hook in the built wheel's metadata, not only in the repository.
- [ ] **`repository.url`** points at the public repository.

## 4. Version parity

All of these must name the same version on the day of submission:

- [ ] the PyPI release;
- [ ] `version` and the package `version` in the registry metadata;
- [ ] `serverInfo.version` in the server's `initialize` answer (it reports `novafabric.__version__`);
- [ ] the GitHub Release and tag;
- [ ] the install line in the README and in [the client guide](mcp-client-setup.md), which today
  says to install from `main` and must be changed to the release.

## 5. Evidence to keep with the submission

- [ ] The output of the offline walkthrough, run from the released wheel in a clean environment.
- [ ] The client verification table, re-run against the released version.
- [ ] The CI run that tested the release's MCP server.

## 6. Before pressing submit

- [ ] Every unchecked item above is either checked or explicitly accepted by the owner.
- [ ] The submission is made by the owner, by hand, with the registry's own publishing tool. It is
  not wired into a release workflow.
