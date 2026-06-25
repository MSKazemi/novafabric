from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console

from novafabric.audit import AUDIT_LOG_PATH, AuditEventType, AuditLog
from novafabric.storage._retention import LegalHold, RetentionPolicy, RetentionPolicyEngine

console = Console()
app = typer.Typer(help="Manage capsule retention, deletion, and lifecycle.")


@app.command("delete")
def capsule_delete(
    capsule_id: str = typer.Argument(..., help="Capsule ID to delete"),
    registry: str = typer.Option(..., "--registry", "-r", help="Registry name"),
    age_days: int = typer.Option(
        0, "--age-days", help="Age of the capsule in days (used for retention check)"
    ),
    force: bool = typer.Option(
        False, "--force", help="Skip retention check (dangerous, requires confirmation)"
    ),
) -> None:
    """Delete a capsule record, subject to retention policy and legal holds.

    Deletion is blocked when: a retention policy exists and the capsule is
    within the retention window; an active legal hold covers this registry;
    or the policy sets deletion_mode=prohibited.

    Scope: single capsule.

    \b
    Examples:
      nova capsule delete <capsule-id> --registry my-registry

      # Force-delete (bypasses retention check)
      nova capsule delete <capsule-id> --registry my-registry --force
    """
    if not force:
        # Check active holds first — independent of whether a retention-policy.yaml exists.
        holds_path = Path(f".novafabric/registries/{registry}/holds.jsonl")
        active_holds: list[LegalHold] = []
        if holds_path.exists():
            for line in holds_path.read_text().splitlines():
                if line.strip():
                    h = json.loads(line)
                    if h["released_at"] is None:
                        active_holds.append(
                            LegalHold(
                                hold_id=h["hold_id"],
                                reason=h["reason"],
                                duration_days=h.get("duration_days"),
                            )
                        )
        if active_holds:
            hold_ids = [h.hold_id for h in active_holds]
            console.print(f"[red]Deletion blocked: active legal holds: {hold_ids}[/red]")
            raise typer.Exit(code=1)

        # Check retention policy if configured for this registry.
        policy_path = Path(f".novafabric/registries/{registry}/retention-policy.yaml")
        if policy_path.exists():
            policy = RetentionPolicy.from_yaml(policy_path)
            engine = RetentionPolicyEngine(holds=[])
            allowed, reason = engine.can_delete(policy, age_days=age_days)
            if not allowed:
                console.print(f"[red]Deletion blocked: {reason}[/red]")
                raise typer.Exit(code=1)

    # Record successful deletion in the audit log
    AuditLog(AUDIT_LOG_PATH).append(
        event_type=AuditEventType.CAPSULE_DELETE,
        actor="cli-user",
        resource_id=capsule_id,
        details={"registry": registry, "age_days": age_days, "force": force},
    )
    console.print(f"[green]Deleted capsule:[/green] {capsule_id} from registry '{registry}'")
