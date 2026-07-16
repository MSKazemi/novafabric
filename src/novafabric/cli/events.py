"""``nova events`` — read the local lifecycle-event log and test-fire events.

Experimental (ADR-0137). Strictly opt-in: nothing is emitted until the user
configures a sink via ``NOVA_EVENTS_LOG`` / ``NOVA_EVENTS_WEBHOOK``. See
``design/spec/lifecycle-webhooks-v0.md`` for the record contract.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer

from novafabric.events.emitter import ENV_LOG, build_emitter_from_env
from novafabric.events.model import EventType, LifecycleEvent, Subject, SubjectKind

events_app = typer.Typer(
    help=(
        "Lifecycle event log and outbound webhooks (experimental, ADR-0137). "
        "Opt-in via NOVA_EVENTS_LOG / NOVA_EVENTS_WEBHOOK; emission is "
        "best-effort and never blocks the workload."
    ),
    no_args_is_help=True,
)


def _resolve_log_path(log: Path | None) -> Path:
    if log is not None:
        return log
    env_path = os.environ.get(ENV_LOG, "").strip()
    if env_path:
        return Path(env_path)
    typer.echo(
        "Error: no events log configured — pass --log PATH or set NOVA_EVENTS_LOG.",
        err=True,
    )
    raise typer.Exit(1)


def _parse_since(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise typer.BadParameter(
            "--since must be ISO 8601 / RFC 3339 (e.g. 2026-07-15T00:00:00Z)"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _occurred_at(record: dict[str, Any]) -> datetime | None:
    raw = str(record.get("occurred_at", ""))
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


@events_app.command("tail")
def events_tail_cmd(
    log: Path | None = typer.Option(
        None, "--log", help="Events log path (default: $NOVA_EVENTS_LOG)"
    ),
    type_: str | None = typer.Option(
        None, "--type", help="Only show events of this type (e.g. capsule.created)"
    ),
    since: str | None = typer.Option(
        None, "--since", help="Only show events at/after this RFC 3339 timestamp"
    ),
    json_: bool = typer.Option(
        False, "--json", help="Print raw JSON lines instead of the summary table"
    ),
    last: int = typer.Option(
        0, "--last", min=0, help="Only show the last N matching events (0 = all)"
    ),
) -> None:
    """Read the local append-only lifecycle-event log (events.jsonl).

    Scope: local file read; fully offline.

    \b
    Examples:
      NOVA_EVENTS_LOG=~/.novafabric/events.jsonl nova events tail
      nova events tail --log ./events.jsonl --type capsule.created --json
    """
    path = _resolve_log_path(log)
    if not path.exists():
        typer.echo(f"Error: events log not found: {path}", err=True)
        raise typer.Exit(1)

    since_ts = _parse_since(since) if since else None
    records: list[tuple[dict[str, Any], str]] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            typer.echo(f"Warning: skipping malformed line {i}", err=True)
            continue
        if type_ and record.get("type") != type_:
            continue
        if since_ts is not None:
            occurred = _occurred_at(record)
            if occurred is None or occurred < since_ts:
                continue
        records.append((record, line))

    if last > 0:
        records = records[-last:]

    for record, line in records:
        if json_:
            typer.echo(line)
        else:
            subject = record.get("subject") or {}
            typer.echo(
                f"{record.get('occurred_at', '?'):32}  "
                f"{record.get('type', '?'):28}  "
                f"{subject.get('kind', '?')}:{subject.get('ref', '?')}"
            )
    if not records:
        typer.echo("No matching events.")


@events_app.command("emit")
def events_emit_cmd(
    type_: str = typer.Option(
        ..., "--type", help="Lifecycle event type (e.g. capsule.created)"
    ),
    subject: str = typer.Option(
        ...,
        "--subject",
        help="Event subject as KIND:REF (e.g. capsule:run-2026-07-15-a1b2c3)",
    ),
    digest: str | None = typer.Option(
        None, "--digest", help="Optional subject content digest (e.g. sha256:...)"
    ),
    payload: str | None = typer.Option(
        None,
        "--payload",
        help="JSON object with non-sensitive context (refs/digests/enums/counts only)",
    ),
    source: str = typer.Option(
        "nova events emit", "--source", help="Emitting component recorded in the event"
    ),
) -> None:
    """Manually emit one lifecycle event through the configured sinks.

    Use this to test a wired CI hook end-to-end. Requires at least one sink
    (NOVA_EVENTS_LOG or NOVA_EVENTS_WEBHOOK); delivery is best-effort.

    Scope: one event; writes the local log and/or POSTs configured webhooks.

    \b
    Examples:
      NOVA_EVENTS_LOG=./events.jsonl nova events emit \\
        --type capsule.created --subject capsule:run-abc123
      NOVA_EVENTS_WEBHOOK=https://ci.internal/hook nova events emit \\
        --type policy.failed --subject policy:promotion:agent-a@1.4.0 \\
        --payload '{"gate": "eval-regression", "decision": "deny"}'
    """
    try:
        event_type = EventType(type_)
    except ValueError as exc:
        valid = ", ".join(t.value for t in EventType)
        raise typer.BadParameter(f"unknown --type {type_!r}; one of: {valid}") from exc

    kind_raw, sep, ref = subject.partition(":")
    if not sep or not ref:
        raise typer.BadParameter("--subject must be KIND:REF (e.g. capsule:run-abc)")
    try:
        kind = SubjectKind(kind_raw)
    except ValueError as exc:
        valid = ", ".join(k.value for k in SubjectKind)
        raise typer.BadParameter(
            f"unknown subject kind {kind_raw!r}; one of: {valid}"
        ) from exc

    payload_obj: dict[str, Any] = {}
    if payload:
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise typer.BadParameter(f"--payload is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise typer.BadParameter("--payload must be a JSON object")
        payload_obj = parsed

    emitter = build_emitter_from_env()
    if not emitter.enabled:
        typer.echo(
            "Error: no sink configured — set NOVA_EVENTS_LOG and/or "
            "NOVA_EVENTS_WEBHOOK (webhooks are opt-in; there is no default "
            "destination).",
            err=True,
        )
        raise typer.Exit(1)

    event = LifecycleEvent(
        type=event_type,
        subject=Subject(kind=kind, ref=ref, digest=digest),
        payload=payload_obj,
        source=source,
    )
    emitter.emit(event)
    typer.echo(f"Emitted {event.type.value} event {event.event_id} (best-effort).")
