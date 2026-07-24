"""Generic compliance-export registry (ADR-0200 §2, ADR-0183 router pattern).

One router replaces the bespoke-route-per-kind pattern for the Wave-A CLI-only
export commands:

- ``GET  /api/compliance/export/kinds``  — server-driven catalog
  (``[{kind, label, cli_equivalent, fields[], output, note}]``) so the single
  React panel renders forms dynamically with zero TypeScript registry drift;
- ``POST /api/compliance/export/{kind}`` — validates the JSON body against the
  kind's field spec, dispatches the ``EXPORT_KINDS`` runner (the *same* pure
  builder function the CLI command calls — never a subprocess), and returns
  ``{ok, run_id, document, cli_equivalent, note}`` mirroring the bespoke
  ``/api/compliance/export/*`` routes.

Wave-A wiring status (per ADR-0200 §2 — honest exclusion beats a broken panel):

- **All 13 Wave-A kinds are wired; none had to be excluded.** Every one is a
  pure, headless document builder — JSON fields in, JSON document out; no
  subprocess, no interactive input, no server-side state it could miss.
- **No Wave-A kind produces a zip.** The CLI half of each renders a JSON
  document only, so ``output`` is ``"document"`` for the whole catalog; the
  ``"zip"`` output value is reserved for bespoke zip routes (rocrate, examiner)
  migrating into the registry later.
- **No Wave-A kind resolves a capsule server-side.** Each CLI command takes a
  self-contained JSON document (refs/digests, never raw capsule bytes), so the
  registry never *requires* ``run_id``. When a client volunteers ``run_id`` it
  is resolved against the capsule store (404 when unknown) purely to validate
  the reference, then echoed back — mirroring ``_resolve_capsule`` semantics
  without importing ``serve.app`` (that import would be circular).

Every export is audit-logged with its ``cli_equivalent`` — not because it
mutates state (it does not) but because disclosure artifacts leave the trust
boundary. The audit sink is an injected callable (the ``serve`` app passes
``novafabric.serve.audit.append``) so this module never reaches into app
internals. Errors: unknown kind → 404, missing/mistyped required field → 422,
builder refusal (``ValueError``/``TypeError``/``KeyError``) → 400 with the
message, missing optional dependency → 501 with an install hint.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from novafabric.serve.capsule_loader import (
    discover_capsule_dirs,
    load_capsule_manifest,
)

# ---------- field + kind specs ----------


@dataclass(frozen=True)
class FieldSpec:
    """One input field of an export kind, as rendered by the dynamic panel."""

    key: str
    label: str
    type: str  # "string" | "boolean" | "json"
    required: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "required": self.required,
        }


@dataclass(frozen=True)
class ExportKindSpec:
    """One registry entry: catalog metadata + the runner the CLI command calls."""

    kind: str
    label: str
    cli_equivalent: str
    fields: list[FieldSpec]
    runner: Callable[[dict[str, Any]], dict[str, Any]]
    output: str = "document"  # "document" | "zip" (no Wave-A kind emits zip)
    note: str = ""
    #: Pass unrecognised body keys through to the runner. Only the
    #: whistleblower kind sets this: its builder receives the *whole* document
    #: so the source-protection invariant can refuse a leaked identifier
    #: instead of this router silently dropping it.
    forward_unknown: bool = False

    def catalog_entry(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "cli_equivalent": self.cli_equivalent,
            "fields": [f.as_dict() for f in self.fields],
            "output": self.output,
            "note": self.note,
        }


# ---------- runners (each mirrors exactly what the CLI command calls) ----------


def _dump(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json")  # type: ignore[no-any-return]


def _run_foia(v: dict[str, Any]) -> dict[str, Any]:
    from novafabric.compliance.export.public._foia import build_foia_export

    return _dump(
        build_foia_export(
            decision_ref=str(v["decision_ref"]),
            record_index=v.get("record_index", []),
            redactions=v.get("redactions", []),
        )
    )


def _run_whistleblower(v: dict[str, Any]) -> dict[str, Any]:
    from novafabric.compliance.export.public._whistleblower import (
        build_whistleblower_attestation,
    )

    # The builder takes the whole document so its source-protection invariant
    # (I-5) sees every supplied field — never a pre-filtered subset.
    return _dump(build_whistleblower_attestation(v))


def _run_election(v: dict[str, Any]) -> dict[str, Any]:
    from novafabric.compliance.export.public._election import build_election_disclosure

    return _dump(
        build_election_disclosure(
            content_ref=str(v.get("content_ref") or ""),
            provenance_receipt_ref=str(v.get("provenance_receipt_ref") or ""),
            disclosure_label=str(v.get("disclosure_label") or ""),
            capsule_refs=v.get("capsule_refs", []),
        )
    )


def _run_transparency_register(v: dict[str, Any]) -> dict[str, Any]:
    from novafabric.compliance.export.public._transparency_register import (
        build_transparency_register,
    )

    return _dump(
        build_transparency_register(
            standard=str(v.get("standard") or "atrs"),
            capsule_root=str(v["capsule_root"]),
            operator_declared=v.get("operator_declared", {}),
            capsule_evidence=v.get("capsule_evidence", {}),
        )
    )


def _run_accessibility(v: dict[str, Any]) -> dict[str, Any]:
    from novafabric.compliance.export.public._accessibility import (
        build_accessibility_claim,
    )

    return _dump(
        build_accessibility_claim(
            declared_standard=str(v.get("declared_standard") or ""),
            audit_digest=v.get("audit_digest"),
            export_format_check=bool(v.get("export_format_check", False)),
        )
    )


def _run_citizen(v: dict[str, Any]) -> dict[str, Any]:
    from novafabric.compliance.export.public._citizen import build_citizen_explanation

    return _dump(
        build_citizen_explanation(
            decision_ref=v.get("decision_ref"),
            factors=v.get("factors", []),
            human_involvement=v.get("human_involvement"),
            contest_channel_ref=v.get("contest_channel_ref"),
            logic_summary_ref=v.get("logic_summary_ref"),
        )
    )


def _run_public_incident(v: dict[str, Any]) -> dict[str, Any]:
    from novafabric.compliance.export.public._public_incident import (
        build_public_incident_disclosure,
    )

    return _dump(
        build_public_incident_disclosure(
            incident_ref=str(v["incident_ref"]),
            public_summary=v.get("public_summary"),
            affected_scope=v.get("affected_scope"),
            remediation_ref=v.get("remediation_ref"),
        )
    )


def _run_public_annex_viii(v: dict[str, Any]) -> dict[str, Any]:
    from novafabric.compliance.export.public.annex_viii import build_annex_viii_entry

    return _dump(
        build_annex_viii_entry(
            capsule_root=str(v["capsule_root"]),
            operator_declared=v.get("operator_declared", {}),
            capsule_evidence=v.get("capsule_evidence", {}),
        )
    )


def _run_public_disclosure(v: dict[str, Any]) -> dict[str, Any]:
    from novafabric.compliance.export.public._public_sector import (
        build_public_sector_disclosure,
    )

    return _dump(
        build_public_sector_disclosure(
            authority_ref=v.get("authority_ref"),
            agent_ref=v.get("agent_ref"),
            decision_scope=v.get("decision_scope"),
            human_oversight_ref=v.get("human_oversight_ref"),
            capsule_refs=v.get("capsule_refs", []),
            system_card_ref=v.get("system_card_ref"),
        )
    )


def _run_control_attestation(v: dict[str, Any]) -> dict[str, Any]:
    from novafabric.compliance.export.control_attestation import (
        build_control_attestation,
    )

    return _dump(
        build_control_attestation(
            capsule_root=str(v["capsule_root"]),
            catalog=v.get("catalog", []),
            present_evidence=v.get("present_evidence", {}),
            declared=v.get("declared", []),
            catalog_ref=v.get("catalog_ref"),
        )
    )


def _run_rai_scorecard(v: dict[str, Any]) -> dict[str, Any]:
    from novafabric.compliance.rai.scorecard import build_rai_scorecard

    return _dump(
        build_rai_scorecard(
            v.get("evidence", {}),
            not_applicable=v.get("not_applicable", []),
            partial=v.get("partial", []),
        )
    )


def _run_part11(v: dict[str, Any]) -> dict[str, Any]:
    from novafabric.compliance.export.healthcare.part11 import build_part11_record

    return _dump(
        build_part11_record(
            capsule_root=str(v["capsule_root"]),
            elements=v.get("elements", {}),
            partial=v.get("partial"),
        )
    )


def _run_model_risk(v: dict[str, Any]) -> dict[str, Any]:
    from novafabric.compliance.export.finance.model_risk import build_model_risk_file

    return _dump(
        build_model_risk_file(
            model_id=str(v["model_id"]),
            development=v.get("development", []),
            independent_validation=v.get("independent_validation", []),
            ongoing_monitoring=v.get("ongoing_monitoring", []),
            model_inventory=v.get("model_inventory", []),
            partial=v.get("partial", []),
        )
    )


# ---------- the registry ----------


def _f(
    key: str, label: str, type_: str = "string", *, required: bool = False
) -> FieldSpec:
    return FieldSpec(key=key, label=label, type=type_, required=required)


def _spec(
    kind: str,
    label: str,
    cli: str,
    fields: list[FieldSpec],
    runner: Callable[[dict[str, Any]], dict[str, Any]],
    note: str,
    *,
    forward_unknown: bool = False,
) -> ExportKindSpec:
    return ExportKindSpec(
        kind=kind,
        label=label,
        cli_equivalent=cli,
        fields=fields,
        runner=runner,
        output="document",
        note=note,
        forward_unknown=forward_unknown,
    )


EXPORT_KINDS: dict[str, ExportKindSpec] = {
    s.kind: s
    for s in [
        _spec(
            "foia",
            "FOIA / public-records export (DRAFT)",
            "nova export-foia <document.json> --json",
            [
                _f("decision_ref", "Decision ref", required=True),
                _f("record_index", "Record index (ordered refs)", "json"),
                _f("redactions", "Redactions [{digest, exemption_ref}]", "json"),
            ],
            _run_foia,
            "DRAFT public-records export — NovaFabric never files it (ADR-0169 D1).",
        ),
        _spec(
            "whistleblower",
            "Whistleblower attestation (source-protecting)",
            "nova export-whistleblower <document.json> --json",
            [
                _f("content_digest", "Content digest", required=True),
                _f(
                    "authenticity_attestation",
                    "Authenticity attestation ref",
                    required=True,
                ),
                _f("anonymity_set_ref", "Anonymity-set ref"),
            ],
            _run_whistleblower,
            "Refuses any source-identifying field — authenticity without the "
            "source (ADR-0169 I-5).",
            forward_unknown=True,
        ),
        _spec(
            "election-disclosure",
            "Election content-provenance disclosure",
            "nova export-election-disclosure <document.json> --json",
            [
                _f("content_ref", "Content ref", required=True),
                _f("provenance_receipt_ref", "Provenance receipt ref", required=True),
                _f(
                    "disclosure_label",
                    "Label (ai_generated | ai_assisted | synthetic_media)",
                    required=True,
                ),
                _f("capsule_refs", "Capsule refs", "json"),
            ],
            _run_election,
            "Records provenance — adjudicates nothing (ADR-0169 D5).",
        ),
        _spec(
            "transparency-register",
            "Algorithm-register record (DRAFT)",
            "nova export-transparency-register <document.json> "
            "--standard <atrs|amsterdam|helsinki> --json",
            [
                _f("capsule_root", "Capsule root digest", required=True),
                _f("standard", "Register standard (atrs | amsterdam | helsinki)"),
                _f("capsule_evidence", "Capsule evidence {field: ref}", "json"),
                _f("operator_declared", "Operator declared {field: value}", "json"),
            ],
            _run_transparency_register,
            "DRAFT register record — the operator submits it; unmapped fields "
            "are listed, never fabricated (ADR-0169 D1).",
        ),
        _spec(
            "accessibility-claim",
            "Accessibility conformance claim (declared)",
            "nova export-accessibility-claim <document.json> --json",
            [
                _f(
                    "declared_standard",
                    "Declared standard (wcag_2_2_aa | en_301_549_v4_1_1)",
                    required=True,
                ),
                _f("audit_digest", "Declared-audit digest"),
                _f("export_format_check", "Export format check", "boolean"),
            ],
            _run_accessibility,
            "A declared claim — NovaFabric performs no accessibility audit "
            "(ADR-0169 D5).",
        ),
        _spec(
            "citizen-explanation",
            "Citizen decision explanation",
            "nova export-citizen-explanation <document.json> --json",
            [
                _f("decision_ref", "Decision ref", required=True),
                _f(
                    "human_involvement",
                    "Human involvement (solely_automated | human_in_the_loop | human_reviewed)",
                    required=True,
                ),
                _f("factors", "Recorded factors", "json"),
                _f("contest_channel_ref", "Contest channel ref"),
                _f("logic_summary_ref", "Logic summary ref"),
            ],
            _run_citizen,
            "Meaningful information, never a legal-sufficiency claim; refuses "
            "model internals and raw identifiers (ADR-0169 D1).",
        ),
        _spec(
            "public-incident",
            "Public-interest incident summary (DRAFT)",
            "nova export-public-incident <document.json> --json",
            [
                _f("incident_ref", "Incident ref", required=True),
                _f("public_summary", "Public summary (aggregate)"),
                _f("affected_scope", "Affected scope (aggregate)"),
                _f("remediation_ref", "Remediation ref"),
            ],
            _run_public_incident,
            "Always DRAFT, never transmitted; refuses per-subject identifiers "
            "(ADR-0169 D1).",
        ),
        _spec(
            "public-annex-viii",
            "EU AI Act Annex VIII public-DB entry (DRAFT)",
            "nova export-public-annex-viii <document.json> --json",
            [
                _f("capsule_root", "Capsule root digest", required=True),
                _f("capsule_evidence", "Capsule evidence {field: ref}", "json"),
                _f("operator_declared", "Operator declared {field: value}", "json"),
            ],
            _run_public_annex_viii,
            "DRAFT Annex VIII / Art. 71 entry — unmapped required fields are "
            "listed, never fabricated (ADR-0169 D1).",
        ),
        _spec(
            "public-disclosure",
            "Public-sector disclosure record (DRAFT)",
            "nova export-public-disclosure <document.json> --json",
            [
                _f("authority_ref", "Authority ref (declared)"),
                _f("agent_ref", "Agent ref"),
                _f("decision_scope", "Decision scope"),
                _f("human_oversight_ref", "Human-oversight ref"),
                _f("capsule_refs", "Capsule refs", "json"),
                _f("system_card_ref", "System-card ref (by digest)"),
            ],
            _run_public_disclosure,
            "References, never re-authors; missing required fields are listed "
            "for manual completion (ADR-0169 D1).",
        ),
        _spec(
            "control-attestation",
            "Governance-control attestation pack",
            "nova export-control-attestation <document.json> --json",
            [
                _f("capsule_root", "Capsule root digest", required=True),
                _f("catalog", "Control catalog [{control_id, evidence_kind?}]", "json"),
                _f("present_evidence", "Present evidence {kind: ref}", "json"),
                _f("declared", "Operator-declared control ids", "json"),
                _f("catalog_ref", "Catalog ref"),
            ],
            _run_control_attestation,
            "Presents governance evidence — never certifies a control "
            "(ADR-0170 D5).",
        ),
        _spec(
            "rai-scorecard",
            "Responsible-AI coverage scorecard",
            "nova export-rai-scorecard <document.json> --json",
            [
                _f("evidence", "Evidence {dimension: [refs]}", "json"),
                _f("not_applicable", "Not-applicable dimensions", "json"),
                _f("partial", "Partial dimensions", "json"),
            ],
            _run_rai_scorecard,
            "Coverage, never a score — no threshold, no pass/fail "
            "(ADR-0158 I-4).",
        ),
        _spec(
            "part11",
            "21 CFR Part 11 electronic-records artifact",
            "nova export-part11 <document.json> --json",
            [
                _f("capsule_root", "Capsule root digest", required=True),
                _f("elements", "Elements {name: source_ref}", "json"),
                _f("partial", "Partial {name: reason}", "json"),
            ],
            _run_part11,
            "Renders facts, never a Part 11 conformity determination "
            "(ADR-0160).",
        ),
        _spec(
            "model-risk",
            "SR 26-2 / SR 11-7 model-risk evidence pack",
            "nova export-model-risk <evidence.json> --json",
            [
                _f("model_id", "Model id", required=True),
                _f("development", "Development refs", "json"),
                _f("independent_validation", "Independent-validation refs", "json"),
                _f("ongoing_monitoring", "Ongoing-monitoring refs", "json"),
                _f("model_inventory", "Model-inventory refs", "json"),
                _f("partial", "Partial pillars", "json"),
            ],
            _run_model_risk,
            "Assembles, never assesses — there is no rating (ADR-0159 D2).",
        ),
    ]
}


# ---------- validation ----------


def _validate_fields(spec: ExportKindSpec, body: dict[str, Any]) -> dict[str, Any]:
    """Validate the JSON body against ``spec.fields``; raise HTTP 422 on failure."""
    values: dict[str, Any] = {}
    missing: list[str] = []
    for f_spec in spec.fields:
        raw = body.get(f_spec.key)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if f_spec.required:
                missing.append(f_spec.key)
            continue
        if f_spec.type == "string":
            if not isinstance(raw, str | int | float):
                raise HTTPException(
                    status_code=422,
                    detail=f"field '{f_spec.key}' must be a string",
                )
            values[f_spec.key] = str(raw)
        elif f_spec.type == "boolean":
            values[f_spec.key] = bool(raw)
        else:  # "json" — a list or object; a JSON-encoded string is accepted
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise HTTPException(
                        status_code=422,
                        detail=f"field '{f_spec.key}' is not valid JSON: {exc}",
                    ) from exc
            if not isinstance(raw, list | dict):
                raise HTTPException(
                    status_code=422,
                    detail=f"field '{f_spec.key}' must be a JSON array or object",
                )
            values[f_spec.key] = raw
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"missing required field(s): {', '.join(missing)}",
        )
    if spec.forward_unknown:
        known = {f_spec.key for f_spec in spec.fields}
        for key, val in body.items():
            if key != "run_id" and key not in known:
                values[key] = val
    return values


# ---------- the router factory ----------


def build_compliance_exports_router(
    verify_token: Callable[..., Any],
    *,
    capsule_dir: Path,
    audit_log: Callable[..., Any],
) -> APIRouter:
    """Build the generic compliance-export registry router.

    ``verify_token`` is the auth dependency guarding every route (it returns
    the actor token fingerprint used for audit records); ``capsule_dir``
    anchors optional ``run_id`` validation; ``audit_log`` is the append
    callable (``novafabric.serve.audit.append`` signature) — injected so this
    module never imports app internals.
    """
    router = APIRouter(
        dependencies=[Depends(verify_token)], tags=["compliance-exports"]
    )

    def _resolve_run_id(run_id: str) -> None:
        # Mirrors serve.app._resolve_capsule semantics (400 on traversal,
        # 404 when unknown) without the circular import.
        if "/" in run_id or ".." in run_id:
            raise HTTPException(status_code=400, detail="invalid run_id")
        candidate = capsule_dir / run_id
        if candidate.is_dir() and (candidate / "capsule.yaml").exists():
            return
        for d in discover_capsule_dirs(capsule_dir):
            try:
                m = load_capsule_manifest(d)
            except FileNotFoundError:
                continue
            if m.get("run_id") == run_id:
                return
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")

    @router.get("/api/compliance/export/kinds")
    async def export_kinds() -> dict[str, Any]:
        """The registry catalog the dynamic panel renders (ADR-0200 §2)."""
        return {
            "kinds": [spec.catalog_entry() for spec in EXPORT_KINDS.values()],
            "count": len(EXPORT_KINDS),
        }

    @router.post("/api/compliance/export/{kind}")
    async def run_export(
        kind: str,
        body: dict[str, Any] = Body(...),
        actor_fp: str = Depends(verify_token),
    ) -> dict[str, Any]:
        """Run one registry exporter — the same builder its CLI command calls."""
        spec = EXPORT_KINDS.get(kind)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"unknown export kind: {kind}")

        run_id = str(body.get("run_id") or "").strip() or None
        if run_id is not None:
            _resolve_run_id(run_id)

        values = _validate_fields(spec, body)

        def _audit(result: str, error: str | None = None) -> None:
            audit_log(
                action=f"export_{kind.replace('-', '_')}",
                args={
                    "kind": kind,
                    "run_id": run_id,
                    # Field keys only — disclosure payloads never enter the
                    # audit log (ADR-0009).
                    "fields": sorted(values),
                },
                cli_equivalent=spec.cli_equivalent,
                actor_token_fp=actor_fp,
                result=result,
                error=error,
            )

        try:
            document = spec.runner(values)
        except ImportError as exc:
            _audit("error", error=str(exc))
            raise HTTPException(
                status_code=501,
                detail=(
                    f"exporter module for '{kind}' not available: {exc} — "
                    "install the matching optional extra "
                    "(pip install 'novafabric[compliance]')"
                ),
            ) from exc
        except (ValueError, TypeError, KeyError) as exc:
            _audit("error", error=str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        _audit("ok")
        return {
            "ok": True,
            "kind": kind,
            "run_id": run_id,
            "document": document,
            "cli_equivalent": spec.cli_equivalent,
            "note": spec.note,
        }

    return router
