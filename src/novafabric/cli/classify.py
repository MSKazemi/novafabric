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
"""CLI for the AI system risk-tier classifier (ADR-0056).

Commands:
  nova classify run              — classify a system from flags or a YAML file
  nova classify list-vocabularies — list available vocabulary versions
  nova classify from-capsule     — classify from captured capsule metadata
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import typer
import yaml
from rich.console import Console
from rich.table import Table

from novafabric.governance import AISystemRecord, ClassificationResult, RiskTierClassifier
from novafabric.governance.classifier import _DEFAULT_VOCAB_DIR

app = typer.Typer(
    name="classify",
    help="Classify an AI system's risk tier (EU AI Act, NIST AI RMF, OMB M-24-10).",
    no_args_is_help=True,
)

console = Console()

_TIER_COLOUR: dict[str, str] = {
    "prohibited": "red",
    "high_risk": "yellow",
    "limited_risk": "cyan",
    "minimal_risk": "green",
}

_IMPACT_COLOUR: dict[str, str] = {
    "critical": "red",
    "high": "yellow",
    "medium": "cyan",
    "low": "green",
}


def _render_result(result: ClassificationResult, name: str) -> None:
    """Render a :class:`ClassificationResult` as a rich table."""
    tier_colour = _TIER_COLOUR.get(result.eu_ai_act_tier, "white")
    impact_colour = _IMPACT_COLOUR.get(result.nist_rmf_impact, "white")

    table = Table(title=f"Risk Classification — {name}", show_header=True)
    table.add_column("Framework", style="bold")
    table.add_column("Result")

    table.add_row(
        "EU AI Act — Tier",
        f"[{tier_colour}]{result.eu_ai_act_tier.upper()}[/{tier_colour}]",
    )
    table.add_row("EU AI Act — Role", result.eu_ai_act_role)

    if result.eu_ai_act_annex_iii_items:
        items_text = "\n".join(
            f"  [{i.item_id}] {i.description}" for i in result.eu_ai_act_annex_iii_items
        )
        table.add_row("EU AI Act — Matched Items", items_text)

    table.add_row(
        "NIST AI RMF — Impact",
        f"[{impact_colour}]{result.nist_rmf_impact.upper()}[/{impact_colour}]",
    )

    if result.omb_flags:
        table.add_row("OMB M-24-10 — Flags", ", ".join(result.omb_flags))
    else:
        table.add_row("OMB M-24-10 — Flags", "[dim](none)[/dim]")

    table.add_row("Vocabulary version", result.vocabulary_version)
    console.print(table)
    console.print()
    console.print("[bold]Rationale[/bold]")
    console.print(result.rationale)


@app.command("run")
def classify_run(
    input: Optional[Path] = typer.Option(
        None, "--input", "-i", help="YAML file describing the AI system"
    ),
    name: Optional[str] = typer.Option(None, "--name", help="AI system name"),
    domain: Optional[str] = typer.Option(None, "--domain", help="Use-case domain"),
    context: Optional[str] = typer.Option(None, "--context", help="Deployment context"),
    description: Optional[str] = typer.Option(
        None, "--description", help="Free-text description of the system"
    ),
    uses_biometrics: bool = typer.Option(False, "--biometrics/--no-biometrics"),
    affects_rights: bool = typer.Option(
        False, "--affects-rights/--no-affects-rights"
    ),
    is_general_purpose: bool = typer.Option(
        False, "--general-purpose/--no-general-purpose"
    ),
    role: str = typer.Option(
        "deployer", "--role", help="Operator role: provider or deployer"
    ),
    subject_count: Optional[int] = typer.Option(
        None, "--subject-count", help="Estimated number of persons affected"
    ),
    output_json: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """Classify an AI system's risk tier.

    Assesses risk against EU AI Act Annex III prohibited/high-risk lists,
    NIST AI RMF impact levels, and OMB M-24-10 minimum practices.

    Scope: single system description (from flags or YAML file).

    \b
    Examples:
      # Classify from command-line flags
      nova classify run --name "My Agent" --purpose "customer support"

      # Classify from a YAML file
      nova classify run --file system.yaml
    """
    if input is not None:
        if not input.exists():
            console.print(f"[red]x[/red] File not found: {input}")
            raise typer.Exit(code=2)
        with open(input) as fh:
            data = yaml.safe_load(fh)
        if not isinstance(data, dict):
            console.print("[red]x[/red] Input YAML did not parse as a mapping.")
            raise typer.Exit(code=2)
        # CLI flags override file values when supplied explicitly
        if name is not None:
            data["name"] = name
        if domain is not None:
            data["use_case_domain"] = domain
        if context is not None:
            data["deployment_context"] = context
        if description is not None:
            data["description"] = description
        record = AISystemRecord.model_validate(data)
    else:
        # Build from flags — all three required
        missing = []
        if not name:
            missing.append("--name")
        if not domain:
            missing.append("--domain")
        if not context:
            missing.append("--context")
        if missing:
            console.print(
                f"[red]x[/red] Required: {', '.join(missing)} "
                "(or use --input to supply a YAML file)"
            )
            raise typer.Exit(code=2)

        record = AISystemRecord(
            name=name,  # type: ignore[arg-type]
            description=description or f"{name} operating in {domain}",
            use_case_domain=domain,  # type: ignore[arg-type]
            deployment_context=context,  # type: ignore[arg-type]
            uses_biometrics=uses_biometrics,
            affects_fundamental_rights=affects_rights,
            is_general_purpose=is_general_purpose,
            operator_role=role,  # type: ignore[arg-type]
            subject_count_estimate=subject_count,
        )

    classifier = RiskTierClassifier()
    result = classifier.classify(record)

    if output_json:
        typer.echo(result.model_dump_json(indent=2))
    else:
        _render_result(result, record.name)

    if result.eu_ai_act_tier == "prohibited":
        raise typer.Exit(code=1)


@app.command("list-vocabularies")
def list_vocabularies() -> None:
    """List available EU AI Act and NIST AI RMF vocabulary versions.

    Scope: global.

    \b
    Examples:
      nova classify list-vocabularies
    """
    vocab_dir = _DEFAULT_VOCAB_DIR
    table = Table(title="Available Vocabularies", show_header=True)
    table.add_column("Framework", style="bold")
    table.add_column("Version file")
    table.add_column("Path")

    for framework_dir in sorted(vocab_dir.iterdir()):
        if not framework_dir.is_dir():
            continue
        for vocab_file in sorted(framework_dir.glob("*.yaml")):
            try:
                with open(vocab_file) as fh:
                    meta = yaml.safe_load(fh)
                version = meta.get("version", "?")
                reference = meta.get("reference", meta.get("regulation", ""))
            except Exception:
                version = "?"
                reference = ""
            table.add_row(
                framework_dir.name,
                f"{version} — {reference}",
                str(vocab_file.relative_to(vocab_dir.parent)),
            )

    console.print(table)


@app.command("from-capsule")
def from_capsule(
    capsule_id: str = typer.Argument(..., help="Run capsule ID to classify from"),
    data_dir: Path = typer.Option(
        Path.home() / "novafabric-data",
        "--data-dir",
        help="Root novafabric-data directory",
    ),
    output_json: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """Classify an AI system from captured capsule metadata.

    Extracts system description fields from a capsule and runs them through
    the risk-tier classifier.

    Scope: single capsule.

    \b
    Examples:
      nova classify from-capsule path/to/my-capsule/
    """
    capsule_dir = data_dir / "capsules" / capsule_id
    if not capsule_dir.exists():
        console.print(f"[red]x[/red] Capsule directory not found: {capsule_dir}")
        raise typer.Exit(code=2)

    manifest_path = capsule_dir / "capsule.yaml"
    if not manifest_path.exists():
        console.print(f"[red]x[/red] capsule.yaml not found in: {capsule_dir}")
        raise typer.Exit(code=2)

    with open(manifest_path) as fh:
        manifest = yaml.safe_load(fh)

    if not isinstance(manifest, dict):
        console.print("[red]x[/red] capsule.yaml did not parse as a mapping.")
        raise typer.Exit(code=2)

    # Extract what we can from the capsule manifest
    asset_meta: dict[str, Any] = manifest.get("asset", {}) or {}
    run_id: str = str(manifest.get("run_id", capsule_id))
    sys_name: str = str(asset_meta.get("name", run_id))
    sys_desc: str = str(asset_meta.get("description", f"AI system from capsule {run_id}"))
    domain: str = str(asset_meta.get("domain", "general"))
    context: str = str(asset_meta.get("deployment_context", asset_meta.get("use_case", domain)))

    record = AISystemRecord(
        name=sys_name,
        description=sys_desc,
        use_case_domain=domain,
        deployment_context=context,
    )

    classifier = RiskTierClassifier()
    result = classifier.classify(record)
    result = result.model_copy(update={"classification_capsule_id": capsule_id})

    if output_json:
        typer.echo(result.model_dump_json(indent=2))
    else:
        _render_result(result, sys_name)

    if result.eu_ai_act_tier == "prohibited":
        raise typer.Exit(code=1)
