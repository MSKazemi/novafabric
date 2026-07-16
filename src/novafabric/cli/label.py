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

"""``nova label`` — deployment labels over the registry (ADR-0113/0114, experimental).

A deployment label is a mutable named pointer (``production``, ``staging``,
custom) from an asset name to one immutable registry version. Moves are
append-only audit rows; ``latest`` is auto-maintained and read-only. At
capture time a ``<type>:<name>@<label>`` reference is frozen to the concrete
version + content hash, so capsules record the resolved pin, never the label.

**Protected labels (ADR-0114):** ``nova label protect`` marks a label as
requiring maker-checker approval. A protected label refuses direct ``set``;
instead ``propose-move`` (maker) creates an Ed25519-signed pending move and
``approve-move`` (a *distinct* checker principal) applies it — self-approval
is refused at the crypto level (key fingerprint + identity, ADR-0058).
"""

from __future__ import annotations

import getpass
import json
from typing import Annotated, Any, Optional

import typer
from rich.console import Console
from rich.table import Table

console = Console()

label_app = typer.Typer(
    name="label",
    help=(
        "Deployment labels: set, get, list, history + protected-label "
        "maker-checker: protect, propose-move, approve-move, status "
        "(experimental, ADR-0113/0114)."
    ),
    no_args_is_help=True,
)


def _parse_asset(asset: str) -> tuple[Optional[str], str]:
    """Split ``[<asset_type>:]<asset_name>`` into ``(asset_type, asset_name)``."""
    if ":" in asset:
        asset_type, _, asset_name = asset.partition(":")
        if not asset_type or not asset_name:
            raise typer.BadParameter(
                f"Invalid asset {asset!r}: expected [<asset_type>:]<asset_name>",
                param_hint="asset",
            )
        return asset_type, asset_name
    return None, asset


def _short_hash(content_hash: Optional[str]) -> str:
    if not content_hash:
        return "-"
    return content_hash.removeprefix("sha256:")[:12] + "…"


def _pointer_line(record: dict[str, Any]) -> str:
    suffix = "  (auto)" if record.get("auto") else ""
    return (
        f"{record['label']} → {record['target_version']}  "
        f"({_short_hash(record['content_hash'])}){suffix}"
    )


@label_app.command("set")
def label_set_cmd(
    asset: Annotated[
        str, typer.Argument(help="Asset as [<asset_type>:]<asset_name>, e.g. prompt:triage.")
    ],
    label: Annotated[
        str, typer.Argument(help="Label name (lowercase; 'latest' is reserved).")
    ],
    version: Annotated[
        str, typer.Argument(help="Existing immutable registry version to point at.")
    ],
    reason: Annotated[
        Optional[str],
        typer.Option("--reason", "-r", help="Free-text note recorded on the move."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the move record as JSON.")
    ] = False,
) -> None:
    """Point a label at a version, appending an audit row (experimental, ADR-0113).

    Fail-closed: the target version must exist; 'latest' is never settable.
    Re-pointing a label at its current target is a no-op (no row written).

    Scope: one label on one asset.

    \b
    Examples:
      nova label set prompt:triage production 4
      nova label set prompt:triage production 3 --reason "rollback: v4 regressed"
    """
    from novafabric.registry.labels import LabelError, set_label

    asset_type, asset_name = _parse_asset(asset)
    try:
        record, moved = set_label(
            asset_name,
            label,
            version,
            asset_type=asset_type,
            reason=reason,
        )
    except (LabelError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    if json_output:
        typer.echo(json.dumps(record, indent=2, ensure_ascii=False))
        return
    if not moved:
        console.print(
            f"[yellow]=[/yellow] '{label}' already points at "
            f"{record['target_version']} — no move recorded."
        )
        return
    previous = record["previous_version"] or "(unset)"
    console.print(
        f"[green]✓[/green] moved {label}: {previous} → "
        f"{record['target_version']}  "
        f"(moved_by={record['moved_by']}, at {record['moved_at']})"
    )


@label_app.command("get")
def label_get_cmd(
    asset: Annotated[
        str, typer.Argument(help="Asset as [<asset_type>:]<asset_name>.")
    ],
    label: Annotated[str, typer.Argument(help="Label name to resolve.")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the current-pointer record as JSON.")
    ] = False,
) -> None:
    """Resolve a label to its current version + content hash.

    Scope: one label on one asset.

    \b
    Examples:
      nova label get prompt:triage production
      nova label get prompt:triage latest --json
    """
    from novafabric.registry.labels import LabelError, get_label

    asset_type, asset_name = _parse_asset(asset)
    try:
        record = get_label(asset_name, label, asset_type=asset_type)
    except (LabelError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    if json_output:
        typer.echo(json.dumps(record, indent=2, ensure_ascii=False))
        return
    console.print(_pointer_line(record))


@label_app.command("list")
def label_list_cmd(
    asset: Annotated[
        str, typer.Argument(help="Asset as [<asset_type>:]<asset_name>.")
    ],
    json_output: Annotated[
        bool, typer.Option("--json", help="Print all current pointers as JSON.")
    ] = False,
) -> None:
    """List all labels on an asset and their current targets.

    Scope: one asset, all labels (auto 'latest' included).

    \b
    Examples:
      nova label list prompt:triage
    """
    from novafabric.registry.labels import list_labels

    asset_type, asset_name = _parse_asset(asset)
    records = list_labels(asset_name, asset_type=asset_type)
    if json_output:
        typer.echo(json.dumps(records, indent=2, ensure_ascii=False))
        return
    if not records:
        console.print(f"No versions or labels found for '{asset_name}'.")
        return
    table = Table(title=f"Labels on '{asset_name}'")
    table.add_column("Label", style="cyan")
    table.add_column("Target", justify="right")
    table.add_column("Content hash")
    table.add_column("Moved at")
    table.add_column("Moved by")
    for r in records:
        table.add_row(
            r["label"] + (" (auto)" if r.get("auto") else ""),
            r["target_version"],
            _short_hash(r["content_hash"]),
            str(r["moved_at"])[:19],
            r["moved_by"],
        )
    console.print(table)


@label_app.command("history")
def label_history_cmd(
    asset: Annotated[
        str, typer.Argument(help="Asset as [<asset_type>:]<asset_name>.")
    ],
    label: Annotated[
        Optional[str],
        typer.Argument(help="Optional label name — restrict the log to one label."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the full move log as JSON.")
    ] = False,
) -> None:
    """The append-only label-move audit log (newest first).

    Scope: one asset (optionally one label), all moves.

    \b
    Examples:
      nova label history prompt:triage
      nova label history prompt:triage production --json
    """
    from novafabric.registry.labels import LabelError, label_history

    asset_type, asset_name = _parse_asset(asset)
    del asset_type  # history is keyed by asset_name; type shown per row
    try:
        records = label_history(asset_name, label)
    except (LabelError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    if json_output:
        typer.echo(json.dumps(records, indent=2, ensure_ascii=False))
        return
    if not records:
        console.print(f"No label moves recorded for '{asset_name}'.")
        return
    scope = f"'{asset_name}'" + (f" label '{label}'" if label else "")
    table = Table(title=f"Label history of {scope}")
    table.add_column("Moved at")
    table.add_column("Label", style="cyan")
    table.add_column("Move", justify="right")
    table.add_column("Moved by")
    table.add_column("Reason")
    for r in records:
        previous = r["previous_version"] or "(unset)"
        table.add_row(
            str(r["moved_at"])[:19],
            r["label"],
            f"{previous} → {r['target_version']}",
            r["moved_by"],
            r["reason"] or "",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Protected labels — maker-checker (ADR-0114, experimental)
# ---------------------------------------------------------------------------


@label_app.command("protect")
def label_protect_cmd(
    asset: Annotated[
        str, typer.Argument(help="Asset as [<asset_type>:]<asset_name>.")
    ],
    label: Annotated[
        str, typer.Argument(help="Label to protect (lowercase; 'latest' is reserved).")
    ],
    required_approvals: Annotated[
        int,
        typer.Option(
            "--required-approvals",
            "-n",
            min=1,
            help="Distinct checker approvals required before a move applies.",
        ),
    ] = 1,
    policy_ref: Annotated[
        Optional[str],
        typer.Option(
            "--policy-ref",
            help=(
                "Path of a Rego policy (ADR-0019) that must allow the move. "
                "Absent = built-in default (distinct-approver invariants)."
            ),
        ),
    ] = None,
    note: Annotated[
        Optional[str],
        typer.Option("--note", help="Free-text rationale recorded on the config."),
    ] = None,
    unprotect: Annotated[
        bool,
        typer.Option(
            "--unprotect", help="Revert the label to free ADR-0113 behaviour."
        ),
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the protection config as JSON.")
    ] = False,
) -> None:
    """Mark a label protected — its moves then require maker-checker approval.

    Protecting an already-assigned label does not move it; it only governs
    future moves (experimental, ADR-0114). --unprotect reverts to free.

    Scope: one label on one asset.

    \b
    Examples:
      nova label protect prompt:triage production
      nova label protect prompt:triage production --required-approvals 2
      nova label protect prompt:triage production --unprotect
    """
    from novafabric.registry.labels import LabelError
    from novafabric.registry.protected_labels import protect_label

    asset_type, asset_name = _parse_asset(asset)
    del asset_type  # protection is keyed by asset_name
    try:
        record = protect_label(
            asset_name,
            label,
            protected=not unprotect,
            required_approvals=required_approvals,
            policy_ref=policy_ref,
            note=note,
        )
    except (LabelError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    if json_output:
        typer.echo(json.dumps(record, indent=2, ensure_ascii=False))
        return
    if record["protected"]:
        console.print(
            f"[green]✓[/green] '{label}' on '{asset_name}' is now protected "
            f"(required approvals: {record['required_approvals']}). Moves need "
            "'nova label propose-move' + 'nova label approve-move' by a "
            "second principal."
        )
    else:
        console.print(
            f"[yellow]✓[/yellow] '{label}' on '{asset_name}' is free again — "
            "'nova label set' applies immediately (ADR-0113)."
        )


@label_app.command("propose-move")
def label_propose_move_cmd(
    asset: Annotated[
        str, typer.Argument(help="Asset as [<asset_type>:]<asset_name>.")
    ],
    label: Annotated[str, typer.Argument(help="Protected label to move.")],
    to: Annotated[
        str,
        typer.Option("--to", help="Existing immutable registry version to move to."),
    ],
    reason: Annotated[
        Optional[str],
        typer.Option("--reason", "-r", help="Maker's stated reason for the move."),
    ] = None,
    identity: Annotated[
        str,
        typer.Option(
            "--identity",
            help="Proposer identity (Ed25519 keyring key; defaults to OS user).",
        ),
    ] = "",
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the pending-move record as JSON.")
    ] = False,
) -> None:
    """Propose a protected-label move (maker step) — the label does NOT move yet.

    Creates an Ed25519-signed pending move. A different principal must
    approve it with 'nova label approve-move' (experimental, ADR-0114).

    Scope: one label on one asset; at most one pending move per label.

    \b
    Examples:
      nova label propose-move prompt:triage production --to 8 --reason "v8 passed the eval gate"
    """
    from novafabric.registry.labels import LabelError
    from novafabric.registry.protected_labels import propose_move

    asset_type, asset_name = _parse_asset(asset)
    try:
        record = propose_move(
            asset_name,
            label,
            to,
            asset_type=asset_type,
            reason=reason,
            identity=identity or getpass.getuser(),
        )
    except (LabelError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    if json_output:
        typer.echo(json.dumps(record, indent=2, ensure_ascii=False))
        return
    previous = record["from_version"] or "(unset)"
    console.print(
        f"[green]✓[/green] proposed move {record['move_id']}: "
        f"{label} {previous} → {record['proposed_version']} "
        f"(proposed_by={record['proposed_by']})\n"
        f"  Awaiting approval — run [bold]nova label approve-move {asset} "
        f"{label} {record['move_id']}[/bold] as a different identity."
    )


@label_app.command("approve-move")
def label_approve_move_cmd(
    asset: Annotated[
        str, typer.Argument(help="Asset as [<asset_type>:]<asset_name>.")
    ],
    label: Annotated[str, typer.Argument(help="Protected label being moved.")],
    move_id: Annotated[str, typer.Argument(help="Pending move id (ULID).")],
    reject: Annotated[
        bool,
        typer.Option("--reject", help="Reject the move instead (terminal)."),
    ] = False,
    note: Annotated[
        Optional[str],
        typer.Option("--note", help="Optional checker comment on the decision."),
    ] = None,
    identity: Annotated[
        str,
        typer.Option(
            "--identity",
            help="Approver identity (Ed25519 keyring key; defaults to OS user).",
        ),
    ] = "",
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the updated move record as JSON.")
    ] = False,
) -> None:
    """Approve (or --reject) a pending protected-label move (checker step).

    Self-approval is refused: the approver's keyring key fingerprint and
    identity must both differ from the proposer's (SoD, ADR-0003/0058).
    When enough distinct approvals exist and the policy gate allows, the
    label is reassigned atomically with an ADR-0113 audit row that reuses
    this move's id (experimental, ADR-0114).

    Scope: one pending move.

    \b
    Examples:
      nova label approve-move prompt:triage production 01J2Q8ZK7M4YZ2K7N9DPBYK2WX
      nova label approve-move prompt:triage production 01J2Q8ZK7M4YZ2K7N9DPBYK2WX --reject
    """
    from novafabric.registry.labels import LabelError
    from novafabric.registry.protected_labels import approve_move

    asset_type, asset_name = _parse_asset(asset)
    del asset_type  # moves are keyed by asset_name
    try:
        record, detail = approve_move(
            asset_name,
            label,
            move_id,
            identity=identity or getpass.getuser(),
            reject=reject,
            note=note,
        )
    except (LabelError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    if json_output:
        typer.echo(json.dumps(record, indent=2, ensure_ascii=False))
        return
    previous = record["from_version"] or "(unset)"
    if record["state"] == "applied":
        console.print(
            f"[green]✓[/green] applied move {move_id}: "
            f"{label} {previous} → {record['proposed_version']} "
            f"(approvals: {len(record['approvals'])})"
        )
    elif record["state"] == "rejected":
        console.print(f"[yellow]✗[/yellow] rejected move {move_id} — label not moved.")
    else:
        console.print(f"[yellow]…[/yellow] move {move_id}: {detail}")


@label_app.command("status")
def label_status_cmd(
    asset: Annotated[
        str, typer.Argument(help="Asset as [<asset_type>:]<asset_name>.")
    ],
    label: Annotated[
        Optional[str],
        typer.Option("--label", "-l", help="Restrict to one label."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the full status record as JSON.")
    ] = False,
) -> None:
    """Label protection config, current targets, and pending moves.

    Scope: one asset (optionally one label) — experimental, ADR-0114.

    \b
    Examples:
      nova label status prompt:triage
      nova label status prompt:triage --label production --json
    """
    from novafabric.registry.labels import LabelError
    from novafabric.registry.protected_labels import label_status

    asset_type, asset_name = _parse_asset(asset)
    del asset_type  # status is keyed by asset_name
    try:
        status = label_status(asset_name, label)
    except (LabelError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    if json_output:
        typer.echo(json.dumps(status, indent=2, ensure_ascii=False))
        return

    protected = {p["label"]: p for p in status["protections"] if p["protected"]}
    if not status["pointers"] and not protected and not status["pending_moves"]:
        console.print(f"No labels, protection, or pending moves for '{asset_name}'.")
        return

    table = Table(title=f"Label status of '{asset_name}'")
    table.add_column("Label", style="cyan")
    table.add_column("Target", justify="right")
    table.add_column("Protected")
    table.add_column("Required approvals", justify="right")
    seen = set()
    for r in status["pointers"]:
        config = protected.get(r["label"])
        table.add_row(
            r["label"] + (" (auto)" if r.get("auto") else ""),
            r["target_version"],
            "yes" if config else "no",
            str(config["required_approvals"]) if config else "-",
        )
        seen.add(r["label"])
    for name, config in protected.items():
        if name not in seen:
            table.add_row(name, "(unset)", "yes", str(config["required_approvals"]))
    console.print(table)

    if status["pending_moves"]:
        moves = Table(title="Moves (maker-checker)")
        moves.add_column("Move id")
        moves.add_column("Label", style="cyan")
        moves.add_column("Move", justify="right")
        moves.add_column("State")
        moves.add_column("Approvals", justify="right")
        moves.add_column("Proposed by")
        for m in status["pending_moves"]:
            previous = m["from_version"] or "(unset)"
            moves.add_row(
                m["move_id"],
                m["label"],
                f"{previous} → {m['proposed_version']}",
                m["state"],
                f"{len([a for a in m['approvals'] if a['decision'] == 'approve'])}"
                f"/{m.get('required_approvals', 1)}",
                m["proposed_by"],
            )
        console.print(moves)
