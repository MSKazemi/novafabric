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
"""``nova retention`` — data-retention policy scheduler CLI (ADR-0134).

An on-demand, idempotent sweep that applies ADR-0031 retention windows over
time. NovaFabric embeds no daemon: run ``nova retention apply`` manually or
from ``cron``/``systemd``. ``plan`` (and ``apply --dry-run``) previews with
the identical due-computation code path and touches nothing.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from novafabric.audit import AUDIT_LOG_PATH, AuditLog
from novafabric.pii.dek import DEKStore, open_dek_store
from novafabric.retention.actions import SweepExecutor
from novafabric.retention.models import (
    Decision,
    PlannedDecision,
    RetentionAction,
    RetentionActionRecord,
    RetentionBinding,
    SweepItem,
    SweepOutcome,
)
from novafabric.retention.sweep import (
    HoldContext,
    enumerate_capsules,
    load_bindings,
    matches,
    plan_sweep,
)
from novafabric.retention.windows import compute_due_at
from novafabric.storage._retention import RetentionPolicy

console = Console()

app = typer.Typer(
    help=(
        "Apply data-retention policy bindings: plan (dry-run), apply, "
        "status, explain (ADR-0134)."
    ),
    no_args_is_help=True,
)

_SCHEMA_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# Context assembly (ground truth read fresh on every sweep — no cursor)
# ---------------------------------------------------------------------------


def _registry_dir(registry: str) -> Path:
    return Path(".novafabric/registries") / registry


def _default_capsule_dir() -> Path:
    home = Path(os.environ.get("NOVAFABRIC_HOME", Path.home() / ".novafabric"))
    return home / "capsules"


def _nova_home() -> Path:
    return Path(os.environ.get("NOVAFABRIC_HOME", Path.home() / ".novafabric"))


def _read_active_hold_ids(registry: str) -> list[str]:
    path = _registry_dir(registry) / "holds.jsonl"
    if not path.exists():
        return []
    holds: list[str] = []
    for line in path.read_text().splitlines():
        if line.strip():
            h = json.loads(line)
            if h.get("released_at") is None:
                holds.append(str(h["hold_id"]))
    return holds


def _read_deletion_mode(registry: str) -> str:
    path = _registry_dir(registry) / "retention-policy.yaml"
    if not path.exists():
        return "defensible"
    return RetentionPolicy.from_yaml(path).deletion_mode


def _read_worm_locks(registry: str, worm_db: Optional[Path]) -> dict[str, datetime]:  # noqa: UP045
    db_path = worm_db if worm_db is not None else _registry_dir(registry) / "worm.db"
    if not db_path.exists():
        return {}
    from novafabric.storage._local_worm import LocalWormAdapter

    adapter = LocalWormAdapter(db_path)
    return {e.capsule_id: e.locked_until for e in adapter.list()}


def _load_bindings_or_exit(registry: str) -> list[RetentionBinding]:
    policy_path = _registry_dir(registry) / "retention-policy.yaml"
    try:
        return load_bindings(policy_path)
    except ValueError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc


def _build_plan(
    registry: str,
    capsule_dir: Path,
    worm_db: Optional[Path],  # noqa: UP045
    action_filter: Optional[str],  # noqa: UP045
) -> tuple[list[PlannedDecision], list[RetentionBinding], list[SweepItem]]:
    bindings = _load_bindings_or_exit(registry)
    if action_filter is not None:
        try:
            wanted = RetentionAction(action_filter)
        except ValueError as exc:
            console.print(
                f"[red]✗[/red] unknown action {action_filter!r}; "
                "expected expire-metadata | purge | crypto-shred"
            )
            raise typer.Exit(code=1) from exc
        bindings = [b for b in bindings if b.action is wanted]
    items = enumerate_capsules(capsule_dir)
    holds = HoldContext(
        active_hold_ids=_read_active_hold_ids(registry),
        deletion_mode=_read_deletion_mode(registry),  # type: ignore[arg-type]
        worm_locks=_read_worm_locks(registry, worm_db),
    )
    return plan_sweep(items, bindings, holds), bindings, items


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _emit_records(
    records: list[RetentionActionRecord],
    *,
    registry: str,
    mode: str,
    json_out: bool,
) -> None:
    _acted = (SweepOutcome.DRY_RUN, SweepOutcome.APPLIED)
    due = [r for r in records if r.outcome in _acted]
    other = [r for r in records if r.outcome not in _acted]
    if json_out:
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "mode": mode,
            "swept_at": datetime.now(tz=timezone.utc).isoformat(),
            "registry": registry,
            "due": [r.model_dump(mode="json", exclude_none=True) for r in due],
            "held": [r.model_dump(mode="json", exclude_none=True) for r in other],
        }
        typer.echo(json.dumps(payload, indent=2))
        return
    if not records:
        console.print("[green]Nothing due:[/green] no binding matches a due item.")
        return
    table = Table(title=f"Retention sweep ({mode}) — registry '{registry}'")
    table.add_column("item")
    table.add_column("binding")
    table.add_column("action")
    table.add_column("due at")
    table.add_column("outcome")
    table.add_column("reason")
    for r in records:
        table.add_row(
            r.item_id,
            r.binding_id,
            r.action.value,
            r.due_at.isoformat(),
            r.outcome.value,
            r.reason or "",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command("plan")
def plan_cmd(
    registry: str = typer.Option(..., "--registry", "-r", help="Registry name"),
    capsule_dir: Optional[Path] = typer.Option(  # noqa: UP045
        None,
        "--capsule-dir",
        help="Capsule directory to sweep (default: $NOVAFABRIC_HOME/capsules).",
    ),
    worm_db: Optional[Path] = typer.Option(  # noqa: UP045
        None,
        "--worm-db",
        help="Local WORM adapter DB consulted for locked_until "
        "(default: .novafabric/registries/<registry>/worm.db).",
    ),
    action: Optional[str] = typer.Option(  # noqa: UP045
        None, "--action", help="Only consider bindings with this action."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Dry-run: list what WOULD be affected by the retention bindings. Touches nothing.

    Identical due-computation code path as `nova retention apply` (ADR-0134 D3).

    Scope: one registry's bindings over one capsule directory.

    \b
    Examples:
      nova retention plan --registry my-registry
      nova retention plan --registry my-registry --json
    """
    resolved_dir = capsule_dir if capsule_dir is not None else _default_capsule_dir()
    plan, _, _ = _build_plan(registry, resolved_dir, worm_db, action)
    executor = SweepExecutor(registry=registry, principal="dry-run", audit_log=None)
    records = executor.execute(plan, dry_run=True)
    _emit_records(records, registry=registry, mode="dry-run", json_out=json_out)


@app.command("apply")
def apply_cmd(
    registry: str = typer.Option(..., "--registry", "-r", help="Registry name"),
    capsule_dir: Optional[Path] = typer.Option(  # noqa: UP045
        None,
        "--capsule-dir",
        help="Capsule directory to sweep (default: $NOVAFABRIC_HOME/capsules).",
    ),
    worm_db: Optional[Path] = typer.Option(  # noqa: UP045
        None,
        "--worm-db",
        help="Local WORM adapter DB consulted for locked_until "
        "(default: .novafabric/registries/<registry>/worm.db).",
    ),
    action: Optional[str] = typer.Option(  # noqa: UP045
        None, "--action", help="Only consider bindings with this action."
    ),
    limit: Optional[int] = typer.Option(  # noqa: UP045
        None, "--limit", min=1, help="Bound the number of applied actions this pass."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview only: identical due-computation, no action."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt (for cron/CI)."
    ),
    principal: str = typer.Option(
        "cli-user", "--principal", help="Acting identity recorded in evidence entries."
    ),
    retention_months: int = typer.Option(
        6,
        "--retention-months",
        min=0,
        help="Art.17(3)(b) minimum retention window for crypto-shred (ADR-0069).",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Apply due retention actions; every decision is appended to the audit log.

    WORM/legal holds always win: a WORM-retained capsule is never purged or
    shredded before its retention date (recorded `skipped: worm_hold`).
    Crypto-shred dispatches to the existing `nova pii erase` DEK destruction.
    Idempotent and fail-safe: re-runs are no-ops; a per-item failure is
    recorded `error` and the sweep continues.

    Scope: one registry's bindings over one capsule directory.

    \b
    Examples:
      nova retention apply --registry my-registry --dry-run
      nova retention apply --registry my-registry --yes
      nova retention apply -r my-registry --action crypto-shred --limit 100 --yes
    """
    resolved_dir = capsule_dir if capsule_dir is not None else _default_capsule_dir()
    plan, _, _ = _build_plan(registry, resolved_dir, worm_db, action)
    due_count = sum(1 for d in plan if d.decision is Decision.DUE)

    if not dry_run and due_count > 0 and not yes:
        console.print(
            f"[yellow]{due_count} due item(s)[/yellow] would be acted on "
            f"({len(plan) - due_count} held/errored). "
            "Run with --dry-run to preview."
        )
        if not typer.confirm(f"Apply {due_count} retention action(s)?"):
            console.print("[red]Aborted:[/red] no action taken.")
            raise typer.Exit(code=1)

    dek_store: DEKStore | None = None
    needs_shred = any(
        d.decision is Decision.DUE and d.action is RetentionAction.CRYPTO_SHRED
        for d in plan
    )
    if not dry_run and needs_shred:
        dek_store = open_dek_store(_nova_home())
    try:
        executor = SweepExecutor(
            registry=registry,
            principal=principal,
            audit_log=None if dry_run else AuditLog(AUDIT_LOG_PATH),
            dek_store=dek_store,
            receipt_dir=_nova_home() / "evidence" / "erasure",
            retention_months=retention_months,
        )
        records = executor.execute(plan, dry_run=dry_run, limit=limit)
    finally:
        if dek_store is not None:
            dek_store.close()

    mode = "dry-run" if dry_run else "apply"
    _emit_records(records, registry=registry, mode=mode, json_out=json_out)
    if not dry_run and not json_out:
        applied = sum(1 for r in records if r.outcome is SweepOutcome.APPLIED)
        console.print(
            f"[green]Sweep complete:[/green] {applied} applied, "
            f"{len(records) - applied} skipped/deferred/errored. "
            f"Evidence appended to {AUDIT_LOG_PATH}."
        )


@app.command("status")
def status_cmd(
    registry: str = typer.Option(..., "--registry", "-r", help="Registry name"),
    capsule_dir: Optional[Path] = typer.Option(  # noqa: UP045
        None,
        "--capsule-dir",
        help="Capsule directory to sweep (default: $NOVAFABRIC_HOME/capsules).",
    ),
    worm_db: Optional[Path] = typer.Option(  # noqa: UP045
        None, "--worm-db", help="Local WORM adapter DB consulted for locked_until."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Read-only: per binding, how many items are due, held, and the next due date.

    Scope: one registry's bindings over one capsule directory.

    \b
    Examples:
      nova retention status --registry my-registry
      nova retention status --registry my-registry --json
    """
    resolved_dir = capsule_dir if capsule_dir is not None else _default_capsule_dir()
    plan, bindings, items = _build_plan(registry, resolved_dir, worm_db, None)
    now = datetime.now(tz=timezone.utc)

    rows: list[dict[str, object]] = []
    for binding in bindings:
        if not binding.enabled:
            continue
        due = sum(
            1
            for d in plan
            if d.decision is Decision.DUE and binding.id in d.matched_binding_ids
        )
        held = sum(
            1
            for d in plan
            if d.decision is not Decision.DUE and binding.id in d.matched_binding_ids
        )
        pending = [
            compute_due_at(binding.window, i.created_at)
            for i in items
            if matches(binding, i, now)
            and now < compute_due_at(binding.window, i.created_at)
        ]
        rows.append(
            {
                "binding_id": binding.id,
                "action": binding.action.value,
                "due_now": due,
                "held": held,
                "next_due_at": min(pending).isoformat() if pending else None,
            }
        )
    if json_out:
        payload = {"schema_version": _SCHEMA_VERSION, "registry": registry, "bindings": rows}
        typer.echo(json.dumps(payload, indent=2))
        return
    if not rows:
        console.print(f"No enabled retention bindings for registry '{registry}'.")
        return
    table = Table(title=f"Retention status — registry '{registry}'")
    table.add_column("binding")
    table.add_column("action")
    table.add_column("due now")
    table.add_column("held")
    table.add_column("next due")
    for row in rows:
        table.add_row(
            str(row["binding_id"]),
            str(row["action"]),
            str(row["due_now"]),
            str(row["held"]),
            str(row["next_due_at"] or "-"),
        )
    console.print(table)


@app.command("explain")
def explain_cmd(
    capsule_id: str = typer.Argument(..., help="Capsule ID to explain"),
    registry: str = typer.Option(..., "--registry", "-r", help="Registry name"),
    capsule_dir: Optional[Path] = typer.Option(  # noqa: UP045
        None,
        "--capsule-dir",
        help="Capsule directory to sweep (default: $NOVAFABRIC_HOME/capsules).",
    ),
    worm_db: Optional[Path] = typer.Option(  # noqa: UP045
        None, "--worm-db", help="Local WORM adapter DB consulted for locked_until."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Read-only: which bindings match this capsule, its due date, and its hold state.

    Scope: single capsule.

    \b
    Examples:
      nova retention explain <capsule-id> --registry my-registry
    """
    resolved_dir = capsule_dir if capsule_dir is not None else _default_capsule_dir()
    bindings = _load_bindings_or_exit(registry)
    items = enumerate_capsules(resolved_dir)
    item = next((i for i in items if i.item_id == capsule_id), None)
    if item is None:
        console.print(f"[red]✗[/red] capsule {capsule_id!r} not found in {resolved_dir}")
        raise typer.Exit(code=1)
    now = datetime.now(tz=timezone.utc)
    locks = _read_worm_locks(registry, worm_db)
    hold_ids = _read_active_hold_ids(registry)
    matched = [
        {
            "binding_id": b.id,
            "action": b.action.value,
            "due_at": compute_due_at(b.window, item.created_at).isoformat(),
            "due_now": now >= compute_due_at(b.window, item.created_at),
        }
        for b in bindings
        if b.enabled and matches(b, item, now)
    ]
    locked_until = locks.get(capsule_id)
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "registry": registry,
        "item_id": capsule_id,
        "created_at": item.created_at.isoformat(),
        "expired": item.expired,
        "active_legal_holds": hold_ids,
        "worm_locked_until": locked_until.isoformat() if locked_until else None,
        "matched_bindings": matched,
    }
    if json_out:
        typer.echo(json.dumps(payload, indent=2))
        return
    console.print(f"Capsule [bold]{capsule_id}[/bold] (created {item.created_at.isoformat()})")
    if hold_ids:
        console.print(f"  [yellow]Active legal holds:[/yellow] {hold_ids} — deletion blocked")
    if locked_until is not None:
        console.print(f"  [yellow]WORM locked until:[/yellow] {locked_until.isoformat()}")
    if item.expired:
        console.print("  Metadata already expired (retention-expired marker present).")
    if not matched:
        console.print("  No enabled binding matches — this capsule is never swept.")
        return
    for m in matched:
        marker = "[red]due now[/red]" if m["due_now"] else "not yet due"
        console.print(
            f"  binding [bold]{m['binding_id']}[/bold] -> {m['action']} "
            f"at {m['due_at']} ({marker})"
        )
