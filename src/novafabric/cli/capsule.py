"""``nova capsule delete`` — governed deletion of one local capsule.

Runs the same core as the ``/v0`` and ``nova serve`` delete routes
(:mod:`novafabric.server.capsule_delete`): legal holds always win (``--force``
included), WORM locks and NovaSeal seals refuse, and the capsule directory plus
its runs-cache rows are removed in a recoverable order. Success is printed and
``capsule.delete`` is audited only after the capsule is actually gone.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import typer
from pydantic import ValidationError
from rich.console import Console

from novafabric import _paths as _nf_paths
from novafabric.audit import AuditEventType, AuditLog, resolve_audit_log_path
from novafabric.storage._retention import RetentionPolicy, RetentionPolicyEngine

console = Console()
log = logging.getLogger(__name__)
app = typer.Typer(help="Manage capsule retention, deletion, and lifecycle.")

_ACTOR = "cli-user"


class RetentionRefusal(Exception):
    """A retention policy refuses this deletion (message says why)."""


def _check_retention(
    registry: str, capsule_dir: Path, *, age_days: int, force: bool
) -> list[str]:
    """Apply every retention policy found for *registry*; return the files applied.

    ``deletion_mode: prohibited`` always refuses. The retention window refuses
    unless *force*. An unreadable or invalid policy file refuses (fail closed).

    Raises:
        RetentionRefusal: a policy refuses the deletion.
    """
    from novafabric.retention.holds import (
        capsule_base_holds_root,
        retention_policy_paths,
    )

    applied: list[str] = []
    for path in retention_policy_paths(
        registry, extra_roots=(capsule_base_holds_root(capsule_dir),)
    ):
        try:
            policy = RetentionPolicy.from_yaml(path)
        except (OSError, ValueError, ValidationError) as exc:
            raise RetentionRefusal(
                f"retention policy {path} is unreadable or invalid (fail closed): {exc}"
            ) from exc
        applied.append(str(path))
        if policy.deletion_mode == "prohibited":
            raise RetentionRefusal(
                f"deletion_mode=prohibited in {path}: capsules in registry "
                f"'{registry}' are never deleted (--force does not override this)"
            )
        if force:
            continue
        allowed, reason = RetentionPolicyEngine(holds=[]).can_delete(policy, age_days=age_days)
        if not allowed:
            raise RetentionRefusal(f"{reason} (policy {path}; --force skips this check)")
    return applied


@app.command("delete")
def capsule_delete(
    capsule_id: str = typer.Argument(..., help="Capsule ID to delete"),
    registry: str = typer.Option(..., "--registry", "-r", help="Registry name"),
    age_days: int = typer.Option(
        0, "--age-days", help="Age of the capsule in days (used for retention check)"
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Skip the retention-window check only. Legal holds, WORM locks, NovaSeal "
            "seals and deletion_mode=prohibited still refuse."
        ),
    ),
    capsule_dir: Optional[Path] = typer.Option(  # noqa: UP045
        None,
        "--capsule-dir",
        file_okay=False,
        help="Capsule store to delete from (default: NOVAFABRIC_CAPSULE_DIR, else "
        "$NOVAFABRIC_HOME/capsules).",
    ),
) -> None:
    """Delete a capsule, subject to legal holds, WORM, seals and retention policy.

    Removes the capsule directory and its index rows through the same governed
    core as the server delete routes, and reports success only once the capsule
    is gone. Deletion is refused when: any legal hold is active (holds are
    registry-global, and --force never overrides one); a WORM lock or NovaSeal
    seal protects the capsule; a retention policy sets deletion_mode=prohibited;
    or the capsule is inside a policy's retention window (the only check --force
    skips). Policies are read from $NOVAFABRIC_HOME/registries/<registry>/ and the
    legacy ./.novafabric/registries/<registry>/; every one found applies.

    Exit codes: 0 = deleted; 1 = refused, not found, or the delete failed (nothing
    is reported or audited as deleted).

    Scope: single capsule.

    \b
    Examples:
      nova capsule delete <capsule-id> --registry my-registry

      # Skip the retention-window check (holds still win)
      nova capsule delete <capsule-id> --registry my-registry --force
    """
    from novafabric.registry.store import get_connection, init_schema
    from novafabric.retention.holds import InvalidRegistryNameError, validate_registry_name
    from novafabric.server import capsule_delete as core

    store = capsule_dir if capsule_dir is not None else _nf_paths.default_capsule_dir()
    try:
        validate_registry_name(registry)
    except InvalidRegistryNameError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    if not core.is_valid_run_id(capsule_id):
        console.print(f"[red]Invalid capsule id: {capsule_id!r}[/red]")
        raise typer.Exit(code=1)

    # Holds always win — every registry, --force included (ADR-0206 D2) — then WORM
    # and seals. Checked before existence so a held id is reported as held.
    try:
        core.check_deletable(store, capsule_id)
    except core.DeleteBlockedError as exc:
        what = "active legal hold(s)" if exc.code == "legal_hold_active" else exc.code
        console.print(
            f"[red]Deletion blocked ({what}):[/red] {exc} {exc.details}", soft_wrap=True
        )
        core.audit_index_event(
            True,
            _ACTOR,
            capsule_id,
            {"code": exc.code, "registry": registry, "via": "nova capsule delete"},
        )
        raise typer.Exit(code=1) from None

    if not core.capsule_exists(store, capsule_id):
        console.print(
            f"[red]Capsule not found:[/red] {capsule_id} in {store} — nothing deleted.",
            soft_wrap=True,
        )
        raise typer.Exit(code=1)

    try:
        policies = _check_retention(registry, store, age_days=age_days, force=force)
    except RetentionRefusal as exc:
        console.print(f"[red]Deletion blocked:[/red] {exc}", soft_wrap=True)
        raise typer.Exit(code=1) from None

    conn = get_connection()
    try:
        init_schema(conn)
        outcome = core.execute_delete(store, capsule_id, conn, actor=_ACTOR)
    except core.DeleteFailedError as exc:
        console.print(f"[red]Delete failed ({exc.code}):[/red] {exc}", soft_wrap=True)
        raise typer.Exit(code=1) from None
    finally:
        conn.close()

    if core.capsule_exists(store, capsule_id):  # defensive: never report a non-effect
        console.print(f"[red]Delete failed:[/red] {capsule_id} is still present.")
        raise typer.Exit(code=1)

    AuditLog(resolve_audit_log_path()).append(
        event_type=AuditEventType.CAPSULE_DELETE,
        actor=_ACTOR,
        resource_id=capsule_id,
        details={
            "registry": registry,
            "age_days": age_days,
            "force": force,
            "capsule_dir": str(store),
            "retention_policies": policies,
            "residue": outcome.residue,
        },
    )
    console.print(f"[green]Deleted capsule:[/green] {capsule_id} from registry '{registry}'")
    if outcome.residue:
        console.print(
            f"[yellow]Note:[/yellow] the capsule is gone from the store and index; "
            f"leftover bytes at {outcome.residue} are removed by a later delete.",
            soft_wrap=True,
        )
