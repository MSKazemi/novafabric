# Connecting an MCP client to the evidence server

**Status: experimental** (unreleased). This page shows how to point an MCP client at the read-only
[MCP evidence server](mcp-evidence-server.md), what the client can and cannot get from it, and how
to check offline that it tells a sealed capsule from a tampered one. Each client section says
whether it was verified, and how.

The server is not in any published release yet: the module ships after v0.109.0, so
`pip install novafabric` from PyPI does not include it. Install from a checkout of the `main`
branch until a release includes it.

## What was verified, and with what

Checked on 2026-11-28 on Linux with Python 3.12, from a wheel built from `main` and installed into
a clean virtual environment (the version string still reads `0.109.0`):

| Client | Result | What was exercised |
|---|---|---|
| Plain JSON-RPC over a pipe (no SDK) | **verified** | `initialize`, `tools/list`, all three tools, and a refused `capsule` argument (`../etc`). The [offline walkthrough](#offline-check-a-sealed-and-a-tampered-capsule) repeats part of it. |
| MCP Python SDK 1.30.0 (`stdio_client` + `ClientSession`) | **verified** | Negotiated protocol `2025-11-25`, listed the three tools, called `list_capsules` and `verify_capsule`. |
| Claude Code 2.1.332 | **verified: connects** | `claude mcp add` registered the server and `claude mcp list` reported `✔ Connected`. Tool calls from inside a Claude Code conversation were **not** exercised. |
| Claude Desktop | **not verified with this client** | Configuration shape below is from the client's own documentation. |
| Cursor | **not verified with this client** | Same. |
| VS Code | **not verified with this client** | Same. |

"Verified" means only what the row says. If you confirm another client, a PR adding a row with the
client version and what you ran is welcome.

## Before you configure any client

You need two absolute paths.

1. **The Python interpreter of the environment where NovaFabric is installed.** MCP clients start
   the server themselves, usually without your shell's `PATH` or activated virtual environment, so
   a bare `python` often finds an interpreter without NovaFabric. From the environment that has
   NovaFabric, print the interpreter path and check that it can start the server:

   ```bash
   python -c "import sys; print(sys.executable)"
   /abs/path/to/venv/bin/python -m novafabric.mcp.evidence_server --help
   ```

   The second command prints `usage: python -m novafabric.mcp.evidence_server [-h] --scope DIR`
   and exits 0.

2. **The scope directory**, the directory whose *direct child directories* are capsules.
   `nova capture` writes to `$NOVAFABRIC_HOME/capsules`, which is `~/.novafabric/capsules` unless
   you set `NOVAFABRIC_HOME`, or to the `--output-dir` you pass it. Give the absolute path:
   **a client config file does not expand `~`**, and the server refuses `~/.novafabric/capsules`
   as "not a directory". Repeat `--scope` for more directories (at most 16). The filesystem root
   is refused.

The server name used below, `novafabric-evidence`, is only a label in your client's config. It
matches the `serverInfo.name` the server reports, but any name works.

## Claude Code

**Verified: connects** (Claude Code 2.1.332). Register the server and check it:

```bash
claude mcp add novafabric-evidence -- /abs/path/to/venv/bin/python \
  -m novafabric.mcp.evidence_server --scope /abs/path/to/capsules
claude mcp list
```

`claude mcp list` starts the server, runs the MCP handshake and prints
`novafabric-evidence: … - ✔ Connected`.

Two details that matter:

- **The `--` is required.** Everything after it is the server's command line. Without it,
  `claude mcp add` reads `-m` as one of its own options and stops with `error: unknown option '-m'`.
- **`claude mcp add` has its own `-s, --scope`** (`local`, `user` or `project`): where Claude Code
  stores the entry. It is unrelated to the server's `--scope DIR`. Put Claude Code's options before
  the server name, for example `claude mcp add -s user novafabric-evidence -- …`.

The default `local` scope keeps the entry in `~/.claude.json`, for the current project only.
`-s project` writes a `.mcp.json` in the project directory instead. This is the file Claude Code
wrote:

```json
{
  "mcpServers": {
    "novafabric-evidence": {
      "type": "stdio",
      "command": "/abs/path/to/venv/bin/python",
      "args": ["-m", "novafabric.mcp.evidence_server", "--scope", "/abs/path/to/capsules"],
      "env": {}
    }
  }
}
```

A `.mcp.json` is meant to be committed, and it would publish your interpreter and capsule paths. For
a personal setup, keep the default `local` scope.

Remove the entry with `claude mcp remove novafabric-evidence -s local` (or the scope you added it with).

## Claude Desktop

**Not verified with this client.** Claude Desktop reads local servers from
`claude_desktop_config.json` (`~/Library/Application Support/Claude/` on macOS,
`%APPDATA%\Claude\` on Windows), in the `mcpServers` shape. Its documentation asks for absolute
paths. See [Connect to local MCP servers](https://modelcontextprotocol.io/docs/develop/connect-local-servers).

```json
{
  "mcpServers": {
    "novafabric-evidence": {
      "command": "/abs/path/to/venv/bin/python",
      "args": ["-m", "novafabric.mcp.evidence_server", "--scope", "/abs/path/to/capsules"]
    }
  }
}
```

## Cursor

**Not verified with this client.** Cursor reads `.cursor/mcp.json` in a project, or
`~/.cursor/mcp.json` for every project, in the same `mcpServers` shape as the Claude Desktop
example above. Its documentation lists variables such as `${userHome}` that Cursor substitutes in
`args`; the server itself never expands them. See
[Cursor's MCP documentation](https://cursor.com/docs/context/mcp).

## VS Code

**Not verified with this client.** VS Code's MCP configuration format has changed between releases
(the editor-managed `servers` shape and the `mcpServers` shape both appear in its documentation).
Use the shape that the [VS Code MCP configuration reference](https://code.visualstudio.com/docs/copilot/reference/mcp-configuration)
gives for your version, with `"type": "stdio"`, the same `command` and the same `args` as above.

## Any other stdio client

The server speaks the MCP stdio transport: newline-delimited JSON-RPC 2.0 on stdin and stdout. It
prints nothing but protocol messages on stdout, logs to stderr, and exits 0 when the client closes
stdin. A client that can start a command with arguments can use it. It answers `initialize`,
`ping`, `tools/list` and `tools/call`, and accepts protocol revisions `2025-11-25`, `2025-06-18`
and `2025-03-26`.

This is the client used for the "MCP Python SDK" row above (the SDK is not a NovaFabric
dependency; install `mcp` wherever you run the client):

```python
import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main(python: str, scope: str) -> None:
    server = StdioServerParameters(
        command=python, args=["-m", "novafabric.mcp.evidence_server", "--scope", scope]
    )
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print("protocol", init.protocolVersion)
            listing = await session.call_tool("list_capsules", {})
            for entry in listing.structuredContent["capsules"]:
                result = await session.call_tool("verify_capsule", {"capsule": entry["capsule"]})
                print(entry["capsule"], result.structuredContent["valid"])


asyncio.run(main(sys.argv[1], sys.argv[2]))
```

Run it as `python client.py /abs/path/to/venv/bin/python /abs/path/to/capsules`.

## What the server exposes, and what it never does

It exposes three tools, each annotated `readOnlyHint: true`, `destructiveHint: false`,
`idempotentHint: true` and `openWorldHint: false`:

| Tool | Gives the client |
|---|---|
| `list_capsules` | Capsule directory names under the scope directories, and whether each has a seal (not whether it verifies). |
| `get_capsule_summary` | Allowlisted manifest fields (run id, status, times, exit code, call counts), the names of withheld fields, and the evidence file names. It does not check the seal. |
| `verify_capsule` | An offline seal check of one capsule, with each check's result and the reasons for a failure. |

It never:

- returns prompts, responses, tool arguments or tool results, with or without an option;
- returns the command line, working directory, host name, git details, inputs, outputs or
  extension blocks (it names them in `withheld_fields`);
- reads outside the scope directories, follows a symbolic link to a capsule, or accepts a `capsule`
  value that is more than one path segment;
- writes a file, starts a subprocess, opens a database or uses the network;
- offers MCP resources, prompts or sampling.

Every returned string goes through the capture redaction rules and is cut at 512 characters, and
scope paths are shown as `<scope N>`. The full contract, error codes and limits are in the
[server reference](mcp-evidence-server.md).

**Privacy: what you still share.** The client's model reads whatever a tool returns. When that model
is a hosted service, capsule names, run ids, timestamps, exit codes, call counts, evidence file
names and verification results leave your machine with the conversation. Point `--scope` only at
directories whose metadata you are willing to share that way.

## Evidence verification is not MCP traffic recording

NovaFabric has two MCP surfaces that are easy to confuse:

| | `nova mcp-proxy` | This evidence server |
|---|---|---|
| Purpose | Record what an agent does through *another* MCP server | Let a client inspect and verify capsules that already exist |
| Position | Sits between a client and an upstream server, passes traffic through | Is the server; there is no upstream |
| Writes | A capsule of the recorded traffic | Nothing |
| Returns content | Forwards the upstream server's results to the client | Never; metadata and verification results only |

So connecting a client to the evidence server does not record anything about that client's
session, and recording a session with `nova mcp-proxy` does not verify anything. A
`verify_capsule` result of `"valid": true` says the capsule's evidence files are unchanged since
the holder of the signing key sealed them. With a local `nova seal init` key, the identity is
self-asserted (`identity_trust: "self-asserted"`), and the seal does not say who that holder is. For
a check against a pinned CA or timestamp authority, run `nova verify` with `--ca-bundle` and
`--tsa-ca-bundle` yourself.

## Offline check: a sealed and a tampered capsule

This walkthrough needs no model, no API key and no network. It was run as written with
networking disabled (`unshare -rn` on Linux). It uses a throwaway `NOVAFABRIC_HOME`, so your own
capsules and keys are not touched. Run it with the NovaFabric environment active, so that `nova` and
`python` are the ones from that environment.

```bash
export NOVAFABRIC_HOME="$(mktemp -d)"
nova seal init                                   # local, self-asserted signing key
nova capture -- python3 -c "print('hello')"
nova capture -- python3 -c "print('hello again')"

CAPS="$NOVAFABRIC_HOME/capsules"
GOOD=$(ls "$CAPS" | sort | head -n 1)
BAD=$(ls "$CAPS" | sort | tail -n 1)
echo '{"event":"tampered"}' >> "$CAPS/$BAD/trace.jsonl"   # edit after sealing

cat > requests.jsonl <<JSON
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_capsules","arguments":{}}}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"verify_capsule","arguments":{"capsule":"$GOOD"}}}
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"verify_capsule","arguments":{"capsule":"$BAD"}}}
JSON
python -m novafabric.mcp.evidence_server --scope "$CAPS" < requests.jsonl > responses.jsonl

python - <<'PY'
import json
for line in open("responses.jsonl"):
    msg = json.loads(line)
    if msg["id"] == 1:
        print("protocol", msg["result"]["protocolVersion"])
        continue
    body = msg["result"]["structuredContent"]
    if "capsules" in body:
        print("capsules", body["count"])
    else:
        print(body["capsule"], "valid =", body["valid"], body["errors"])
PY
```

The output (capsule names are new ULIDs on every run):

```text
protocol 2025-11-25
capsules 2
01M8HHJN44KS7M2BHFY59QEX3D valid = True []
01M8HHJSQGGJDS1F8F4ACKWNGQ valid = False ['evidence file modified after sealing: trace.jsonl']
```

`nova verify "$CAPS/$GOOD"` exits 0 and `nova verify "$CAPS/$BAD"` exits 1 on the same pair. The
tampered capsule's full result keeps `signature_ok: true` (the signed manifest itself is intact)
and reports `capsule_binding_ok: false`: the evidence no longer matches what was signed.

Of the five lines in `requests.jsonl`, one is a notification, which gets no response, so
`responses.jsonl` has four lines.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `claude mcp add` stops with `error: unknown option '-m'` | The `--` before the server command is missing. |
| The client reports the server failed to connect or the connection closed (Claude Code: `✘ Failed to connect`) | The server exited at startup. Run the exact `command` and `args` from your config in a terminal; the reason is on stderr. The two common ones are below. |
| `No module named 'novafabric'`, or `No module named novafabric.mcp.evidence_server` | The first: the `command` is an interpreter without NovaFabric. The second: it has a release that predates the server (v0.109.0 and earlier). Use the absolute interpreter path from [Before you configure any client](#before-you-configure-any-client), from an install of `main`. |
| `error: scope '…' is not a directory` (exit 2) | The path does not exist, or it starts with `~` or holds a variable the client did not expand. Use an absolute path. |
| `error: scope '/' is the filesystem root` (exit 2) | Name the directory that holds your capsules instead. |
| `list_capsules` returns `count: 0` | The scope is one level too high or too low. It must be the directory whose direct children are capsule directories, such as `~/.novafabric/capsules`, not `~/.novafabric` and not one capsule. |
| A tool returns `isError: true` with `invalid_argument` | `capsule` must be the name exactly as `list_capsules` returns it: one path segment, no `/` or `..`. |
| `ambiguous_capsule` | Two scope directories hold a capsule of that name. Pass `root`, the index of the scope in the order you gave them. |
| `"valid": false` | The seal did not check out, or there is none (`sealed: false`). `errors` says why; `nova verify <capsule-dir>` gives the same verdict with more detail. |
| The client shows no prompts or resources | Expected: the server offers tools only. |

## Related

- [MCP evidence server reference](mcp-evidence-server.md): tool arguments, results, errors, limits.
- [MCP Registry submission checklist](mcp-registry-checklist.md): what must be true before the
  server is listed in a registry.
