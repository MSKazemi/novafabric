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

"""``nova annotate`` sub-commands (ADR-0118, experimental).

Human annotation queues: route capsules/spans to reviewers, track each item
through its lifecycle, and land completed annotations as ``HUMAN``-source
``Score`` records in the subject capsule's ``scores.jsonl``.

    nova annotate queue create --name q1 --criteria factuality [--require-checker]
    nova annotate queue add q1 --capsule <capsule-dir>
    nova annotate queue list [--json]
    nova annotate queue show q1 [--json]
    nova annotate next --queue q1 --as reviewer:a
    nova annotate submit <item_id> --score factuality=true [--as reviewer:a]
    nova annotate confirm <item_id> --as reviewer:b     # checker step (SoD)
    nova annotate skip <item_id> [--note ...]

Annotation is off the live workload path: everything here reads stored capsules
and only ever *appends* to ``scores.jsonl``.
"""

from __future__ import annotations

import getpass
import json
from pathlib import Path
from typing import Annotated, NoReturn, Optional

import typer
from rich.console import Console
from rich.table import Table

from novafabric.eval.annotation_queue import (
    AnnotationError,
    AnnotationQueue,
    AssignmentPolicy,
    ItemState,
    QueueItem,
    SubjectSelector,
)
from novafabric.eval.score_config import ScoreConfigViolation

console = Console()

annotate_app = typer.Typer(
    name="annotate",
    help="Human annotation queues (experimental, ADR-0118).",
    no_args_is_help=True,
)
queue_app = typer.Typer(
    name="queue",
    help="Create, populate, and inspect annotation queues.",
    no_args_is_help=True,
)
annotate_app.add_typer(queue_app, name="queue")

_STATE_STYLE = {
    "pending": "yellow",
    "assigned": "cyan",
    "checker_pending": "magenta",
    "completed": "green",
    "skipped": "dim",
}


def _default_identity() -> str:
    return getpass.getuser()


def _fail(exc: Exception) -> NoReturn:
    console.print(f"[red]{exc}[/red]")
    raise typer.Exit(code=1) from exc


def _item_json(item: QueueItem) -> str:
    return json.dumps(json.loads(item.model_dump_json(exclude_none=True)), indent=2)


def _print_item(item: QueueItem) -> None:
    style = _STATE_STYLE.get(item.state.value, "white")
    console.print(
        f"item {item.item_id}  [{style}]{item.state.value}[/{style}]  "
        f"subject={item.subject}  kind={item.subject_kind}"
    )
    if item.assignee:
        console.print(f"  assignee: {item.assignee}")
    if item.checker:
        console.print(f"  checker:  {item.checker}")
    if item.resulting_score_ids:
        console.print(f"  scores:   {', '.join(item.resulting_score_ids)}")


def _parse_selector(pairs: list[str]) -> SubjectSelector:
    """Parse repeatable ``--select key=value`` pairs into a selector."""
    raw: dict[str, object] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key or not value:
            raise typer.BadParameter(f"--select {pair!r}: expected key=value")
        if key == "sample":
            try:
                raw[key] = float(value)
            except ValueError:
                raise typer.BadParameter(f"--select sample={value!r}: not a number") from None
        elif key in ("run_ids", "tool_names", "tags"):
            raw[key] = [part for part in value.split(",") if part]
        else:
            raw[key] = value
    return SubjectSelector.model_validate(raw)


def _parse_scores(pairs: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or not name:
            raise typer.BadParameter(f"--score {pair!r}: expected name=value")
        if name in values:
            raise typer.BadParameter(f"--score {pair!r}: criterion given twice")
        values[name] = value
    return values


# ── queue sub-commands ─────────────────────────────────────────────────────────


@queue_app.command("create")
def queue_create(
    name: Annotated[str, typer.Option("--name", help="Queue name (unique per store).")],
    criteria: Annotated[
        list[str],
        typer.Option(
            "--criteria",
            help="Score-config name(s) reviewers grade against "
            "(repeatable or comma-separated; ADR-0117).",
        ),
    ],
    policy: Annotated[
        AssignmentPolicy,
        typer.Option(
            "--policy",
            help="round-robin (next pending) | manual (reviewer names the item).",
        ),
    ] = AssignmentPolicy.ROUND_ROBIN,
    select: Annotated[
        Optional[list[str]],
        typer.Option(
            "--select",
            help="Subject-selector key=value (repeatable): subject_kind, "
            "run_ids, tool_names, tags, sample.",
        ),
    ] = None,
    require_checker: Annotated[
        bool,
        typer.Option(
            "--require-checker",
            help="Maker-checker: a second distinct reviewer must confirm before an item completes.",
        ),
    ] = False,
    seal: Annotated[
        bool,
        typer.Option(
            "--seal",
            help="Mark completed scores for evidence-bundle sealing (planned, P5).",
        ),
    ] = False,
    description: Annotated[
        Optional[str], typer.Option("--description", help="Free-text note.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the queue record as JSON.")
    ] = False,
) -> None:
    """Create an annotation queue. Every criterion must be a registered score config."""
    from novafabric.eval.annotation_store import create_queue

    flat = [part for entry in criteria for part in entry.split(",") if part]
    try:
        selector = _parse_selector(select or [])
        queue = create_queue(
            name=name,
            criteria=flat,
            assignment_policy=policy,
            subject_selector=selector,
            require_checker=require_checker,
            seal=seal,
            description=description,
        )
    except (AnnotationError, ValueError) as exc:
        _fail(exc)
    if json_output:
        typer.echo(queue.model_dump_json(exclude_none=True, indent=2))
        return
    checker_note = " (maker-checker)" if queue.require_checker else ""
    console.print(
        f"[green]Created[/green] queue [bold]{queue.name}[/bold]{checker_note}\n"
        f"  queue_id={queue.queue_id}\n  criteria: {', '.join(queue.criteria)}"
    )
    if queue.seal:
        console.print(
            "  [yellow]note:[/yellow] --seal recorded; evidence-bundle sealing of "
            "completed scores is planned (ADR-0118 P5). scores.jsonl is already "
            "covered by the capsule Merkle root at Evidence-Bundle time."
        )


@queue_app.command("add")
def queue_add(
    queue_ref: Annotated[str, typer.Argument(help="Queue id (ULID) or name.")],
    capsule: Annotated[
        Path,
        typer.Option(
            "--capsule",
            help="Capsule directory whose scores.jsonl receives the completed scores.",
            exists=True,
            file_okay=False,
        ),
    ],
    subject: Annotated[
        Optional[str],
        typer.Option(
            "--subject",
            help="Explicit sha256:<hex> subject digest (e.g. a span). "
            "Default: the capsule's content-addressed root digest.",
        ),
    ] = None,
    kind: Annotated[
        Optional[str],
        typer.Option("--kind", help="Subject kind: span | capsule. Default: capsule "
                     "when --subject is omitted, span otherwise."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the item record as JSON.")
    ] = False,
) -> None:
    """Enqueue one subject (a capsule, or a span within it) for human review."""
    from novafabric.eval.annotation_store import annotation_subject_digest, enqueue_item

    subject_kind = kind or ("span" if subject is not None else "capsule")
    resolved_subject = subject or annotation_subject_digest(capsule)
    try:
        item = enqueue_item(
            queue_ref,
            subject=resolved_subject,
            subject_kind=subject_kind,
            capsule_ref=str(capsule),
        )
    except (AnnotationError, ValueError) as exc:
        _fail(exc)
    if json_output:
        typer.echo(_item_json(item))
        return
    console.print(
        f"[green]Enqueued[/green] item {item.item_id} ({item.subject_kind}) "
        f"on queue {queue_ref}\n  subject={item.subject}"
    )


@queue_app.command("list")
def queue_list(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit JSON instead of a table.")
    ] = False,
) -> None:
    """List annotation queues with per-state progress."""
    from novafabric.eval.annotation_store import list_queues, queue_progress

    queues = list_queues()
    if json_output:
        payload = [
            {
                **json.loads(q.model_dump_json(exclude_none=True)),
                "progress": queue_progress(q.queue_id),
            }
            for q in queues
        ]
        typer.echo(json.dumps(payload, indent=2))
        return
    table = Table(title=f"annotation queues ({len(queues)})")
    for col in ("name", "criteria", "policy", "checker", "pending", "assigned",
                "checker_pending", "completed", "skipped"):
        table.add_column(col, overflow="fold")
    for q in queues:
        progress = queue_progress(q.queue_id)
        table.add_row(
            q.name,
            ", ".join(q.criteria),
            q.assignment_policy.value,
            "yes" if q.require_checker else "no",
            *(str(progress[s.value]) for s in ItemState),
        )
    console.print(table)


@queue_app.command("show")
def queue_show(
    queue_ref: Annotated[str, typer.Argument(help="Queue id (ULID) or name.")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit JSON instead of tables.")
    ] = False,
) -> None:
    """Show one queue: definition, progress, and its items."""
    from novafabric.eval.annotation_store import get_queue, list_items, queue_progress

    try:
        queue: AnnotationQueue = get_queue(queue_ref)
    except AnnotationError as exc:
        _fail(exc)
    items = list_items(queue.queue_id)
    if json_output:
        payload = {
            **json.loads(queue.model_dump_json(exclude_none=True)),
            "progress": queue_progress(queue.queue_id),
            "items": [json.loads(i.model_dump_json(exclude_none=True)) for i in items],
        }
        typer.echo(json.dumps(payload, indent=2))
        return
    console.print(f"[bold]{queue.name}[/bold]  ({queue.assignment_policy.value})")
    console.print(f"  queue_id: {queue.queue_id}")
    console.print(f"  criteria: {', '.join(queue.criteria)}")
    console.print(f"  maker-checker: {'required' if queue.require_checker else 'off'}")
    if queue.description:
        console.print(f"  {queue.description}")
    progress = queue_progress(queue.queue_id)
    console.print(
        "  progress: "
        + "  ".join(f"{state.value}={progress[state.value]}" for state in ItemState)
    )
    table = Table(title=f"items ({len(items)})")
    for col in ("item_id", "state", "kind", "assignee", "checker", "scores"):
        table.add_column(col, overflow="fold")
    for item in items:
        table.add_row(
            item.item_id,
            item.state.value,
            item.subject_kind,
            item.assignee or "",
            item.checker or "",
            str(len(item.resulting_score_ids)),
        )
    console.print(table)


# ── reviewer commands ──────────────────────────────────────────────────────────


@annotate_app.command("next")
def annotate_next(
    queue_ref: Annotated[
        Optional[str], typer.Option("--queue", help="Restrict to one queue (id or name).")
    ] = None,
    reviewer: Annotated[
        str,
        typer.Option(
            "--as",
            help="Reviewer identity (becomes Score.evaluator_id; defaults to OS user).",
        ),
    ] = "",
    item_id: Annotated[
        Optional[str],
        typer.Option("--item", help="Claim this specific pending item (manual policy)."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the claimed item as JSON.")
    ] = False,
) -> None:
    """Claim the next pending item (round-robin), or a named one with --item."""
    from novafabric.eval.annotation_store import claim_item, claim_next

    identity = reviewer or _default_identity()
    try:
        if item_id is not None:
            item: QueueItem | None = claim_item(item_id, identity)
        else:
            item = claim_next(identity, queue_ref=queue_ref)
    except AnnotationError as exc:
        _fail(exc)
    if item is None:
        console.print("[yellow]Queue empty[/yellow] — no pending items to claim.")
        return
    if json_output:
        typer.echo(_item_json(item))
        return
    console.print(f"[green]Claimed[/green] by {identity}:")
    _print_item(item)
    console.print(
        f"  Grade it with [bold]nova annotate submit {item.item_id} "
        f"--score <criterion>=<value>[/bold]"
    )


@annotate_app.command("submit")
def annotate_submit(
    item_id: Annotated[str, typer.Argument(help="Item id (ULID) to grade.")],
    score: Annotated[
        list[str],
        typer.Option(
            "--score",
            help="criterion=value (repeatable). Every queue criterion must be graded "
            "or explicitly skipped with --skip-criterion.",
        ),
    ],
    reviewer: Annotated[
        Optional[str],
        typer.Option("--as", help="Reviewer identity; must match the item's assignee."),
    ] = None,
    skip_criterion: Annotated[
        Optional[list[str]],
        typer.Option(
            "--skip-criterion",
            help="Explicitly leave this criterion ungraded (repeatable).",
        ),
    ] = None,
    note: Annotated[
        Optional[str], typer.Option("--note", help="Free-text rationale recorded on the item.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the updated item as JSON.")
    ] = False,
) -> None:
    """Submit typed scores for an assigned item (validated against ADR-0117 configs).

    Writes one HUMAN-source Score per criterion to the capsule's scores.jsonl via
    the existing append path, Ed25519-signed by the maker's keyring key. On a
    maker-checker queue the item awaits 'nova annotate confirm' by a distinct
    reviewer; otherwise it completes immediately.
    """
    from novafabric.eval.annotation_store import get_queue, submit_item

    values = _parse_scores(score)
    try:
        item, scores = submit_item(
            item_id,
            values,
            reviewer=reviewer,
            note=note,
            skip_criteria=skip_criterion or [],
        )
    except (AnnotationError, ScoreConfigViolation) as exc:
        _fail(exc)
    if json_output:
        typer.echo(_item_json(item))
        return
    console.print(
        f"[green]Submitted[/green] {len(scores)} HUMAN score(s) by {item.assignee}:"
    )
    for s in scores:
        console.print(f"  {s.name} = {s.value!r}  (score_id={s.score_id})")
    if item.state is ItemState.CHECKER_PENDING:
        console.print(
            f"  Awaiting checker — run [bold]nova annotate confirm {item.item_id} "
            f"--as <checker>[/bold] as a different identity."
        )
    else:
        console.print(f"  Item {item.item_id} completed.")
    queue = get_queue(item.queue_id)
    if queue.seal:
        console.print(
            "  [yellow]note:[/yellow] queue is marked --seal; evidence-bundle sealing "
            "is planned (ADR-0118 P5) — the scores are unsigned lines covered by the "
            "capsule Merkle root."
        )


@annotate_app.command("confirm")
def annotate_confirm(
    item_id: Annotated[str, typer.Argument(help="checker_pending item id (ULID).")],
    checker: Annotated[
        str,
        typer.Option("--as", help="Checker identity — must differ from the maker (SoD)."),
    ] = "",
    note: Annotated[
        Optional[str], typer.Option("--note", help="Optional checker comment.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the updated item as JSON.")
    ] = False,
) -> None:
    """Checker step (maker-checker queues): confirm a submitted item.

    The checker's identity and Ed25519 keyring fingerprint must both differ
    from the maker's (separation of duties, ADR-0118 D4 / ADR-0003).
    """
    from novafabric.eval.annotation_store import confirm_item

    identity = checker or _default_identity()
    try:
        item = confirm_item(item_id, identity, note=note)
    except AnnotationError as exc:
        _fail(exc)
    if json_output:
        typer.echo(_item_json(item))
        return
    console.print(
        f"[green]Confirmed[/green] item {item.item_id} by {item.checker} "
        f"(maker: {item.assignee}) — completed."
    )


@annotate_app.command("skip")
def annotate_skip(
    item_id: Annotated[str, typer.Argument(help="Item id (ULID) to skip.")],
    note: Annotated[
        Optional[str], typer.Option("--note", help="Why the item was skipped.")
    ] = None,
) -> None:
    """Skip an item (terminal; writes no score)."""
    from novafabric.eval.annotation_store import skip_item

    try:
        item = skip_item(item_id, note=note)
    except AnnotationError as exc:
        _fail(exc)
    console.print(f"[yellow]Skipped[/yellow] item {item.item_id} (no score written).")
