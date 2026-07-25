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
"""``nova export-compliance`` — CLI surface for the ADR-0107 compliance exporters.

Thin wrappers over the shipped exporter builders. Each subcommand reads its input (a
capsule directory or a JSON file) and writes a report as JSON (to ``--out`` or stdout):

* ``genai-profile`` — NIST GenAI + CSA Agentic overlay, built from a capsule's NIST-RMF
  report (integrates :class:`~novafabric.compliance.export.nist_rmf.NISTAIRMFReporter`);
* ``iso42001`` — ISO/IEC 42001 control mapping from a declared control catalog;
* ``gpai53`` — a GPAI Art. 53 documentation form (first sealed revision);
* ``pmm`` — an Art. 72 post-market-monitoring report from monitoring findings.

The exporters themselves live under ``novafabric.compliance.export``; this module only
parses arguments and serialises results, so the compliance logic stays testable without a
CLI.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Optional

import typer
from pydantic import BaseModel
from rich.console import Console

from novafabric.compliance.export.genai_csa_profile import build_genai_csa_profile
from novafabric.compliance.export.gpai53 import build_gpai53_form
from novafabric.compliance.export.iso42001 import build_iso42001_mapping
from novafabric.compliance.export.nist_rmf import NISTAIRMFReporter
from novafabric.compliance.export.pmm import PmmFinding, build_pmm_report
from novafabric.compliance.export.provenance import EvidenceSourceRef

console = Console()

export_compliance_app = typer.Typer(
    name="export-compliance",
    help=(
        "Export EU AI Act / ISO / NIST compliance evidence from a capsule or a JSON "
        "input (ADR-0107): genai-profile, iso42001, gpai53, pmm."
    ),
    no_args_is_help=True,
)

# Reusable option annotations (kept short to respect the line-length gate).
_EvidenceOpt = Annotated[
    Optional[str],
    typer.Option("--evidence", help="Comma-separated governance-evidence kinds present"),
]
_OutOpt = Annotated[
    Optional[Path],
    typer.Option("--out", help="Write report JSON here (default: stdout)"),
]


def _emit(model: BaseModel, out: Optional[Path], *, label: str) -> None:
    """Write *model* as JSON to *out*, or print it to stdout."""
    payload = model.model_dump_json(indent=2)
    if out is not None:
        out.write_text(payload + "\n", encoding="utf-8")
        console.print(f"[green]✓[/green] {label} written to {out}")
    else:
        console.print_json(payload)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(f"could not read JSON from {path}: {exc}") from exc


def _split_evidence(evidence: Optional[str]) -> set[str]:
    if not evidence:
        return set()
    return {tok.strip() for tok in evidence.split(",") if tok.strip()}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@export_compliance_app.command("genai-profile")
def genai_profile_cmd(
    capsule: Annotated[
        Path, typer.Argument(help="Capsule directory to derive the NIST-RMF report from")
    ],
    evidence: _EvidenceOpt = None,
    out: _OutOpt = None,
) -> None:
    """NIST GenAI + CSA Agentic overlay over a capsule's NIST-RMF report (NF-097)."""
    if not (capsule / "capsule.yaml").exists():
        raise typer.BadParameter(f"{capsule} is not a capsule directory (no capsule.yaml)")
    rmf_report = NISTAIRMFReporter().build_report(capsule)
    report = build_genai_csa_profile(rmf_report, present_evidence=_split_evidence(evidence))
    _emit(report, out, label="GenAI/CSA profile")


@export_compliance_app.command("iso42001")
def iso42001_cmd(
    catalog: Annotated[
        Path,
        typer.Option("--catalog", help="JSON control catalog: [{control_id, evidence_kind?}]"),
    ],
    evidence: _EvidenceOpt = None,
    capsule_id: Annotated[
        Optional[str],
        typer.Option("--capsule-id", help="Capsule id to attribute evidenced controls to"),
    ] = None,
    out: _OutOpt = None,
) -> None:
    """ISO/IEC 42001 control-evidence mapping from a declared catalog (NF-095)."""
    entries = _load_json(catalog)
    if not isinstance(entries, list):
        raise typer.BadParameter("catalog JSON must be a list of {control_id, evidence_kind?}")
    ref = (
        EvidenceSourceRef(
            capsule_id=capsule_id,
            content_digest="sha256:" + "0" * 64,
            verified_at=_now_iso(),
        )
        if capsule_id
        else None
    )
    report = build_iso42001_mapping(entries, _split_evidence(evidence), capsule_ref=ref)
    _emit(report, out, label="ISO/IEC 42001 mapping")


@export_compliance_app.command("gpai53")
def gpai53_cmd(
    model: Annotated[str, typer.Option("--model", help="GPAI model name")],
    fields: Annotated[
        Path,
        typer.Option("--fields", help="JSON object of Art. 53(1) documentation fields"),
    ],
    out: _OutOpt = None,
) -> None:
    """GPAI Art. 53 Model Documentation Form — first sealed revision (NF-093)."""
    data = _load_json(fields)
    if not isinstance(data, dict) or not data:
        raise typer.BadParameter("fields JSON must be a non-empty object of {field: value}")
    form = build_gpai53_form(
        model,
        initial_fields={str(k): str(v) for k, v in data.items()},
        created_at=datetime.now(timezone.utc),
    )
    _emit(form, out, label="GPAI Art. 53 form")


@export_compliance_app.command("pmm")
def pmm_cmd(
    system: Annotated[str, typer.Option("--system", help="System name")],
    findings: Annotated[
        Path, typer.Option("--findings", help="JSON list of monitoring findings")
    ],
    occurred_at: Annotated[
        str,
        typer.Option("--occurred-at", help="ISO date/time anchoring referred incidents"),
    ],
    period_start: Annotated[
        str, typer.Option("--period-start", help="Monitoring period start")
    ] = "",
    period_end: Annotated[
        str, typer.Option("--period-end", help="Monitoring period end")
    ] = "",
    out: _OutOpt = None,
) -> None:
    """Art. 72 post-market-monitoring report; serious findings refer incidents (NF-091)."""
    raw = _load_json(findings)
    if not isinstance(raw, list):
        raise typer.BadParameter("findings JSON must be a list of monitoring findings")
    try:
        parsed = [PmmFinding(**item) for item in raw]
        anchor = datetime.fromisoformat(occurred_at)
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        report = build_pmm_report(
            system,
            period_start=period_start,
            period_end=period_end,
            findings=parsed,
            occurred_at=anchor,
        )
    except (ValueError, TypeError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    _emit(report, out, label="Art. 72 PMM report")
