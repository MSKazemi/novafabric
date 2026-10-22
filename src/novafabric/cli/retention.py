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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from novafabric import _paths as _nf_paths
from novafabric.audit import AuditLog, resolve_audit_log_path
from novafabric.pii.dek import DEKStore, open_dek_store
from novafabric.retention.actions import SweepExecutor
from novafabric.retention.holds import capsule_base_holds_root
from novafabric.retention.models import (
    Decision,
    PlannedDecision,
    RetentionAction,
    RetentionActionRecord,
    RetentionBinding,
    SweepItem,
    SweepOptions,
    SweepOutcome,
)
from novafabric.retention.sweep import (
    MANIFEST_YAML,
    HoldContext,
    capture_exclusion_notice,
    enumerate_capsules,
    load_bindings,
    load_sweep_options,
    matches,
    plan_sweep,
    split_by_scope,
)
from novafabric.retention.windows import compute_due_at
from novafabric.storage._retention import RetentionPolicy

console = Console()

app = typer.Typer(
    help=(
        "Apply data-retention policy bindings: plan (dry-run), apply, "
        "status, explain (experimental, ADR-0134)."
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
    home = _nf_paths.nova_home()
    return home / "capsules"


def _nova_home() -> Path:
    return _nf_paths.nova_home()


def _pii_pepper() -> bytes | None:
    """``NOVA_PII_PEPPER`` (ADR-0314): resolves a capsule's subject HMAC to its DEK."""
    raw = os.environ.get("NOVA_PII_PEPPER", "")
    return raw.encode("utf-8") if raw else None


def _read_active_hold_ids(registry: str, capsule_dir: Path) -> list[str]:
    """Active holds on *registry* from the one legal-hold store.

    Same store ``nova hold`` and ``POST /api/holds`` write
    (:mod:`novafabric.retention.holds`), plus the pre-unification locations —
    ``./.novafabric/registries`` and the swept capsule base's
    ``<capsule_dir>/../registries`` — so a hold placed on either surface stops
    the sweep. A damaged ledger line counts as a hold (fail closed).
    """
    from novafabric.retention.holds import (
        InvalidRegistryNameError,
        active_hold_ids,
        capsule_base_holds_root,
    )

    try:
        return active_hold_ids(
            registry, extra_roots=(capsule_base_holds_root(capsule_dir),)
        )
    except InvalidRegistryNameError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc


def _apply_time_hold_check(
    registry: str, capsule_dir: Path, worm_db: Optional[Path]  # noqa: UP045
) -> Callable[[PlannedDecision], Optional[str]]:  # noqa: UP045
    """Re-read holds and WORM locks right before each action (ADR-0134 D4).

    The plan was built from a snapshot; the confirmation prompt and the sweep
    itself take time, and a hold placed meanwhile must still win.
    """
    from novafabric.retention.holds import active_hold_ids

    def check(decision: PlannedDecision) -> Optional[str]:  # noqa: UP045
        ids = active_hold_ids(registry, extra_roots=(capsule_base_holds_root(capsule_dir),))
        if ids:
            return f"legal_hold_active: {', '.join(ids)}"
        locked_until = _read_worm_locks(registry, worm_db).get(decision.item.item_id)
        if locked_until is not None and locked_until > datetime.now(tz=timezone.utc):
            return "worm_hold"
        return None

    return check


def _read_deletion_mode(registry: str) -> str:
    path = _policy_path(registry)
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


def _policy_path(registry: str) -> Path:
    return _registry_dir(registry) / "retention-policy.yaml"


def _load_bindings_or_exit(registry: str) -> list[RetentionBinding]:
    try:
        return load_bindings(_policy_path(registry))
    except ValueError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc


def _load_options_or_exit(registry: str) -> SweepOptions:
    try:
        return load_sweep_options(_policy_path(registry))
    except ValueError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc


@dataclass(frozen=True)
class _Sweep:
    """One sweep's ground truth, split by what ``apply`` may act on."""

    plan: list[PlannedDecision]
    bindings: list[RetentionBinding]
    items: list[SweepItem]
    options: SweepOptions
    acted: list[PlannedDecision]
    excluded: list[PlannedDecision]

    @property
    def capture_capsules(self) -> int:
        return sum(1 for i in self.items if i.manifest == MANIFEST_YAML)

    def in_scope(self, item: SweepItem) -> bool:
        return self.options.include_capture_capsules or item.manifest != MANIFEST_YAML

    def notice(self, registry: str) -> str | None:
        if self.options.include_capture_capsules:
            return None
        return capture_exclusion_notice(
            self.capture_capsules, len(self.excluded), _policy_path(registry)
        )


def _build_plan(
    registry: str,
    capsule_dir: Path,
    worm_db: Optional[Path],  # noqa: UP045
    action_filter: Optional[str],  # noqa: UP045
) -> _Sweep:
    bindings = _load_bindings_or_exit(registry)
    options = _load_options_or_exit(registry)
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
        active_hold_ids=_read_active_hold_ids(registry, capsule_dir),
        deletion_mode=_read_deletion_mode(registry),  # type: ignore[arg-type]
        worm_locks=_read_worm_locks(registry, worm_db),
    )
    plan = plan_sweep(items, bindings, holds)
    acted, excluded = split_by_scope(plan, options)
    return _Sweep(plan, bindings, items, options, acted, excluded)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _records_table(records: list[RetentionActionRecord], title: str) -> Table:
    table = Table(title=title)
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
    return table


def _emit_records(
    records: list[RetentionActionRecord],
    *,
    registry: str,
    mode: str,
    json_out: bool,
    sweep: _Sweep,
    excluded: list[RetentionActionRecord],
) -> None:
    """Print the sweep result; *excluded* are capture capsules apply does not act on.

    *excluded* are previewed (never applied) so the operator sees what opting in
    with ``include_capture_capsules: true`` would do to them.
    """
    _acted = (SweepOutcome.DRY_RUN, SweepOutcome.APPLIED)
    due = [r for r in records if r.outcome in _acted]
    other = [r for r in records if r.outcome not in _acted]
    notice = sweep.notice(registry)
    if json_out:
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "mode": mode,
            "swept_at": datetime.now(tz=timezone.utc).isoformat(),
            "registry": registry,
            "due": [r.model_dump(mode="json", exclude_none=True) for r in due],
            "held": [r.model_dump(mode="json", exclude_none=True) for r in other],
            "capture_capsules": {
                "included": sweep.options.include_capture_capsules,
                "enumerated": sweep.capture_capsules,
                "excluded": [r.model_dump(mode="json", exclude_none=True) for r in excluded],
                "notice": notice,
            },
        }
        typer.echo(json.dumps(payload, indent=2))
        return
    if not records:
        console.print("[green]Nothing due:[/green] no binding matches a due item.")
    else:
        console.print(
            _records_table(records, f"Retention sweep ({mode}) — registry '{registry}'")
        )
    if excluded:
        console.print(
            _records_table(
                excluded,
                "Capture capsules (capsule.yaml) NOT acted on by apply — "
                "shown as they would be swept with include_capture_capsules: true",
            )
        )
    if notice is not None:
        console.print(f"[yellow]Note:[/yellow] {notice}")


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
    sweep = _build_plan(registry, resolved_dir, worm_db, action)
    executor = SweepExecutor(registry=registry, principal="dry-run", audit_log=None)
    records = executor.execute(sweep.acted, dry_run=True)
    excluded = executor.execute(sweep.excluded, dry_run=True)
    _emit_records(
        records, registry=registry, mode="dry-run", json_out=json_out,
        sweep=sweep, excluded=excluded,
    )


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
    shredded before its retention date (recorded `skipped: worm_hold`), and a
    legal hold placed with `nova hold` or through `POST /api/holds` holds every
    item. Idempotent and fail-safe: re-runs are no-ops; a per-item failure is
    recorded `error` and the sweep continues.

    Limitations (experimental): only `<capsule-dir>/<run>/capsule.json`
    manifests are swept (`nova capture` writes capsule.yaml), and crypto-shred
    destroys the DEK named by `metadata.pii_subject_id`, which nothing writes
    today, so it records `no_dek` and destroys no key. Use `nova pii erase`
    to crypto-shred a data subject.

    Scope: one registry's bindings over one capsule directory.

    \b
    Examples:
      nova retention apply --registry my-registry --dry-run
      nova retention apply --registry my-registry --yes
      nova retention apply -r my-registry --action crypto-shred --limit 100 --yes
    """
    resolved_dir = capsule_dir if capsule_dir is not None else _default_capsule_dir()
    sweep = _build_plan(registry, resolved_dir, worm_db, action)
    plan = sweep.acted  # capture capsules only when the policy opts in
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
            audit_log=None if dry_run else AuditLog(resolve_audit_log_path()),
            dek_store=dek_store,
            receipt_dir=_nova_home() / "evidence" / "erasure",
            retention_months=retention_months,
            hold_check=_apply_time_hold_check(registry, resolved_dir, worm_db),
            hold_roots=(capsule_base_holds_root(resolved_dir),),
            pepper=_pii_pepper(),
        )
        records = executor.execute(plan, dry_run=dry_run, limit=limit)
    finally:
        if dek_store is not None:
            dek_store.close()

    excluded = SweepExecutor(
        registry=registry, principal="dry-run", audit_log=None
    ).execute(sweep.excluded, dry_run=True)
    mode = "dry-run" if dry_run else "apply"
    _emit_records(
        records, registry=registry, mode=mode, json_out=json_out,
        sweep=sweep, excluded=excluded,
    )
    if not dry_run and not json_out:
        applied = sum(1 for r in records if r.outcome is SweepOutcome.APPLIED)
        console.print(
            f"[green]Sweep complete:[/green] {applied} applied, "
            f"{len(records) - applied} skipped/deferred/errored. "
            f"Evidence appended to {resolve_audit_log_path()}."
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
    sweep = _build_plan(registry, resolved_dir, worm_db, None)
    now = datetime.now(tz=timezone.utc)

    rows: list[dict[str, object]] = []
    for binding in sweep.bindings:
        if not binding.enabled:
            continue
        mine = [d for d in sweep.acted if binding.id in d.matched_binding_ids]
        due = sum(1 for d in mine if d.decision is Decision.DUE)
        excluded = sum(1 for d in sweep.excluded if binding.id in d.matched_binding_ids)
        pending = [
            compute_due_at(binding.window, i.created_at)
            for i in sweep.items
            if sweep.in_scope(i)
            and matches(binding, i, now)
            and now < compute_due_at(binding.window, i.created_at)
        ]
        rows.append(
            {
                "binding_id": binding.id,
                "action": binding.action.value,
                "due_now": due,
                "held": len(mine) - due,
                "excluded_capture_capsules": excluded,
                "next_due_at": min(pending).isoformat() if pending else None,
            }
        )
    notice = sweep.notice(registry)
    if json_out:
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "registry": registry,
            "include_capture_capsules": sweep.options.include_capture_capsules,
            "capture_capsules_enumerated": sweep.capture_capsules,
            "bindings": rows,
        }
        typer.echo(json.dumps(payload, indent=2))
        return
    if not rows:
        console.print(f"No enabled retention bindings for registry '{registry}'.")
        return
    show_excluded = notice is not None
    table = Table(title=f"Retention status — registry '{registry}'")
    table.add_column("binding")
    table.add_column("action")
    table.add_column("due now")
    table.add_column("held")
    if show_excluded:
        table.add_column("excluded")  # capture capsules apply does not act on
    table.add_column("next due")
    for row in rows:
        cells = [str(row["binding_id"]), str(row["action"]), str(row["due_now"]), str(row["held"])]
        if show_excluded:
            cells.append(str(row["excluded_capture_capsules"]))
        table.add_row(*cells, str(row["next_due_at"] or "-"))
    console.print(table)
    if notice is not None:
        console.print(f"[yellow]Note:[/yellow] {notice}")


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
    options = _load_options_or_exit(registry)
    items = enumerate_capsules(resolved_dir)
    item = next((i for i in items if i.item_id == capsule_id), None)
    if item is None:
        console.print(f"[red]✗[/red] capsule {capsule_id!r} not found in {resolved_dir}")
        raise typer.Exit(code=1)
    now = datetime.now(tz=timezone.utc)
    locks = _read_worm_locks(registry, worm_db)
    hold_ids = _read_active_hold_ids(registry, resolved_dir)
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
        "manifest": item.manifest,
        "swept_by_apply": options.include_capture_capsules or item.manifest != MANIFEST_YAML,
        "expired": item.expired,
        "active_legal_holds": hold_ids,
        "worm_locked_until": locked_until.isoformat() if locked_until else None,
        "matched_bindings": matched,
    }
    if json_out:
        typer.echo(json.dumps(payload, indent=2))
        return
    console.print(f"Capsule [bold]{capsule_id}[/bold] (created {item.created_at.isoformat()})")
    if not payload["swept_by_apply"]:
        console.print(
            "  [yellow]Not acted on by `nova retention apply`:[/yellow] capture capsule "
            f"(capsule.yaml); set `include_capture_capsules: true` in {_policy_path(registry)}."
        )
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
