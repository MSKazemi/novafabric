#!/usr/bin/env python3
"""Generate `generatedCommands.ts` from the live nova Typer app.

The dashboard's CommandsTab (Layer C copy-only builder, ADR-0027) mirrors the
`nova` CLI: every command appears as a fillable form that produces a copyable
command string. Hand-curated defs in `commandRegistry.ts` cover the most-used
commands with rich hints; this script fills in *every other* command so the
dashboard covers the complete CLI surface — and stays in sync when commands are
added or removed.

Usage:
    uv run python web/scripts/gen-command-registry.py

Writes: web/src/components/dashboard/commands/generatedCommands.ts

A pytest guard (tests/serve/test_command_registry_coverage.py) re-runs this
introspection and fails if the checked-in file drifts from the CLI.
"""
from __future__ import annotations

import json
from pathlib import Path

import click
import typer.main

from novafabric.cli.main import app

OUT = (
    Path(__file__).resolve().parent.parent
    / "src/components/dashboard/commands/generatedCommands.ts"
)

# First token after `nova` -> journey bucket. NOTE: these 4 journeys group
# commands INSIDE the Commands tab; they are unrelated to (and need not track)
# the sidebar's 7 navigation groups.
FAMILY_JOURNEY: dict[str, str] = {
    # Debug & Investigate
    "capture": "debug", "replay": "debug", "diff": "debug", "verify": "debug",
    "validate": "debug", "query": "debug", "trend": "debug", "diagnose": "debug",
    "scan-secrets": "debug", "assure": "debug", "inspect": "debug", "list": "debug",
    "report": "debug", "graph": "debug", "run": "debug", "session": "debug",
    "experiment": "debug", "media": "debug", "comment": "debug", "schema": "debug",
    "view": "debug", "new-run-id": "debug", "cost": "debug", "prompt": "debug",
    # Govern & Promote
    "register": "govern", "promote": "govern", "approve": "govern",
    "rollback": "govern", "unregister": "govern", "label": "govern",
    "eval": "govern", "annotate": "govern", "score": "govern", "policy": "govern",
    "asset": "govern", "suggest-register": "govern", "incident": "govern",
    "classify": "govern", "safety-case": "govern", "dataset": "govern",
    # Audit & Verify
    "verify-envelope": "audit", "redact": "audit", "subject-proof": "audit",
    "evidence": "audit", "audit": "audit", "seal": "audit", "lineage": "audit",
    "lineage-store": "audit", "euaiact": "audit", "aibom": "audit",
    "erasure": "audit", "pii": "audit", "energy": "audit", "ledger": "audit",
    "kg": "audit", "retention": "audit", "hold": "audit", "mcp": "audit",
    # Infrastructure
    "init": "infra", "serve": "infra", "server": "infra", "doctor": "infra",
    "login": "infra", "logout": "infra", "daemon": "infra", "collector": "infra",
    "storage": "infra", "db": "infra", "rebuild-metadata-db": "infra",
    "ingest-capsule": "infra", "api-proxy": "infra", "mcp-proxy": "infra",
    "pricing": "infra",
}
# Prefixes that mean infra even without an exact family match.
INFRA_PREFIXES = ("migrate", "export")  # export* -> audit override below


def journey_for(path: str) -> str:
    tokens = path.split(" ")  # ['nova', 'seal', 'sign']
    family = tokens[1] if len(tokens) > 1 else ""
    if family in FAMILY_JOURNEY:
        return FAMILY_JOURNEY[family]
    if family.startswith("export"):
        return "audit"
    if family.startswith("migrate"):
        return "infra"
    return "infra"


def field_type(p: click.Parameter) -> str:
    if getattr(p, "is_flag", False):
        return "toggle"
    if isinstance(p.type, click.Choice):
        return "select"
    tname = getattr(p.type, "name", "text")
    if tname in {"integer", "int", "float"}:
        return "number"
    return "text"


def stringify_default(default) -> str | None:
    if default is None or default is False:
        return None
    if default is True:
        return "true"
    if isinstance(default, (str, int, float)):
        return str(default)
    return None


def make_field(p: click.Parameter) -> dict | None:
    if p.name in {"help", "version"}:
        return None
    kind = getattr(p, "param_type_name", "option")
    ftype = field_type(p)
    positional = kind == "argument"

    # Resolve the canonical long option string and the field key.
    opt = None
    for o in getattr(p, "opts", []) or []:
        if o.startswith("--"):
            opt = o
            break
    if opt is None and getattr(p, "opts", None):
        opt = p.opts[0]

    if positional:
        key = (p.name or "arg").replace("_", "-")
        label = (p.metavar or p.name or "arg").upper() if hasattr(p, "metavar") else key
        # click Arguments don't reliably expose metavar via attribute; keep name.
        label = key
    else:
        key = opt[2:] if opt and opt.startswith("--") else (p.name or "opt").replace("_", "-")
        label = opt or f"--{key}"

    field: dict = {
        "key": key,
        "label": label,
        "type": ftype,
        "hint": "",
    }

    help_txt = getattr(p, "help", None) or ""
    if positional:
        field["positional"] = True
        field["hint"] = f"Positional argument <{p.name}>."
    else:
        field["flag"] = opt or f"--{key}"
        hint = help_txt
        if getattr(p, "multiple", False):
            hint = (hint + " (repeatable — add more in your terminal)").strip()
        field["hint"] = hint or f"Option {field['flag']}."

    if getattr(p, "required", False):
        field["required"] = True

    if ftype == "select":
        choices = list(p.type.choices)  # type: ignore[union-attr]
        dv = stringify_default(p.default)
        if not p.required:
            # allow leaving unset (builder omits empty values)
            choices = ["", *choices]
            field["defaultValue"] = dv or ""
        elif dv:
            field["defaultValue"] = dv
        field["options"] = choices
    else:
        dv = stringify_default(p.default)
        if dv is not None and not positional:
            field["defaultValue"] = dv

    return field


def walk(cmd: click.Command, path: str, out: list) -> None:
    if isinstance(cmd, click.Group):
        for name, sub in sorted(cmd.commands.items()):
            walk(sub, f"{path} {name}", out)
        return
    if getattr(cmd, "hidden", False):
        return
    fields = []
    for p in cmd.params:
        f = make_field(p)
        if f is not None:
            fields.append(f)
    desc = (cmd.help or cmd.short_help or "").strip().split("\n")[0]
    out.append({
        "id": path.replace(" ", "-"),
        "name": path,
        "journey": journey_for(path),
        "description": desc or f"Run `{path}`. See the CLI reference for details.",
        "fields": fields,
        "docsPath": "/spec",
        "generated": True,
    })


def main() -> None:
    root = typer.main.get_command(app)
    out: list = []
    walk(root, "nova", out)
    out.sort(key=lambda r: r["name"])

    body = json.dumps(out, indent=2, ensure_ascii=False)
    header = (
        "// AUTO-GENERATED by web/scripts/gen-command-registry.py — DO NOT EDIT.\n"
        "// Mirrors the complete `nova` CLI surface into the dashboard CommandsTab.\n"
        "// Regenerate: uv run python web/scripts/gen-command-registry.py\n"
        f"// Commands: {len(out)}\n\n"
        "import type { CommandDef } from './commandRegistry';\n\n"
        "export const GENERATED_COMMANDS: readonly CommandDef[] = "
    )
    OUT.write_text(header + body + ";\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(out)} commands)")


if __name__ == "__main__":
    main()
