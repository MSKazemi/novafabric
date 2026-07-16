from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Annotated

import typer

from novafabric.capsule.cli import capsule_phase3_app
from novafabric.cli.aibom import aibom_app
from novafabric.cli.annotate import annotate_app
from novafabric.cli.api_proxy import api_proxy_cmd
from novafabric.cli.approve import approve_cmd
from novafabric.cli.asset import asset_app
from novafabric.cli.assure import assure_cmd
from novafabric.cli.assure_case import assure_case_cmd
from novafabric.cli.assure_coverage import assure_coverage_cmd
from novafabric.cli.audit import app as audit_app
from novafabric.cli.backup import app as backup_app
from novafabric.cli.backup import restore_cmd
from novafabric.cli.capsule import app as capsule_app
from novafabric.cli.capture import capture_cmd
from novafabric.cli.capture_level import app as capture_level_app
from novafabric.cli.classify import app as classify_app
from novafabric.cli.collector import collector_app
from novafabric.cli.comment import app as comment_app
from novafabric.cli.cost import app as cost_app
from novafabric.cli.cost_attribute import cost_attribute_cmd
from novafabric.cli.cost_fairness import cost_fairness_cmd
from novafabric.cli.cost_usage_breakdown import cost_usage_breakdown_cmd
from novafabric.cli.daemon import app as daemon_app
from novafabric.cli.dataset import dataset_app
from novafabric.cli.diagnose import diagnose_cmd
from novafabric.cli.diff import diff_cmd
from novafabric.cli.doctor import doctor_cmd
from novafabric.cli.drift import app as drift_app
from novafabric.cli.dsar import dsar_app
from novafabric.cli.energy import app as energy_app
from novafabric.cli.erasure import app as erasure_app
from novafabric.cli.euaiact import euaiact_app
from novafabric.cli.eval_run import eval_app
from novafabric.cli.events import events_app
from novafabric.cli.evidence import evidence_app
from novafabric.cli.experiment import experiment_app
from novafabric.cli.export_accessibility import export_accessibility_claim_cmd
from novafabric.cli.export_blob import export_blob_cmd
from novafabric.cli.export_c2pa import export_c2pa_cmd
from novafabric.cli.export_citizen import export_citizen_explanation_cmd
from novafabric.cli.export_control_attestation import export_control_attestation_cmd
from novafabric.cli.export_election import export_election_disclosure_cmd
from novafabric.cli.export_evidence import (
    export_aibom_cmd,
    export_annex_iv_cmd,
    export_evidence_cmd,
    export_hipaa_proof_cmd,
    export_nis2_cmd,
    export_nist_rmf_cmd,
    export_ropa_cmd,
)
from novafabric.cli.export_examiner import app as export_examiner_app
from novafabric.cli.export_finance import export_model_risk_cmd
from novafabric.cli.export_foia import export_foia_cmd
from novafabric.cli.export_html import export_cmd
from novafabric.cli.export_part11 import export_part11_cmd
from novafabric.cli.export_public import export_public_annex_viii_cmd
from novafabric.cli.export_public_disclosure import export_public_disclosure_cmd
from novafabric.cli.export_public_incident import export_public_incident_cmd
from novafabric.cli.export_rai import export_rai_scorecard_cmd
from novafabric.cli.export_rocrate import export_rocrate_cmd
from novafabric.cli.export_system_card import export_system_card_cmd
from novafabric.cli.export_transparency_register import export_transparency_register_cmd
from novafabric.cli.export_whistleblower import export_whistleblower_cmd
from novafabric.cli.forensics import forensics_app
from novafabric.cli.graph import graph_app
from novafabric.cli.hold import app as hold_app
from novafabric.cli.incident import incident_app
from novafabric.cli.ingest_capsule import ingest_capsule_cmd
from novafabric.cli.init_ import init_cmd
from novafabric.cli.inspect_ import inspect_cmd
from novafabric.cli.kg import kg_app
from novafabric.cli.label import label_app
from novafabric.cli.ledger import ledger_app
from novafabric.cli.lineage import lineage_app
from novafabric.cli.lineage_migrate import lineage_store_app
from novafabric.cli.list_ import list_cmd
from novafabric.cli.login import login_cmd, logout_cmd
from novafabric.cli.mcp_ import app as mcp_app
from novafabric.cli.mcp_proxy import mcp_proxy_cmd
from novafabric.cli.media import media_app
from novafabric.cli.merkle_tree import merkle_tree_cmd
from novafabric.cli.migrate import migrate_to_postgres_cmd
from novafabric.cli.migrate_capsule import migrate_capsule_cmd
from novafabric.cli.migrate_schema import migrate_schema_cmd
from novafabric.cli.passport import passport_app
from novafabric.cli.pii_erase import app as pii_app
from novafabric.cli.policy import app as policy_app
from novafabric.cli.pricing import app as pricing_app
from novafabric.cli.promote import promote_app
from novafabric.cli.prompt import prompt_app
from novafabric.cli.query import query_cmd
from novafabric.cli.rebuild import app as rebuild_app
from novafabric.cli.redact import redact_cmd, subject_proof_cmd
from novafabric.cli.redaction_xray import redaction_xray_cmd
from novafabric.cli.register import register_cmd
from novafabric.cli.replay import replay_cmd
from novafabric.cli.report import report_cmd
from novafabric.cli.retention import app as retention_app
from novafabric.cli.rollback import rollback_cmd
from novafabric.cli.safety_case import safety_case_app
from novafabric.cli.scan_secrets import scan_secrets_cmd
from novafabric.cli.schema import app as schema_app
from novafabric.cli.score import score_app
from novafabric.cli.seal_propose import seal_app
from novafabric.cli.serve import serve_cmd
from novafabric.cli.server import server_app
from novafabric.cli.session import session_app
from novafabric.cli.storage_scale import app as storage_scale_app
from novafabric.cli.suggest_register import suggest_register_cmd
from novafabric.cli.support_bundle import support_bundle_cmd
from novafabric.cli.toolschema import app as toolschema_app
from novafabric.cli.trend import trend_cmd
from novafabric.cli.trust_radar import trust_radar_cmd
from novafabric.cli.unregister import unregister_cmd
from novafabric.cli.validate import validate_cmd
from novafabric.cli.verify import verify_cmd
from novafabric.cli.verify_envelope import verify_envelope_cmd
from novafabric.cli.view import view_app
from novafabric.metadata_store.cli import metadata_db_app


def _get_version() -> str:
    try:
        return version("novafabric")
    except PackageNotFoundError:
        return "unknown"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"novafabric {_get_version()}")
        raise typer.Exit()


app = typer.Typer(
    name="novafabric",
    help="Capture, replay, audit, and govern AI agent runs.",
    no_args_is_help=True,
)


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option(
            "--version", "-V",
            help="Show NovaFabric version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Capture, replay, audit, and govern AI agent runs."""

app.command("init")(init_cmd)
app.command(
    "capture",
    context_settings={"allow_extra_args": True, "allow_interspersed_args": False},
)(capture_cmd)
app.command("register")(register_cmd)
app.command("suggest-register")(suggest_register_cmd)
app.command("list")(list_cmd)
app.command("inspect")(inspect_cmd)
app.command("ingest-capsule")(ingest_capsule_cmd)
app.command(
    "mcp-proxy",
    context_settings={"allow_extra_args": True, "allow_interspersed_args": False},
)(mcp_proxy_cmd)
app.add_typer(
    mcp_app,
    name="mcp",
    help="Scan MCP servers for supply-chain risks.",
)
app.command("api-proxy")(api_proxy_cmd)
app.command("verify-envelope")(verify_envelope_cmd)
app.command("merkle-tree")(merkle_tree_cmd)
app.add_typer(
    forensics_app,
    name="forensics",
    help="Read-only incident forensics over sealed evidence (experimental, ADR-0155).",
)
app.add_typer(
    dsar_app,
    name="dsar",
    help="Read-only subject-rights (DSAR) assembly over sealed evidence (experimental, ADR-0161).",
)
app.add_typer(
    drift_app,
    name="drift",
    help="Offline drift detection over sealed capsule windows (experimental, ADR-0147).",
)
app.add_typer(
    toolschema_app,
    name="toolschema",
    help="Tool-schema replay-impact analysis over historical payloads (experimental, ADR-0148).",
)
app.add_typer(
    daemon_app,
    name="daemon",
    help="Control the warm capture daemon (resident emitter).",
)
app.add_typer(
    promote_app,
    name="promote",
    help="Promote an asset through the lifecycle (direct or maker-checker).",
)
app.add_typer(
    prompt_app,
    name="prompt",
    help=(
        "Prompt versioning: register, get, list, history, diff + "
        "composition: compose, tree (experimental, ADR-0112/0115)."
    ),
)
app.add_typer(
    label_app,
    name="label",
    help=(
        "Deployment labels: set, get, list, history + protected-label "
        "maker-checker: protect, propose-move, approve-move, status "
        "(experimental, ADR-0113/0114)."
    ),
)
app.add_typer(backup_app, name="backup")
app.command("restore")(restore_cmd)
app.command("approve")(approve_cmd)
app.command("rollback")(rollback_cmd)
app.command("unregister")(unregister_cmd)
app.add_typer(eval_app, name="eval", help="Run and manage evaluation suites.")
app.add_typer(
    experiment_app,
    name="experiment",
    help="Dataset-experiment harness: run, list, show, compare (experimental, ADR-0120).",
)
app.add_typer(
    media_app,
    name="media",
    help=(
        "Content-addressed media on model calls: list "
        "(experimental, ADR-0125)."
    ),
)
app.add_typer(
    session_app,
    name="session",
    help=(
        "Group N independent runs into one multi-turn session: new, add, "
        "list, show (experimental, ADR-0122); replay them in order "
        "(experimental, ADR-0123)."
    ),
)
app.add_typer(
    events_app,
    name="events",
    help="Lifecycle event log and outbound webhooks (experimental, opt-in).",
)
app.command("export-evidence")(export_evidence_cmd)
app.add_typer(
    evidence_app,
    name="evidence",
    help="Completeness assertion, criterion bindings, re-performance attestation (experimental).",
)
app.command("export-annex-iv")(export_annex_iv_cmd)
app.command("export-nis2")(export_nis2_cmd)
app.command("export-ropa")(export_ropa_cmd)
app.command("export-aibom")(export_aibom_cmd)
app.command("export-hipaa-proof")(export_hipaa_proof_cmd)
app.command("export-nist-rmf")(export_nist_rmf_cmd)
app.add_typer(
    export_examiner_app,
    name="export-examiner",
    help="Export examiner-mode evidence packages (BagIt, FDA PCCP, ISO 42001).",
)
app.command("export")(export_cmd)
app.command("export-blob")(export_blob_cmd)
app.command("export-rocrate")(export_rocrate_cmd)
app.command("export-c2pa")(export_c2pa_cmd)
app.command("export-model-risk")(export_model_risk_cmd)
app.command("export-public-annex-viii")(export_public_annex_viii_cmd)
app.command("export-transparency-register")(export_transparency_register_cmd)
app.command("export-public-disclosure")(export_public_disclosure_cmd)
app.command("export-foia")(export_foia_cmd)
app.command("export-whistleblower")(export_whistleblower_cmd)
app.command("export-citizen-explanation")(export_citizen_explanation_cmd)
app.command("export-public-incident")(export_public_incident_cmd)
app.command("export-election-disclosure")(export_election_disclosure_cmd)
app.command("export-accessibility-claim")(export_accessibility_claim_cmd)
app.command("export-control-attestation")(export_control_attestation_cmd)
app.command("export-rai-scorecard")(export_rai_scorecard_cmd)
app.command("export-part11")(export_part11_cmd)
app.command("export-system-card")(export_system_card_cmd)
app.add_typer(
    euaiact_app,
    name="euaiact",
    help="EU AI Act Article 12 compliance: structured log export for authority access requests.",
)
app.add_typer(
    aibom_app,
    name="aibom",
    help="AI Bill of Materials (AI-SBOM) — CycloneDX ML-BOM v1.7 status and CRA compliance.",
)
app.add_typer(
    dataset_app,
    name="dataset",
    help="Dataset supply-chain provenance (signed provenance cards, NF-058).",
)
app.add_typer(
    incident_app,
    name="incident",
    help="Incident records with EU AI Act Art. 73 deadline clock (experimental).",
)
app.add_typer(
    collector_app,
    name="collector",
    help="Event-buffer operations: offset-replay rebuild (experimental, ADR-0020).",
)
app.add_typer(
    comment_app,
    name="comment",
    help="Append-only comments on capsule evidence (experimental, ADR-0121).",
)
app.add_typer(
    annotate_app,
    name="annotate",
    help="Human annotation queues — route subjects to reviewers, emit HUMAN scores "
    "(experimental, ADR-0118).",
)
app.add_typer(
    score_app,
    name="score",
    help="Submit externally-computed scores into a capsule's append-only "
    "scores.jsonl (experimental, ADR-0119).",
)
app.command("diff")(diff_cmd)
app.command("diagnose")(diagnose_cmd)
app.command("redact")(redact_cmd)
app.command("subject-proof")(subject_proof_cmd)
app.command("replay")(replay_cmd)
app.command("report")(report_cmd)
app.command("query")(query_cmd)
app.add_typer(
    view_app,
    name="view",
    help="Saved views: named, persisted nova query definitions (experimental, ADR-0130).",
)
app.command("trend")(trend_cmd)
app.command("scan-secrets")(scan_secrets_cmd)
app.command("assure")(assure_cmd)
app.command("assure-case")(assure_case_cmd)
app.command("assure-coverage")(assure_coverage_cmd)
app.command("trust-radar")(trust_radar_cmd)
app.command("redaction-xray")(redaction_xray_cmd)
app.command("validate")(validate_cmd)
app.command("verify")(verify_cmd)
app.command("serve")(serve_cmd)
app.add_typer(lineage_app, name="lineage")
app.add_typer(
    graph_app,
    name="graph",
    help="Execution-graph projections from captured capsules (experimental, ADR-0124).",
)
app.add_typer(
    lineage_store_app,
    name="lineage-store",
    help="Lineage store migration and deployment.",
)
app.add_typer(
    hold_app,
    name="hold",
    help="Place and release legal holds to prevent capsule deletion.",
)
app.add_typer(
    retention_app,
    name="retention",
    help="Apply data-retention policy bindings: plan, apply, status, explain (ADR-0134).",
)
app.add_typer(
    asset_app,
    name="asset",
    help="Asset lifecycle operations (diff, etc.).",
)
app.add_typer(capsule_app, name="capsule")
app.add_typer(
    rebuild_app,
    name="rebuild-metadata-db",
    help="Rebuild the metadata database from the manifest chain log (disaster recovery).",
)
app.add_typer(policy_app, name="policy", help="Manage and test OPA Rego promotion policies.")
app.add_typer(
    audit_app,
    name="audit",
    help="Map capsule evidence to regulatory controls and report coverage gaps.",
)
app.add_typer(
    seal_app,
    name="seal",
    help="NovaSeal cryptographic signing (propose, approve, verify).",
)
app.command("doctor")(doctor_cmd)
app.command("support-bundle")(support_bundle_cmd)
app.add_typer(
    server_app,
    name="server",
    help="Manage the multi-user REST API server.",
)
app.command("login")(login_cmd)
app.command("logout")(logout_cmd)
app.command("migrate-to-postgres")(migrate_to_postgres_cmd)
app.command("migrate")(migrate_capsule_cmd)
app.command("migrate-schema")(migrate_schema_cmd)

app.add_typer(
    capsule_phase3_app,
    name="run",
    help="Parent/child capsule operations for distributed runs.",
)
app.add_typer(
    metadata_db_app,
    name="db",
    help="Manage the metadata database (migrations, schema upgrades).",
)
app.add_typer(
    classify_app,
    name="classify",
    help="Classify an AI system's risk tier (EU AI Act, NIST AI RMF, OMB M-24-10).",
)
app.add_typer(
    safety_case_app,
    name="safety-case",
    help="Compile/verify/export an evidence-grounded safety case (ADR-0095).",
)
app.add_typer(
    ledger_app,
    name="ledger",
    help="Adversary-anchored accountability ledger: anchor/verify/status (ADR-0094).",
)
app.add_typer(
    kg_app,
    name="kg",
    help="Capsule Knowledge Graph: ingest, query, and inspect (requires novafabric[scale-kg]).",
)

# Top-level `nova new-run-id` shortcut (FR-27)
_new_run_id_cb = capsule_phase3_app.registered_commands[0].callback
assert _new_run_id_cb is not None
app.command(
    "new-run-id",
    help="Print a fresh run ID for use as NOVAFABRIC_GLOBAL_RUN_ID.",
)(_new_run_id_cb)


app.add_typer(
    schema_app,
    name="schema",
    help="List capsule event schema types.",
)
cost_app.command("fairness")(cost_fairness_cmd)
# NF-148 (ADR-0146 D3): wasted/failure-spend attribution over captured run cost + status.
cost_app.command("attribute")(cost_attribute_cmd)
# ADR-0132 D3/D4: token usage-type composition over the manifest usage_totals aggregate.
cost_app.command("usage-breakdown")(cost_usage_breakdown_cmd)
app.add_typer(
    cost_app,
    name="cost",
    help="Report LLM cost attribution per run.",
)
app.add_typer(
    pricing_app,
    name="pricing",
    help="Local model-pricing catalog: list, show, add (ADR-0133, experimental).",
)
app.add_typer(
    energy_app,
    name="energy",
    help="Energy-Anchored Action Receipts: measure/attest/verify/report (ADR-0093).",
)
app.add_typer(
    storage_scale_app,
    name="storage",
    help="Inspect and validate WORM storage objects.",
)
app.add_typer(
    erasure_app,
    name="erasure",
    help="Request and track GDPR PII erasure for a data subject.",
)
app.add_typer(
    pii_app,
    name="pii",
    help="GDPR Art.17 PII crypto-shredding — destroy DEKs to erase data subjects (ADR-0069).",
)
# NF-179 (ADR-0149): portable agent-passport projection — offline green/amber/red.
app.add_typer(passport_app, name="passport")

# `nova policy capture-level get/set` — nested under existing policy typer
policy_app.add_typer(
    capture_level_app,
    name="capture-level",
    help="Get or set the capture-level policy for new runs.",
)

if __name__ == "__main__":
    app()
