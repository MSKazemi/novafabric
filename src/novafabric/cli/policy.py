from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from novafabric.policy._opa_engine import _get_bundle_default

console = Console()
err_console = Console(stderr=True)
app = typer.Typer(
    help="Manage and test OPA Rego promotion policies.",
    no_args_is_help=True,
)


@app.command("test")
def policy_test(
    bundle: str = typer.Option(
        None,
        "--bundle",
        help="Rego bundle directory (default: built-in bundle)",
    ),
) -> None:
    """Run the OPA Rego test suite against the policy bundle.

    Requires `opa` on PATH. Install from https://www.openpolicyagent.org/

    Scope: policy bundle directory.

    \b
    Examples:
      # Test with the built-in bundle
      nova policy test

      # Test with a custom bundle
      nova policy test --bundle path/to/my-bundle/
    """
    bundle_path = bundle or str(_get_bundle_default())
    try:
        result = subprocess.run(
            ["opa", "test", bundle_path, "--verbose"], capture_output=False
        )
    except FileNotFoundError:
        console.print(
            "[red]opa binary not found on PATH. "
            "Install OPA: https://www.openpolicyagent.org/docs/latest/#running-opa[/red]"
        )
        raise typer.Exit(code=1)
    raise typer.Exit(code=result.returncode)


@app.command("explain")
def policy_explain(
    decision_id: str = typer.Argument(...),
    audit_log_path: str = typer.Option(
        "~/.local/share/novafabric/audit.jsonl",
        "--audit-log",
        help="Path to the audit log JSONL file",
    ),
) -> None:
    """Show the full decision record for a past policy evaluation.

    Scope: audit log (read-only).

    \b
    Examples:
      nova policy explain <decision-id>
      nova policy explain <decision-id> --audit-log /custom/audit.jsonl
    """
    from novafabric.audit import AuditLog

    path = Path(audit_log_path).expanduser()
    log = AuditLog(path)
    entries = log.query()
    match = [
        e
        for e in entries
        if e.entry_id == decision_id or e.details.get("decision_id") == decision_id
    ]
    if not match:
        console.print(f"[red]Decision ID not found: {decision_id}[/red]")
        raise typer.Exit(code=1)
    for e in match:
        console.print_json(e.model_dump_json(indent=2))


@app.command("sign")
def policy_sign(
    key: Path = typer.Option(..., "--key", help="Path to ECDSA P-256 private key PEM"),
    cert: Path = typer.Option(..., "--cert", help="Path to X.509 certificate PEM"),
    proposer_subjects: str = typer.Option(
        ..., "--proposer-subjects", help="Comma-separated list of proposer subject CNs"
    ),
    approver_subjects: str = typer.Option(
        ..., "--approver-subjects", help="Comma-separated list of approver subject CNs"
    ),
    bypass_valid_hours: int = typer.Option(
        24, "--bypass-valid-hours", help="Bypass validity window in hours (1-168)"
    ),
    db_path: Path = typer.Option(
        Path.home() / ".local" / "share" / "novafabric" / "merkle.db",
        "--db",
        help="SQLite DB path",
    ),
) -> None:
    """Sign and store a new promotion policy.

    Creates a versioned ECDSA-signed policy predicate controlling who can
    propose and who can approve promotions in the maker-checker workflow.
    The bypass validity window limits how long an emergency bypass is valid.

    Scope: policy store.

    \b
    Examples:
      nova policy sign \\
        --key ~/.novafabric/keys/policy.key \\
        --cert ~/.novafabric/keys/policy.crt \\
        --proposer-subjects "alice" \\
        --approver-subjects "bob,carol"
    """
    if not (1 <= bypass_valid_hours <= 168):
        err_console.print(
            f"[red]Error:[/red] --bypass-valid-hours must be between 1 and 168 "
            f"(got {bypass_valid_hours})"
        )
        raise typer.Exit(code=1)

    from novafabric.promote.exceptions import PredicateValidationError
    from novafabric.promote.policy_store import PolicyStore
    from novafabric.promote.predicates import (
        POLICY_PAYLOAD_TYPE,
        build_policy_predicate,
        sign_promote_envelope,
        validate_predicate,
    )

    proposers = [s.strip() for s in proposer_subjects.split(",") if s.strip()]
    approvers = [s.strip() for s in approver_subjects.split(",") if s.strip()]

    if not proposers:
        err_console.print("[red]Error:[/red] --proposer-subjects must not be empty")
        raise typer.Exit(code=1)
    if not approvers:
        err_console.print("[red]Error:[/red] --approver-subjects must not be empty")
        raise typer.Exit(code=1)

    predicate = build_policy_predicate(proposers, approvers, bypass_valid_hours)

    try:
        validate_predicate("promote_policy_v1.json", predicate)
    except PredicateValidationError as exc:  # pragma: no cover
        err_console.print(f"[red]Schema validation error:[/red] {exc}")
        raise typer.Exit(code=1)

    payload = json.dumps(predicate).encode()
    try:
        envelope = sign_promote_envelope(payload, POLICY_PAYLOAD_TYPE, key, cert)
    except Exception as exc:  # pragma: no cover
        err_console.print(f"[red]Signing error:[/red] {exc}")
        raise typer.Exit(code=1)

    policy_store = PolicyStore(db_path)
    version = policy_store.put(envelope.decode("utf-8"))

    console.print(f"[green]Policy signed and stored:[/green] version {version}")
    console.print(f"  proposers: {proposers}")
    console.print(f"  approvers: {approvers}")
    console.print(f"  bypass_valid_hours: {bypass_valid_hours}")


@app.command("list")
def policy_list(
    db: Optional[Path] = typer.Option(
        None,
        "--db",
        help=(
            "PolicyStore SQLite DB path "
            "(default: NOVAFABRIC_HOME/promote/policy.db or "
            "~/.local/share/novafabric/merkle.db)"
        ),
    ),
    namespace: Optional[str] = typer.Option(
        None,
        "--namespace",
        "-n",
        help="Filter signed policies to this namespace (default: all)",
    ),
    bundle: Optional[str] = typer.Option(
        None,
        "--bundle",
        help="Rego bundle directory (default: built-in bundle)",
    ),
) -> None:
    """List Rego policy files and signed promotion policies.

    Scope: policy bundle + policy store.

    \b
    Examples:
      nova policy list
      nova policy list --namespace production
    """
    from novafabric._paths import nova_home
    from novafabric.promote.policy_store import PolicyStore

    # ── Section 1: Rego bundle files ─────────────────────────────────────────
    bundle_path = Path(bundle) if bundle else _get_bundle_default()
    rego_files = sorted(bundle_path.rglob("*.rego")) if bundle_path.exists() else []

    if rego_files:
        tbl = Table(
            title=f"Rego Bundle  ({bundle_path})",
            show_header=True,
            header_style="bold cyan",
        )
        tbl.add_column("File", style="cyan")
        tbl.add_column("Size", justify="right")
        for f in rego_files:
            size = f.stat().st_size
            size_str = f"{size / 1024:.1f} KB" if size >= 1024 else f"{size} B"
            tbl.add_row(str(f.relative_to(bundle_path)), size_str)
        console.print(tbl)
    else:
        console.print(f"[yellow]No Rego files found in bundle:[/yellow] {bundle_path}")

    # ── Section 2: signed promotion policies ─────────────────────────────────
    # Resolve DB path: explicit flag → NOVAFABRIC_HOME/promote/policy.db → legacy default
    if db is not None:
        db_path = db
    else:
        home_db = nova_home() / "promote" / "policy.db"
        legacy_db = Path.home() / ".local" / "share" / "novafabric" / "merkle.db"
        db_path = home_db if home_db.exists() else legacy_db

    if not db_path.exists():
        console.print(
            f"\n[dim]No signed promotion policies found "
            f"(DB not yet created: {db_path})[/dim]"
        )
        return

    try:
        store = PolicyStore(db_path)
        rows = store.list_all(namespace=namespace)
        store.close()
    except Exception as exc:  # noqa: BLE001
        err_console.print(f"[red]Error reading PolicyStore ({db_path}):[/red] {exc}")
        raise typer.Exit(code=1)

    if not rows:
        ns_hint = f" (namespace={namespace!r})" if namespace else ""
        console.print(f"\n[yellow]No signed promotion policies found{ns_hint}.[/yellow]")
        return

    ptbl = Table(
        title=f"Signed Promotion Policies  ({db_path})",
        show_header=True,
        header_style="bold magenta",
    )
    ptbl.add_column("Version", justify="right")
    ptbl.add_column("Namespace")
    ptbl.add_column("Created At")
    for row in rows:
        ptbl.add_row(str(row["version"]), str(row["namespace"]), str(row["created_at"]))
    console.print(ptbl)
