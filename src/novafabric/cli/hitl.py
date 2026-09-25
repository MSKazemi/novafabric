# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""``nova hitl`` — read-only human-agent accountability views (experimental, ADR-0150).

Every subcommand is read-only: it loads ``capsule.yaml`` from a capsule
directory (or a run id), never writes, and prints the record-only notice. The
views cover the conversation thread (NF-181), decision-context receipts
(NF-182), overrides (NF-187) and surfaced rationale (NF-188).

``nova hitl context verify`` exit codes: 0 every receipt re-performs, 1 at
least one receipt (or the capsule) is defective, 2 nothing to check.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
import yaml
from rich.console import Console

from novafabric.cli._capsule_ref import CapsuleRefError, resolve_capsule_ref
from novafabric.hitl._records import RecordDefect, dangling_refs, duplicate_turn_ids
from novafabric.hitl.conversation import (
    ConversationFacet,
    broken_parent_refs,
    facet_from_capsule,
    resolve_turn,
)
from novafabric.hitl.decision_context import (
    ReceiptVerdict,
    receipts_for_turn,
    verify_decision_contexts,
)
from novafabric.hitl.override import load_overrides
from novafabric.hitl.rationale import load_rationales

console = Console()
err_console = Console(stderr=True)

#: capsule.yaml is a manifest, not a data file; anything larger is refused
#: before parsing so a hostile capsule cannot make a read-only view expensive.
MAX_MANIFEST_BYTES = 16 * 1024 * 1024

NOTICE = (
    "Record-only: NovaFabric records what was shown, decided, overridden and "
    "surfaced. It does not adjudicate the decision, grant or deny any right, or "
    "certify that human oversight was adequate or lawful (ADR-0150 D7)."
)

EXIT_OK = 0
EXIT_DEFECTIVE = 1
EXIT_NOTHING = 2

_CAPSULE_HELP = "Capsule directory or run id."
_JSON_HELP = "Emit deterministic JSON instead of a table."

app = typer.Typer(
    help=(
        "Human-agent accountability evidence: conversation thread, decision-context "
        "receipts, overrides, rationale — read-only (experimental, ADR-0150)."
    ),
    no_args_is_help=True,
)
thread_app = typer.Typer(help="Conversation-thread provenance (NF-181).", no_args_is_help=True)
context_app = typer.Typer(
    help="Decision-context receipts — what the human saw (NF-182).", no_args_is_help=True
)
override_app = typer.Typer(help="Human-override provenance (NF-187).", no_args_is_help=True)
rationale_app = typer.Typer(
    help="Agent rationale surfaced to the human (NF-188).", no_args_is_help=True
)
app.add_typer(thread_app, name="thread")
app.add_typer(context_app, name="context")
app.add_typer(override_app, name="override")
app.add_typer(rationale_app, name="rationale")


class CapsuleLoadError(Exception):
    """The capsule manifest could not be located, read, or parsed."""


# ── Helpers ───────────────────────────────────────────────────────────────


def _load_capsule(ref: str) -> dict[str, Any]:
    """Return the parsed ``capsule.yaml`` for ``ref`` (bounded, read-only)."""
    try:
        capsule_dir = resolve_capsule_ref(ref)
    except CapsuleRefError as exc:
        raise CapsuleLoadError(str(exc)) from exc
    manifest = Path(capsule_dir) / "capsule.yaml"
    try:
        size = manifest.stat().st_size
        if size > MAX_MANIFEST_BYTES:
            raise CapsuleLoadError(
                f"capsule.yaml is {size} bytes, over the {MAX_MANIFEST_BYTES}-byte limit"
            )
        with manifest.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise CapsuleLoadError(f"cannot read {manifest}: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise CapsuleLoadError(f"{manifest} is not a mapping")
    return data


def _load_facet(capsule: dict[str, Any]) -> ConversationFacet | None:
    """Parse ``facets.conversation``; a malformed facet is a load error."""
    try:
        return facet_from_capsule(capsule)
    except Exception as exc:  # noqa: BLE001 — surfaced, fail-closed
        raise CapsuleLoadError(f"facets.conversation is malformed ({type(exc).__name__})") from exc


def _load_or_exit(ref: str) -> tuple[dict[str, Any], ConversationFacet | None]:
    try:
        capsule = _load_capsule(ref)
        return capsule, _load_facet(capsule)
    except CapsuleLoadError as exc:
        err_console.print(f"[red]Error:[/red] {exc}", markup=True, highlight=False)
        raise typer.Exit(EXIT_DEFECTIVE) from exc


def _emit_json(payload: dict[str, Any]) -> None:
    payload = {**payload, "notice": NOTICE}
    typer.echo(json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False))


def _notice() -> None:
    console.print(f"[dim]{NOTICE}[/dim]", highlight=False)


def _defects_json(defects: list[RecordDefect]) -> list[dict[str, Any]]:
    return [{"index": d.index, "error": d.error} for d in defects]


def _print_defects(defects: list[RecordDefect], kind: str) -> None:
    for d in defects:
        where = "list" if d.index < 0 else f"entry {d.index}"
        console.print(f"  [red]DEFECT[/red] {kind} {where}: {d.error}", highlight=False)


# ── thread ────────────────────────────────────────────────────────────────


@thread_app.command("show")
def thread_show(
    capsule: str = typer.Option(..., "--capsule", help=_CAPSULE_HELP),
    as_json: bool = typer.Option(False, "--json", help=_JSON_HELP),
) -> None:
    """Show the conversation thread: turns in order, pseudonymous authors.

    Exits 1 when a parent_turn_id does not resolve or a turn id repeats.

    \b
    Examples:
      nova hitl thread show --capsule 01KZ...
      nova hitl thread show --capsule ./capsule-dir --json
    """
    _, facet = _load_or_exit(capsule)
    if facet is None:
        if as_json:
            _emit_json({"conversation": None})
        else:
            console.print("No conversation facet in this capsule.")
            _notice()
        raise typer.Exit(EXIT_OK)
    broken = broken_parent_refs(facet)
    dupes = duplicate_turn_ids(facet)
    turns = [t.model_dump(exclude_none=True) for t in facet.turns]
    if as_json:
        _emit_json(
            {
                "session_ref": facet.session_ref,
                "turns": turns,
                "broken_parent_refs": broken,
                "duplicate_turn_ids": dupes,
            }
        )
    else:
        console.print(f"Conversation: {len(turns)} turn(s)", highlight=False)
        for t in facet.turns:
            parent = f" <- {t.parent_turn_id}" if t.parent_turn_id else ""
            flag = " [red]BROKEN PARENT[/red]" if t.turn_id in broken else ""
            console.print(
                f"  {t.turn_id}{parent}  {t.role}  {t.author}  {t.at}  {t.content_digest}{flag}",
                highlight=False,
            )
        for d in dupes:
            console.print(f"  [red]DUPLICATE turn_id[/red] {d}", highlight=False)
        _notice()
    raise typer.Exit(EXIT_DEFECTIVE if broken or dupes else EXIT_OK)


# ── context ───────────────────────────────────────────────────────────────


@context_app.command("show")
def context_show(
    capsule: str = typer.Option(..., "--capsule", help=_CAPSULE_HELP),
    turn: str = typer.Option(..., "--turn", help="Deciding turn_id."),
    as_json: bool = typer.Option(False, "--json", help=_JSON_HELP),
) -> None:
    """Show what the human saw when deciding at a turn, with an offline re-check.

    Exits 1 when the turn does not resolve, no receipt exists for it, or the
    stored context_root does not match the shown items.

    \b
    Examples:
      nova hitl context show --capsule 01KZ... --turn t7
    """
    cap, facet = _load_or_exit(capsule)
    resolves = facet is not None and resolve_turn(facet, turn) is not None
    found = receipts_for_turn(cap, turn)
    verification = verify_decision_contexts(cap)
    by_index: dict[int, ReceiptVerdict] = {v.index: v for v in verification.verdicts}
    entries = [
        {"receipt": rec.model_dump(exclude_none=True), "verified": by_index[i].to_dict()}
        for i, rec in found
    ]
    ok = resolves and bool(entries) and all(by_index[i].ok for i, _ in found)
    if as_json:
        _emit_json({"turn": turn, "turn_resolves": resolves, "receipts": entries, "ok": ok})
    else:
        if not resolves:
            console.print(f"[red]Dangling turn_ref:[/red] {turn}", highlight=False)
        if not entries:
            console.print(f"No decision-context receipt for turn {turn}.", highlight=False)
        for i, rec in found:
            verdict = by_index[i]
            console.print(
                f"Turn {rec.turn_ref}: decision={rec.decision} reason={rec.reason} "
                f"decided_by={rec.decided_by}",
                highlight=False,
            )
            for pos, item in enumerate(rec.shown_context):
                console.print(
                    f"  [{pos}] {item.item_kind}  {item.item_digest}  {item.rendered_at}",
                    highlight=False,
                )
            status = "[green]MATCH[/green]" if verdict.root_matches else "[red]MISMATCH[/red]"
            console.print(f"  context_root {rec.context_root} {status}", highlight=False)
            if rec.nf086_approval_ref:
                console.print(f"  nf086_approval_ref {rec.nf086_approval_ref}", highlight=False)
        _notice()
    raise typer.Exit(EXIT_OK if ok else EXIT_DEFECTIVE)


@context_app.command("verify")
def context_verify(
    capsule: str = typer.Option(..., "--capsule", help=_CAPSULE_HELP),
    as_json: bool = typer.Option(False, "--json", help=_JSON_HELP),
) -> None:
    """Re-perform every decision-context receipt offline (fail-closed).

    Exit 0 all receipts re-perform; 1 any receipt is defective (dangling
    turn_ref, context_root mismatch, duplicate, malformed); 2 nothing to check.

    \b
    Examples:
      nova hitl context verify --capsule 01KZ...
    """
    cap, _ = _load_or_exit(capsule)
    result = verify_decision_contexts(cap)
    status = result.status
    if as_json:
        _emit_json(
            {
                "status": status,
                "verdicts": [v.to_dict() for v in result.verdicts],
                "defects": _defects_json(result.defects),
                "duplicate_turn_ids": result.duplicate_turn_ids,
            }
        )
    else:
        if status == "empty":
            console.print("Nothing to check: no decision-context receipts.")
        for v in result.verdicts:
            label = "[green]OK[/green]" if v.ok else "[red]DEFECTIVE[/red]"
            console.print(
                f"  {label} [{v.index}] turn={v.turn_ref} resolves={v.turn_resolves} "
                f"root_matches={v.root_matches} duplicate={v.duplicate_for_turn}",
                highlight=False,
            )
        _print_defects(result.defects, "decision_context")
        console.print(f"Status: {status}", highlight=False)
        _notice()
    code = {"ok": EXIT_OK, "defective": EXIT_DEFECTIVE, "empty": EXIT_NOTHING}[status]
    raise typer.Exit(code)


# ── override ──────────────────────────────────────────────────────────────


@override_app.command("list")
def override_list(
    capsule: str = typer.Option(..., "--capsule", help=_CAPSULE_HELP),
    as_json: bool = typer.Option(False, "--json", help=_JSON_HELP),
) -> None:
    """List human overrides of agent actions, in stored order.

    Exits 1 when an override is malformed or its turn_ref does not resolve.

    \b
    Examples:
      nova hitl override list --capsule 01KZ... --json
    """
    cap, facet = _load_or_exit(capsule)
    loaded = load_overrides(cap)
    dangling = set(dangling_refs(facet, [r.turn_ref for _, r in loaded.records]))
    rows = [
        {"index": i, **r.model_dump(exclude_none=True), "turn_resolves": r.turn_ref not in dangling}
        for i, r in loaded.records
    ]
    if as_json:
        _emit_json({"overrides": rows, "defects": _defects_json(loaded.defects)})
    else:
        console.print(f"Overrides: {len(rows)}", highlight=False)
        for i, r in loaded.records:
            flag = " [red]DANGLING turn_ref[/red]" if r.turn_ref in dangling else ""
            console.print(
                f"  [{i}] turn={r.turn_ref} overrider={r.overrider} reason={r.reason} "
                f"at={r.at}{flag}\n      {r.overridden_action_digest} -> "
                f"{r.override_action_digest}",
                highlight=False,
            )
        _print_defects(loaded.defects, "override")
        _notice()
    raise typer.Exit(EXIT_DEFECTIVE if dangling or loaded.defects else EXIT_OK)


# ── rationale ─────────────────────────────────────────────────────────────


@rationale_app.command("show")
def rationale_show(
    capsule: str = typer.Option(..., "--capsule", help=_CAPSULE_HELP),
    turn: str = typer.Option(..., "--turn", help="turn_id the rationale was surfaced at."),
    as_json: bool = typer.Option(False, "--json", help=_JSON_HELP),
) -> None:
    """Show the agent's stated reason(s) surfaced to the human at a turn.

    Records the stated reason by digest; does not judge its truthfulness.
    Exits 1 when the turn does not resolve, no rationale exists for it, or a
    stored rationale is malformed.

    \b
    Examples:
      nova hitl rationale show --capsule 01KZ... --turn t6
    """
    cap, facet = _load_or_exit(capsule)
    resolves = facet is not None and resolve_turn(facet, turn) is not None
    loaded = load_rationales(cap)
    rows = [
        {"index": i, **r.model_dump(exclude_none=True)}
        for i, r in loaded.records
        if r.turn_ref == turn
    ]
    ok = resolves and bool(rows) and not loaded.defects
    if as_json:
        _emit_json(
            {
                "turn": turn,
                "turn_resolves": resolves,
                "rationales": rows,
                "defects": _defects_json(loaded.defects),
                "ok": ok,
            }
        )
    else:
        if not resolves:
            console.print(f"[red]Dangling turn_ref:[/red] {turn}", highlight=False)
        if not rows:
            console.print(f"No rationale recorded for turn {turn}.", highlight=False)
        for row in rows:
            console.print(
                f"  [{row['index']}] model={row['model_ref']} surfaced_at={row['surfaced_at']} "
                f"stated_reason_digest={row['stated_reason_digest']}",
                highlight=False,
            )
        _print_defects(loaded.defects, "rationale")
        _notice()
    raise typer.Exit(EXIT_OK if ok else EXIT_DEFECTIVE)
