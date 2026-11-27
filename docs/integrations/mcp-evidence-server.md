# Read-only MCP evidence server (stdio)

**Status: experimental** (unreleased; tracked in
[issue #244](https://github.com/MSKazemi/novafabric/issues/244)). The tool names, arguments and
result fields may still change.

The evidence server lets an MCP client, such as an IDE assistant or an agent host, look at local
Run Capsules: list them, read a summary of one, and verify one's seal offline. It runs as a local
process that the client starts and talks to over stdin/stdout (the MCP stdio transport). It has no
network listener and needs no server deployment.

It is a different thing from the other MCP surfaces in NovaFabric:

| Surface | What it does |
|---|---|
| `nova mcp-proxy` | Records the traffic between an MCP client and *another* MCP server into a capsule. |
| `nova mcp scan` | Scans an MCP server manifest for supply-chain risks. |
| SEP-1649 Server Card (`/.well-known/mcp.json` on `nova serve`) | Describes the `nova serve` HTTP surface. It does **not** advertise this stdio server. |
| **This server** | Answers read-only questions about capsules already on disk. |

## Start it

There is no `nova` sub-command for it yet. Run the module with the Python environment that has
NovaFabric installed:

```bash
python -m novafabric.mcp.evidence_server --scope ~/.novafabric/capsules
```

`--scope` is required, and it has no default. Each `--scope` directory is one whose **direct
child directories** are capsules (`~/.novafabric/capsules` is where `nova capture` writes by
default). Repeat `--scope` to allow more than one directory, up to 16. Each is resolved once to its
real path when the server starts. The filesystem root is refused.

The process prints protocol messages on stdout and nothing else. Warnings and errors go to stderr.
It exits 0 when the client closes stdin, and 2 when a `--scope` is not a usable directory.

### Client configuration

Most MCP clients take a command and its arguments. In the common `mcpServers` JSON shape:

```json
{
  "mcpServers": {
    "novafabric-evidence": {
      "command": "python",
      "args": ["-m", "novafabric.mcp.evidence_server", "--scope", "/abs/path/to/capsules"]
    }
  }
}
```

Use the absolute path of the Python interpreter and of the scope directory if the client does not
start from your shell's working directory or `PATH`. The test suite drives the server with the
official MCP Python SDK client. Other clients have not been tested.

## Tools

All three tools are declared `readOnlyHint: true`, `destructiveHint: false`,
`openWorldHint: false`, and accept no arguments other than the ones listed.

| Tool | Arguments | Returns |
|---|---|---|
| `list_capsules` | `root` (scope index, optional), `cursor` (optional), `limit` (1–200, default 50) | `capsules`: `{root, capsule, sealed}` per capsule, `count`, `next_cursor` (`null` on the last page) and `skipped_unaddressable`, the number of capsule directories skipped because their name could not be passed back as a `capsule` argument (not valid UTF-8, or a control or invisible character). `sealed` means a seal is present, not that it verifies. |
| `get_capsule_summary` | `capsule`, `root` (optional) | Allowlisted manifest fields (`fields`), the names of the fields it withheld (`withheld_fields`), the evidence file names the seal covers (`evidence_files`), `manifest_source` and `sealed`. `signature_checked` is always `false`. |
| `verify_capsule` | `capsule`, `root` (optional) | `valid`, `sealed`, `signature_ok`, `capsule_binding_ok`, `capsule_id_ok`, `timestamp_ok`, `timestamp_trust`, `log_inclusion`, `identity_trust`, `unanchored_claims`, `errors`, and `verification_level`, which says what was checked. |

`capsule` is the capsule's directory name exactly as `list_capsules` returns it. `root` is the
index of a `--scope` directory in the order you gave them. You need `root` only when two scope
directories hold a capsule of the same name.

A sealed capsule's summary is read from the signed payload in `.seal/manifest.dsse`, not from
`capsule.yaml`, so an edited `capsule.yaml` cannot change it. The summary still does not check the
signature. Only `verify_capsule` returning `"valid": true` means the seal checked out.

### What `verify_capsule` checks

It runs the same verification as `nova verify` on a machine with no `novaseal.yaml`. That covers
the DSSE signature, the binding of the seal to this directory and its evidence digests, and the
Merkle inclusion proof and RFC 3161 token that the capsule carries, if any. It does not use a CA
bundle, a TSA trust anchor or the sealer's Merkle log, so signer identity and timestamp are at most
self-asserted. `unanchored_claims` lists what rests on no anchor, in the same terms as
`nova verify --json`. For anchored checks, run `nova verify --ca-bundle … --tsa-ca-bundle …`
yourself.

This is a real result for a freshly sealed capsule (the `structuredContent` of the call):

```json
{"root": 0, "capsule": "b", "verification_level": "offline, from the capsule alone: DSSE signature, seal-to-directory binding and evidence digests, a carried Merkle inclusion proof and RFC 3161 token if present; no CA bundle, TSA anchor or sealer Merkle log is consulted, so signer identity and timestamp are at most self-asserted (see unanchored_claims)", "sealed": true, "valid": true, "signature_ok": true, "capsule_binding_ok": true, "capsule_id_ok": true, "timestamp_ok": null, "timestamp_trust": "absent", "log_inclusion": "carried-proof", "identity_trust": "self-asserted", "unanchored_claims": ["identity-self-asserted", "log-head-unanchored"], "errors": [], "errors_truncated": false}
```

An unsealed capsule, a capsule edited after sealing, a corrupted envelope, a `.seal` that is a
symbolic link and a link planted anywhere in the capsule all return `"valid": false`, with the
reasons in `errors`.

## Access scope and what is never returned

- **Only direct children of a scope directory.** A `capsule` value must be a single path segment:
  no `/`, `\`, `..`, NUL, control or invisible formatting character, and no absolute path. A
  capsule entry that is a symbolic link is refused (`outside_scope`), even one that points to
  another capsule in the same scope, and `list_capsules` never lists one. Files inside a capsule
  are opened without following links.
- **No content.** No tool returns prompts, responses, tool arguments or tool results. The summary
  never returns `command`, `working_directory`, `host`, `metadata`, `error`, `git`, `inputs`,
  `outputs` or extension blocks, because they can hold command-line secrets, local paths, host
  names or free text. It names them in `withheld_fields` instead.
- **Redacted and bounded.** Every returned string goes through the same secret-redaction rules as
  capsule capture (`[REDACTED:<rule>]`) and is cut at 512 characters. Lists and mappings are
  capped at 200 entries, and nesting at 3 levels. The scope directories' absolute paths are
  replaced by `<scope N>`.
- **Read only.** The server does not open any file for writing, start a subprocess, open a
  database or Merkle log, or use the network. A test runs it as a process and checks that the
  scope directory and an isolated `HOME` are unchanged afterwards.

## Errors

Protocol problems are JSON-RPC errors: `-32700` (not JSON, or nested too deeply to parse), `-32600` (invalid request, a batch,
or a message over 1 MiB), `-32601` (unknown method), `-32602` (unknown tool, or non-object
`params` or `arguments`), `-32603` (internal error), and `-32002` (a request other than
`initialize` or `ping` before `initialize`).

A tool call the server refuses returns a normal result with `isError: true` and
`{"error": {"code": …, "message": …}}`, as JSON text and, from protocol 2025-06-18 on, as
`structuredContent`:

| `code` | Meaning |
|---|---|
| `invalid_argument` | An argument has the wrong type or range, is unknown, or a `capsule` is not one path segment. |
| `outside_scope` | The capsule entry is a symbolic link or resolves outside its scope directory. |
| `capsule_not_found` | No capsule directory of that name in scope. |
| `ambiguous_capsule` | More than one scope directory holds that name. Pass `root`. |
| `capsule_unreadable` | The manifest is present but unusable (a link, a special file, too large, or not a mapping). |
| `response_too_large` | The result would exceed 512 KiB. Ask for a smaller page. |
| `internal_error` | The tool failed. The detail is in the server's stderr, not in the result. |

## Protocol versions

The server accepts MCP protocol revisions `2025-11-25`, `2025-06-18` and `2025-03-26`. If a client
asks for any other revision, the server answers with `2025-11-25`, and the client decides whether
to continue. That is standard MCP version negotiation. It implements `initialize`, `ping`,
`tools/list` and `tools/call` only, with no resources, prompts, sampling or subscriptions. JSON-RPC
batches are refused. Responses are written as ASCII-only JSON, so a non-ASCII or lone-surrogate
value in a request or a file name is escaped, never a reason for the process to stop. A request
the server cannot handle is answered with an error, and the next line is still read.

## Not in this slice

The following are **not implemented**: structural diff and lineage tools, any tool that returns
prompt or response content (even with an opt-in), a `nova` CLI entry point, per-call timeouts, the
Streamable HTTP transport, and the 2026-07-28 protocol revision. Verification of a very large
capsule runs to completion; there is no time limit on it yet.
