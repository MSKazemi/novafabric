"""``nova safety-case`` — compile, verify, and export an Evidence-Grounded Safety Case.

Experimental (ADR-0095, slices C0-C2). The compiler core: ``build`` assembles a
Claims-Arguments-Evidence tree from a capsule, ``verify`` re-checks artifact hashes /
case_hash / invariant I1, and ``export`` renders the case as JSON or Markdown. The
evidence-bundle admissibility binding (slice C2) lives in
``novafabric.evidence.admissibility``. This ``safety_case_app`` is registered in
``cli/main.py``.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from novafabric.compliance.export.safety_case import render_safety_case
from novafabric.safetycase.compiler import SafetyCaseCompiler
from novafabric.safetycase.models import SafetyCase
from novafabric.safetycase.templates import KNOWN_TEMPLATE_IDS, TemplateError
from novafabric.safetycase.verifier import verify_exit_code, verify_safety_case

console = Console()

safety_case_app = typer.Typer(
    help=(
        "Evidence-Grounded Safety Case (CAE) operations: build a case from a "
        "capsule, verify it, export it (experimental, ADR-0095)."
    ),
    no_args_is_help=True,
)


class ExportFormat(str, Enum):
    json = "json"
    markdown = "markdown"
    annex_iv = "annex-iv"
    nist_rmf = "nist-rmf"


_OutOpt = Annotated[
    Optional[Path],
    typer.Option("--output", "-o", help="Output file (default: stdout)"),
]


def _resolve_capsule(ref: str) -> Path:
    candidate = Path(ref)
    if candidate.is_dir() and (candidate / "capsule.yaml").exists():
        return candidate
    console.print(f"[red]Not a valid capsule directory: {ref}[/red]")
    raise typer.Exit(code=1)


def _load_case(case_file: Path) -> SafetyCase:
    if not case_file.exists():
        console.print(f"[red]Safety-case file not found: {case_file}[/red]")
        raise typer.Exit(code=1)
    try:
        data = json.loads(case_file.read_text())
    except json.JSONDecodeError as exc:
        console.print(f"[red]Malformed safety-case JSON:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    try:
        return SafetyCase.model_validate(data)
    except ValueError as exc:
        console.print(f"[red]Invalid safety case:[/red] {exc}")
        raise typer.Exit(code=1) from exc


@safety_case_app.command("build")
def build_cmd(
    capsule: Annotated[str, typer.Argument(help="Capsule directory")],
    template: Annotated[
        str,
        typer.Option(
            "--template",
            help=f"Template id: one of {', '.join(KNOWN_TEMPLATE_IDS)}",
        ),
    ] = "clymer-generic-v0",
    output: _OutOpt = None,
) -> None:
    """Compile a safety case from a capsule against a CAE template.

    Backing states are assigned mechanically from evidence statistics; an
    unresolved template leaf becomes an honest UNSUPPORTED claim.

    Scope: single capsule.

    \b
    Examples:
      nova safety-case build path/to/capsule --template clymer-generic-v0
      nova safety-case build 01HX... --template eu-ai-act-annex-iv-v0 -o safety-case.json
    """
    capsule_dir = _resolve_capsule(capsule)
    try:
        case = SafetyCaseCompiler().build(capsule_dir, template)
    except TemplateError as exc:
        console.print(f"[red]Template error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    payload = json.dumps(case.to_record(), indent=2)
    if output:
        output.write_text(payload + "\n")
        console.print(
            f"[green]✓[/green] Safety case ({case.template_id}) → {output} "
            f"[top claim: {_state_of(case, case.top_claim_id)}]"
        )
    else:
        typer.echo(payload)


@safety_case_app.command("verify")
def verify_cmd(
    case_file: Annotated[Path, typer.Argument(help="Safety-case JSON file")],
    capsule: Annotated[
        str, typer.Option("--capsule", help="Capsule directory the case was built from")
    ],
) -> None:
    """Re-verify a safety case: artifact hashes, case_hash, and invariant I1.

    Exit codes: 0 OK; 3 case_hash mismatch; 4 artifact failure; 5 I1 violation.

    \b
    Examples:
      nova safety-case verify safety-case.json --capsule path/to/capsule
    """
    capsule_dir = _resolve_capsule(capsule)
    case = _load_case(case_file)
    verdict = verify_safety_case(case, capsule_dir)
    if verdict.ok:
        console.print(f"[green]✓[/green] {verdict.summary()}")
    else:
        console.print(f"[red]✗[/red] {verdict.summary()}")
    raise typer.Exit(code=verify_exit_code(verdict))


@safety_case_app.command("export")
def export_cmd(
    case_file: Annotated[Path, typer.Argument(help="Safety-case JSON file")],
    fmt: Annotated[
        ExportFormat, typer.Option("--format", help="Output format")
    ] = ExportFormat.markdown,
    output: _OutOpt = None,
) -> None:
    """Render a safety case as JSON, Markdown, an EU AI Act Annex IV section, or a
    NIST AI RMF report.

    Backing states render honestly: a CONTESTED claim surfaces its contest reason and
    an UNSUPPORTED claim is never laundered into "compliant".

    \b
    Examples:
      nova safety-case export safety-case.json --format markdown
      nova safety-case export safety-case.json --format annex-iv
      nova safety-case export safety-case.json --format nist-rmf
      nova safety-case export safety-case.json --format json -o case.json
    """
    case = _load_case(case_file)
    rendered = render_safety_case(case, fmt.value)
    if output:
        output.write_text(rendered + "\n")
        console.print(f"[green]✓[/green] Exported {fmt.value} → {output}")
    else:
        typer.echo(rendered)


# --- rendering helpers -------------------------------------------------------


def _state_of(case: SafetyCase, claim_id: str) -> str:
    claim = case.get_claim(claim_id)
    return claim.backing_state.value if claim is not None else "UNKNOWN"
