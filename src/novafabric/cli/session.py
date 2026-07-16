"""``nova session`` — group N independent runs into one session (ADR-0122).

Experimental. A session is a local, content-addressed ``session.json``
manifest referencing member Run Capsules in turn order; it copies no capsule
data and never writes a member capsule. Distinct from the parent/child
distributed-run hierarchy (ADR-0032/0039).

``nova session replay`` (ADR-0123 P1, experimental) orchestrates the existing
per-capsule replay engine over the session's members in sequence order.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from novafabric.session import (
    SessionError,
    add_member,
    list_sessions,
    load_session,
    new_session,
    replay_session,
    resolve_members,
    save_session,
    session_stats,
    write_session_replay_result,
)

session_app = typer.Typer(no_args_is_help=True)
console = Console()

_SESSION_DIR_HELP = (
    "Root directory holding session manifests. Defaults to "
    "$NOVAFABRIC_SESSION_DIR, then $NOVAFABRIC_HOME/sessions."
)


class SessionKindOpt(str, Enum):
    conversation = "conversation"
    workflow = "workflow"
    custom = "custom"


class SessionReplayModeOpt(str, Enum):
    forensic = "forensic"
    mocked = "mocked"
    semantic = "semantic"
    exact = "exact"


class OnDivergenceOpt(str, Enum):
    stop = "stop"
    cont = "continue"


def _fmt_duration(ms: int | None) -> str:
    if ms is None:
        return "-"
    return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms}ms"


def _fmt_cost(cost: dict[str, float] | None) -> str:
    if not cost:
        return "-"
    return "  ".join(f"{amount:.4f} {currency}" for currency, amount in sorted(cost.items()))


@session_app.command("new")
def new_cmd(
    kind: Annotated[
        SessionKindOpt,
        typer.Option("--kind", help="Session kind: conversation | workflow | custom."),
    ] = SessionKindOpt.conversation,
    user: Annotated[
        Optional[str],
        typer.Option(
            "--user",
            help=(
                "Opaque, redaction-safe reference to the user/actor the session "
                "belongs to (use a stable hash or handle, never a raw identifier)."
            ),
        ),
    ] = None,
    session_dir: Annotated[
        Path | None, typer.Option("--session-dir", help=_SESSION_DIR_HELP)
    ] = None,
) -> None:
    """Create an empty session manifest and print the new session_id.

    \b
    Example:
      SID=$(nova session new --kind conversation)
      nova capture --session-id "$SID" --session-sequence 0 -- python agent.py
    """
    manifest = new_session(kind=kind.value, user_ref=user)
    path = save_session(manifest, root=session_dir)
    # session_id on stdout (script-friendly: SID=$(nova session new)); hint on stderr.
    typer.echo(manifest.session_id)
    Console(stderr=True).print(f"[dim]Session manifest written: {path}[/dim]")


@session_app.command("add")
def add_cmd(
    session_id: Annotated[str, typer.Argument(help="Session to append to.")],
    capsule: Annotated[
        str,
        typer.Argument(
            help=(
                "Member capsule: a capsule directory path, or a run_id resolved "
                "under the default capsule directory."
            )
        ),
    ],
    role: Annotated[
        Optional[str],
        typer.Option(
            "--role",
            help="Optional free-form member role label (e.g. user-turn, retrieval-step).",
        ),
    ] = None,
    reopen: Annotated[
        bool,
        typer.Option("--reopen", help="Allow adding to a finalized session."),
    ] = False,
    session_dir: Annotated[
        Path | None, typer.Option("--session-dir", help=_SESSION_DIR_HELP)
    ] = None,
) -> None:
    """Append a capsule as the next ordered member (auto-assigns sequence).

    The capsule is only read, never modified — the session manifest records a
    content-addressed reference (relative path + sha256 of capsule.yaml).
    """
    capsule_dir = Path(capsule)
    if not capsule_dir.is_dir():
        from novafabric._paths import default_capsule_dir

        candidate = default_capsule_dir() / capsule
        if candidate.is_dir():
            capsule_dir = candidate
        else:
            console.print(
                f"[red]Capsule not found:[/red] {capsule} "
                f"(not a directory, and no {candidate})"
            )
            raise typer.Exit(code=1)
    try:
        manifest = load_session(session_id, root=session_dir)
        member = add_member(
            manifest, capsule_dir, root=session_dir, role=role, reopen=reopen
        )
        save_session(manifest, root=session_dir)
    except SessionError as exc:
        console.print(f"[red]Session error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[green]✓[/green] Added run {member.run_id} to session {session_id} "
        f"as turn {member.sequence}"
    )


@session_app.command("list")
def list_cmd(
    session_dir: Annotated[
        Path | None, typer.Option("--session-dir", help=_SESSION_DIR_HELP)
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON instead of a table.")
    ] = False,
) -> None:
    """List known sessions (kind, member count, created time), newest first."""
    manifests = list_sessions(root=session_dir)
    if json_output:
        console.print_json(json.dumps([m.to_json_dict() for m in manifests]))
        return
    if not manifests:
        console.print("[dim]No sessions found.[/dim]")
        return
    table = Table(title=f"Sessions ({len(manifests)})")
    table.add_column("session_id", style="cyan", no_wrap=True)
    table.add_column("kind")
    table.add_column("members", justify="right")
    table.add_column("created_at")
    table.add_column("finalized")
    for m in manifests:
        table.add_row(
            m.session_id,
            m.session_kind,
            str(len(m.member_runs)),
            m.created_at,
            m.finalized_at or "-",
        )
    console.print(table)


@session_app.command("show")
def show_cmd(
    session_id: Annotated[str, typer.Argument(help="Session to display.")],
    session_dir: Annotated[
        Path | None, typer.Option("--session-dir", help=_SESSION_DIR_HELP)
    ] = None,
    capsule_dir: Annotated[
        Path | None,
        typer.Option(
            "--capsule-dir",
            help=(
                "Extra base directory searched as <capsule-dir>/<run_id> for "
                "members whose recorded path no longer resolves. Defaults to "
                "the default capsule directory."
            ),
        ),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON instead of a table.")
    ] = False,
) -> None:
    """Show the ordered turns of a session, with aggregate stats.

    A member whose capsule was deleted or moved is reported 'missing'; one
    whose capsule.yaml no longer matches the recorded content hash is
    reported 'tampered'. Neither fails the command.
    """
    try:
        manifest = load_session(session_id, root=session_dir)
    except SessionError as exc:
        console.print(f"[red]Session error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if capsule_dir is None:
        from novafabric._paths import default_capsule_dir

        capsule_dir = default_capsule_dir()
    resolved = resolve_members(manifest, root=session_dir, capsule_base=capsule_dir)
    stats = session_stats(resolved)

    if json_output:
        payload = {
            "session": manifest.to_json_dict(),
            "members": [r.model_dump(exclude_none=True) for r in resolved],
            "stats": stats.model_dump(exclude_none=True),
        }
        console.print_json(json.dumps(payload))
        return

    console.print(
        f"[bold]Session {manifest.session_id}[/bold] "
        f"({manifest.session_kind}, created {manifest.created_at}"
        + (f", finalized {manifest.finalized_at}" if manifest.finalized_at else "")
        + ")"
    )
    if manifest.user_ref:
        console.print(f"[dim]user_ref: {manifest.user_ref}[/dim]")

    table = Table(title=f"Turns ({stats.turns})")
    table.add_column("seq", justify="right")
    table.add_column("run_id", style="cyan", no_wrap=True)
    table.add_column("started_at")
    table.add_column("integrity")
    table.add_column("run status")
    table.add_column("duration", justify="right")
    table.add_column("role")
    style_for = {"ok": "green", "missing": "yellow", "tampered": "red"}
    for r in resolved:
        table.add_row(
            str(r.member.sequence),
            r.member.run_id,
            r.member.started_at,
            f"[{style_for[r.status]}]{r.status}[/{style_for[r.status]}]",
            r.run_status or "-",
            _fmt_duration(r.duration_ms),
            r.member.role or "-",
        )
    console.print(table)
    console.print(
        f"turns={stats.turns}  resolved={stats.resolved}  "
        f"missing={stats.missing}  tampered={stats.tampered}  "
        f"duration={_fmt_duration(stats.total_duration_ms)}  "
        f"tokens={stats.total_tokens if stats.total_tokens is not None else '-'}  "
        f"cost={_fmt_cost(stats.cost_by_currency)}"
    )


@session_app.command("replay")
def replay_cmd(
    session_id: Annotated[str, typer.Argument(help="Session to replay, turn by turn.")],
    mode: Annotated[
        SessionReplayModeOpt,
        typer.Option(
            "--mode",
            help=(
                "Per-turn replay mode (the four single-capsule modes of "
                "ADR-0005), applied to every member. Default: mocked."
            ),
        ),
    ] = SessionReplayModeOpt.mocked,
    on_divergence: Annotated[
        OnDivergenceOpt,
        typer.Option(
            "--on-divergence",
            help=(
                "stop (default): halt at the first diverged turn; continue: "
                "record the drift and keep replaying later turns."
            ),
        ),
    ] = OnDivergenceOpt.stop,
    continue_past_refusal: Annotated[
        bool,
        typer.Option(
            "--continue-past-refusal",
            help=(
                "Keep replaying past a hard refusal (missing/tampered member, "
                "exact-mode precondition failure). Off by default; the "
                "override is logged into the result."
            ),
        ),
    ] = False,
    session_dir: Annotated[
        Path | None, typer.Option("--session-dir", help=_SESSION_DIR_HELP)
    ] = None,
    capsule_dir: Annotated[
        Path | None,
        typer.Option(
            "--capsule-dir",
            help=(
                "Extra base directory searched as <capsule-dir>/<run_id> for "
                "members whose recorded path no longer resolves. Defaults to "
                "the default capsule directory."
            ),
        ),
    ] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output-dir",
            "-o",
            help=(
                "Base directory for replay output (per-turn replay capsules "
                "plus the session replay result). Defaults to "
                "./.novafabric/replays."
            ),
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the SessionReplayResult JSON on stdout."),
    ] = False,
) -> None:
    """Replay every turn of a session, in sequence order (experimental).

    Orchestrates the existing single-capsule replay engine (ADR-0005) over
    the session's members (ADR-0123 P1): each turn produces its own replay
    capsule, and the session gets one SessionReplayResult with ordered
    per-turn verdicts plus a whole-session verdict. A missing or tampered
    member is an honest per-turn refusal — never silently skipped. State-seam
    verification between turns (ADR-0123 P2) is future design.

    Exit code is 0 only when the whole-session verdict is 'reproduced'.
    """
    from novafabric._paths import default_capsule_dir
    from novafabric.capture._ulid import new_ulid

    if capsule_dir is None:
        capsule_dir = default_capsule_dir()
    base = output_dir or (Path.cwd() / ".novafabric" / "replays")
    try:
        result = replay_session(
            session_id,
            mode=mode.value,
            on_divergence=on_divergence.value,
            continue_past_refusal=continue_past_refusal,
            root=session_dir,
            capsule_base=capsule_dir,
            base_dir=base,
        )
    except SessionError as exc:
        console.print(f"[red]Session replay error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    result_path = write_session_replay_result(
        result, base / f"session-replay-{new_ulid()}"
    )

    if json_output:
        console.print_json(json.dumps(result.to_json_dict()))
    else:
        table = Table(title=f"Session replay: {session_id} (mode: {result.mode})")
        table.add_column("seq", justify="right")
        table.add_column("source run", style="cyan", no_wrap=True)
        table.add_column("mode")
        table.add_column("status")
        table.add_column("replay capsule", no_wrap=True)
        table.add_column("detail")
        turn_style = {
            "reproduced": "green",
            "diverged": "yellow",
            "refused": "red",
            "skipped": "dim",
        }
        for turn in result.turns:
            table.add_row(
                str(turn.sequence),
                turn.source_capsule_id,
                turn.effective_mode,
                f"[{turn_style[turn.status]}]{turn.status}[/{turn_style[turn.status]}]",
                turn.replay_capsule_id or "-",
                (turn.divergence or {}).get("detail", "-"),
            )
        console.print(table)
        replayed = len(result.turns)
        total = None
        try:
            total = len(load_session(session_id, root=session_dir).member_runs)
        except SessionError:  # pragma: no cover - session read a moment ago
            pass
        if total is not None and replayed < total:
            console.print(
                f"[yellow]Halted after turn {result.turns[-1].sequence}: "
                f"{total - replayed} later turn(s) not replayed.[/yellow]"
            )
        verdict_style = {
            "reproduced": "green",
            "diverged": "yellow",
            "refused": "red",
            "partial": "yellow",
        }[result.whole_session_verdict]
        console.print(
            f"whole_session_verdict: "
            f"[{verdict_style}]{result.whole_session_verdict}[/{verdict_style}]"
        )
        console.print(f"[dim]Session replay result written: {result_path}[/dim]")

    if result.whole_session_verdict != "reproduced":
        raise typer.Exit(code=1)
