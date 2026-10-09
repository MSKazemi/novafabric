"""``nova session`` — group N independent runs into one session (ADR-0122).

Experimental. A session is a local, content-addressed ``session.json``
manifest referencing member Run Capsules in turn order; it copies no capsule
data and never writes a member capsule. Distinct from the parent/child
distributed-run hierarchy (ADR-0032/0039).

``nova session list`` reads the rebuildable SQLite session index when it is
fresh and falls back to a directory scan otherwise; ``nova session reindex``
(re)builds it (ADR-0122 P3). ``nova session export | verify-bundle | import``
move a session and its member capsules as one verifiable ZIP (ADR-0122 P4).

``nova session replay`` (ADR-0123 P1 + P5, experimental) orchestrates the
existing per-capsule replay engine over the session's members in sequence
order, optionally over a ``--from/--to`` slice with per-turn ``--turn-mode``
pins, or prints the plan only with ``--dry-run``.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from novafabric.cli._output import emit_json
from novafabric.session import (
    SessionError,
    SessionReplayMode,
    SessionReplayPlan,
    add_member,
    export_session_bundle,
    import_session_bundle,
    list_sessions_fast,
    load_session,
    new_session,
    plan_session_replay,
    rebuild_index,
    replay_session,
    resolve_members,
    save_session,
    session_stats,
    verify_session_bundle,
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
                f"[red]Capsule not found:[/red] {capsule} (not a directory, and no {candidate})"
            )
            raise typer.Exit(code=1)
    try:
        manifest = load_session(session_id, root=session_dir)
        member = add_member(manifest, capsule_dir, root=session_dir, role=role, reopen=reopen)
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
    rebuild_index_first: Annotated[
        bool,
        typer.Option(
            "--rebuild-index",
            help=(
                "Rebuild the session index from the manifests before listing "
                "(same as `nova session reindex`)."
            ),
        ),
    ] = False,
) -> None:
    """List known sessions (kind, member count, created time), newest first.

    Served from the local SQLite session index when it is fresh; a missing,
    stale, or corrupt index falls back to a directory scan of the sessions
    root (same output), with a hint on stderr to run `nova session reindex`.
    """
    err = Console(stderr=True)
    if rebuild_index_first:
        try:
            rebuild_index(root=session_dir)
        except SessionError as exc:
            err.print(f"[yellow]Session index not rebuilt:[/yellow] {exc}")
    listing = list_sessions_fast(root=session_dir)
    if listing.index_status in ("stale", "corrupt", "version_mismatch"):
        err.print(
            f"[dim]Session index {listing.index_status} ({listing.detail}); "
            "listed by directory scan. Run `nova session reindex` to rebuild.[/dim]"
        )
    manifests = listing.manifests
    if json_output:
        emit_json(json.dumps([m.to_json_dict() for m in manifests]))
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


@session_app.command("reindex")
def reindex_cmd(
    session_dir: Annotated[
        Path | None, typer.Option("--session-dir", help=_SESSION_DIR_HELP)
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the rebuild report as JSON.")
    ] = False,
) -> None:
    """(Re)build the local SQLite session index from the session manifests.

    The index (<sessions-root>/.session-index.sqlite) is a rebuildable cache
    for fast `nova session list`; the session.json manifests stay
    authoritative. Safe to run at any time, including concurrently.
    """
    try:
        report = rebuild_index(root=session_dir)
    except SessionError as exc:
        console.print(f"[red]Session index error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    if json_output:
        emit_json(report.model_dump_json())
        return
    console.print(
        f"[green]✓[/green] Indexed {report.indexed} session(s)"
        + (f", {report.unreadable} unreadable (skipped)" if report.unreadable else "")
        + f" → {report.index_path}"
    )


@session_app.command("export")
def export_cmd(
    session_id: Annotated[str, typer.Argument(help="Session to bundle.")],
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Path of the bundle ZIP to write."),
    ],
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
        bool, typer.Option("--json", help="Emit the export report as JSON.")
    ] = False,
) -> None:
    """Write a session plus all its member capsules as one verifiable ZIP.

    Deterministic (sorted entries, pinned timestamps): the same session and
    capsules give byte-identical bundles. Refuses an empty session, a missing
    or tampered member, or a capsule containing a symlink. Unsigned — for
    signed evidence over the members use `nova evidence export`.
    """
    if capsule_dir is None:
        from novafabric._paths import default_capsule_dir

        capsule_dir = default_capsule_dir()
    try:
        report = export_session_bundle(
            session_id, output, root=session_dir, capsule_base=capsule_dir
        )
    except SessionError as exc:
        console.print(f"[red]Session bundle error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    if json_output:
        emit_json(report.model_dump_json())
        return
    console.print(
        f"[green]✓[/green] Bundled session {session_id}: {report.members} member(s), "
        f"{report.files} file(s) → {report.path}"
    )
    console.print(f"[dim]archive {report.archive_sha256}[/dim]")


@session_app.command("verify-bundle")
def verify_bundle_cmd(
    bundle: Annotated[Path, typer.Argument(help="Session bundle ZIP to verify.")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the verification report as JSON.")
    ] = False,
) -> None:
    """Verify a session bundle offline: every digest, member, and path.

    Recomputes every file digest, rejects unlisted files and unsafe archive
    paths, re-checks session.json ordering, and matches each member's
    capsule.yaml to its content-addressed capsule_ref. Exit 1 on any problem.
    """
    report = verify_session_bundle(bundle)
    if json_output:
        emit_json(report.model_dump_json())
    else:
        console.print(f"Session bundle verification: {bundle.name}")
        console.print(
            f"  session: {report.session_id or '-'}  members: {report.members}  "
            f"files checked: {report.files_checked}"
        )
        for problem in report.problems:
            console.print(f"    [red]✗[/red] {problem}")
        if report.ok:
            console.print("[green]Session bundle verification PASSED[/green]")
        else:
            console.print("[red]Session bundle verification FAILED[/red]")
    if not report.ok:
        raise typer.Exit(code=1)


@session_app.command("import")
def import_cmd(
    bundle: Annotated[Path, typer.Argument(help="Session bundle ZIP to import.")],
    session_dir: Annotated[
        Path | None, typer.Option("--session-dir", help=_SESSION_DIR_HELP)
    ] = None,
) -> None:
    """Verify a session bundle, then add it to the local sessions root.

    Nothing is written unless verification passes. Members land under
    <sessions-root>/<session_id>/capsules/, where `nova session show` and
    `nova session replay` find them. Never overwrites an existing session.
    """
    try:
        result = import_session_bundle(bundle, root=session_dir)
    except SessionError as exc:
        console.print(f"[red]Session import error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[green]✓[/green] Imported session {result.session_id} "
        f"({result.members} member(s)) → {result.session_dir}"
    )


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
        emit_json(json.dumps(payload))
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


_VALID_MODES: tuple[SessionReplayMode, ...] = ("forensic", "mocked", "semantic", "exact")


def _parse_turn_modes(raw: list[str]) -> dict[int, SessionReplayMode]:
    """Parse repeatable ``SEQ=MODE`` pins; a malformed or repeated pin is fatal."""
    pins: dict[int, SessionReplayMode] = {}
    for item in raw:
        seq_text, sep, mode_text = item.partition("=")
        mode_value = mode_text.strip()
        if not sep or not seq_text.strip().isdigit() or mode_value not in _VALID_MODES:
            raise typer.BadParameter(
                f"expected SEQ=MODE with MODE in {'|'.join(_VALID_MODES)}, got {item!r}",
                param_hint="--turn-mode",
            )
        seq = int(seq_text.strip())
        if seq in pins:
            raise typer.BadParameter(
                f"turn {seq} is pinned more than once", param_hint="--turn-mode"
            )
        pins[seq] = next(m for m in _VALID_MODES if m == mode_value)
    return pins


def _print_plan(plan: SessionReplayPlan, json_output: bool) -> None:
    """Render a dry-run plan (nothing was executed)."""
    if json_output:
        emit_json(json.dumps(plan.to_json_dict()))
        return
    scope = (
        f"turns {plan.range[0]}..{plan.range[1]} of {plan.total_turns}"
        if plan.range
        else f"all {plan.total_turns} turn(s)"
    )
    table = Table(title=f"Session replay plan (dry run): {plan.session_id} — {scope}")
    table.add_column("seq", justify="right")
    table.add_column("source run", style="cyan", no_wrap=True)
    table.add_column("mode")
    table.add_column("integrity")
    table.add_column("tool calls", justify="right")
    table.add_column("mutating", justify="right")
    table.add_column("tool decisions")
    for turn in plan.turns:
        exposure = turn.tool_exposure
        table.add_row(
            str(turn.sequence),
            turn.source_capsule_id,
            turn.effective_mode + (" (pinned)" if turn.mode_pinned else ""),
            turn.integrity + (" → refuse" if turn.would_refuse else ""),
            str(exposure.tool_calls) if exposure else "-",
            str(exposure.mutating) if exposure else "-",
            (", ".join(f"{k}={v}" for k, v in sorted(exposure.decisions.items())) or "-")
            if exposure
            else "-",
        )
    console.print(table)
    console.print(
        "[dim]Dry run: nothing executed, nothing written. Exact-mode "
        "preconditions are checked only on execution.[/dim]"
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
    from_seq: Annotated[
        Optional[int],
        typer.Option(
            "--from",
            min=0,
            help="First turn (sequence, inclusive) of a contiguous sub-range.",
        ),
    ] = None,
    to_seq: Annotated[
        Optional[int],
        typer.Option(
            "--to",
            min=0,
            help="Last turn (sequence, inclusive) of a contiguous sub-range.",
        ),
    ] = None,
    turn_mode: Annotated[
        Optional[list[str]],
        typer.Option(
            "--turn-mode",
            help=(
                "Pin one turn's mode as SEQ=MODE (repeatable), e.g. "
                "--turn-mode 2=forensic. Other turns use --mode."
            ),
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help=(
                "Print the plan (turn order, per-turn effective mode, member "
                "integrity, mutating-tool exposure) without executing or "
                "writing anything."
            ),
        ),
    ] = False,
) -> None:
    """Replay every turn of a session, in sequence order (experimental).

    Orchestrates the existing single-capsule replay engine (ADR-0005) over
    the session's members (ADR-0123 P1): each turn produces its own replay
    capsule, and the session gets one SessionReplayResult with ordered
    per-turn verdicts plus a whole-session verdict. A missing or tampered
    member is an honest per-turn refusal — never silently skipped. State-seam
    verification between turns (ADR-0123 P2) is future design.

    --from/--to replay one contiguous slice (recorded as `range`);
    --turn-mode pins a mode per turn (recorded as `turn_mode_policy`, and in
    each turn's effective_mode); --dry-run prints the plan and exits 0.

    Exit code is 0 only when the whole-session verdict is 'reproduced'.
    """
    from novafabric._paths import default_capsule_dir
    from novafabric.capture._ulid import new_ulid

    if capsule_dir is None:
        capsule_dir = default_capsule_dir()
    pins = _parse_turn_modes(turn_mode or [])
    base = output_dir or (Path.cwd() / ".novafabric" / "replays")
    try:
        if dry_run:
            plan = plan_session_replay(
                session_id,
                mode=mode.value,
                on_divergence=on_divergence.value,
                continue_past_refusal=continue_past_refusal,
                root=session_dir,
                capsule_base=capsule_dir,
                from_seq=from_seq,
                to_seq=to_seq,
                turn_modes=pins,
            )
            _print_plan(plan, json_output)
            return
        result = replay_session(
            session_id,
            mode=mode.value,
            on_divergence=on_divergence.value,
            continue_past_refusal=continue_past_refusal,
            root=session_dir,
            capsule_base=capsule_dir,
            base_dir=base,
            from_seq=from_seq,
            to_seq=to_seq,
            turn_modes=pins,
        )
    except SessionError as exc:
        console.print(f"[red]Session replay error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    result_path = write_session_replay_result(result, base / f"session-replay-{new_ulid()}")

    if json_output:
        emit_json(json.dumps(result.to_json_dict()))
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
        if result.range is not None:
            total = result.range[1] - result.range[0] + 1
            console.print(
                f"[dim]Sub-range: turns {result.range[0]}..{result.range[1]} "
                "(verdict covers this slice only)[/dim]"
            )
        else:
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
