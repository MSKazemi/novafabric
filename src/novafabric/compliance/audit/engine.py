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
"""Compliance audit engine: maps capsule evidence to regulatory controls."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from ulid import ULID

from novafabric._capsule_fs import read_capsule_member_or_none
from novafabric.compliance.capture_coverage import evidence_is_uncaptured, not_captured_note

from .models import AuditReport, Control, ControlCoverage, ControlProfile, EvidenceRequirement

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Evidence-type → file presence check mapping
# ---------------------------------------------------------------------------

# Each evidence type maps to a callable that takes the capsule dir and returns
# True if evidence of that type is present.
_EvidenceChecker = Callable[[Path], bool]


def _check_model_calls(capsule_dir: Path) -> bool:
    p = capsule_dir / "model-calls.jsonl"
    return p.exists() and p.stat().st_size > 0


def _check_tool_permissions(capsule_dir: Path) -> bool:
    p = capsule_dir / "tool-permission-events.jsonl"
    return p.exists() and p.stat().st_size > 0


def _check_pii_scan(capsule_dir: Path) -> bool:
    """The ``--pii-subject`` stage's ``redaction_manifest.json``, parsed (compliance-privacy-04).

    Every capture writes ``redaction-proof.json`` — the *secret* scanner's proof — so
    accepting it scored GDPR Art.17 erasure "covered" for runs no PII scan ever saw.
    Neither is any file under ``compliance/pii/`` (nothing writes one there) evidence.
    """
    from novafabric.compliance.pii.manifest import RedactionManifest  # noqa: PLC0415

    text = read_capsule_member_or_none(
        capsule_dir / "redaction_manifest.json", operation="compliance export"
    )
    if text is None:
        return False
    try:
        RedactionManifest.model_validate_json(text)
    except ValueError:
        return False
    return True


def _check_policy_events(capsule_dir: Path) -> bool:
    """Policy events: tool-permission-events.jsonl with at least one deny decision."""
    p = capsule_dir / "tool-permission-events.jsonl"
    if not p.exists() or p.stat().st_size == 0:
        return False
    # Check whether any line contains a deny decision (loose check)
    try:
        text = (read_capsule_member_or_none(p, operation="compliance export") or "")
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if record.get("decision") in ("deny", "denied", "block", "blocked"):
                    return True
                if record.get("event_type") in ("policy_decision", "policy_event"):
                    return True
            except json.JSONDecodeError:
                continue
    except OSError:
        return False
    # Fall back: file exists and is non-empty → no policy evidence found
    return False


def _check_seal_signature(capsule_dir: Path) -> bool:
    """A NovaSeal seal that verifies — never the existence of a file (review cli-a-03)."""
    from novafabric.trust.novaseal.capsule_verify import capsule_seal_verifies  # noqa: PLC0415

    return capsule_seal_verifies(capsule_dir)


def _check_lineage(capsule_dir: Path) -> bool:
    p = capsule_dir / "lineage.jsonl"
    return p.exists() and p.stat().st_size > 0


_EVIDENCE_CHECKERS: dict[str, _EvidenceChecker] = {
    "model_calls": _check_model_calls,
    "tool_permissions": _check_tool_permissions,
    "pii_scan": _check_pii_scan,
    "policy_events": _check_policy_events,
    "seal_signature": _check_seal_signature,
    "lineage": _check_lineage,
}


def _has_evidence(capsule_dir: Path, evidence_type: str) -> bool:
    """Return True if *capsule_dir* contains evidence of *evidence_type*."""
    checker = _EVIDENCE_CHECKERS.get(evidence_type)
    if checker is None:
        log.warning("Unknown evidence type %r — treating as absent", evidence_type)
        return False
    try:
        return checker(capsule_dir)
    except Exception:
        log.debug(
            "Evidence check failed for %r in %s", evidence_type, capsule_dir, exc_info=True
        )
        return False


# ---------------------------------------------------------------------------
# AuditEngine
# ---------------------------------------------------------------------------


class AuditEngine:
    """Scans capsules in *data_dir* and maps evidence to controls in *profile*.

    Args:
        data_dir: Root data directory (contains a ``capsules/`` sub-directory).
        profile: :class:`ControlProfile` loaded from a YAML file.
        capsules_dir: Where the capsules are, when not ``<data_dir>/capsules``
            (e.g. ``$NOVAFABRIC_CAPSULE_DIR``). Optional; additive.
        capsule_paths: Scan exactly these capsule directories instead of every
            sub-directory of *capsules_dir* — a single-capsule audit. Optional;
            additive.
    """

    def __init__(
        self,
        data_dir: Path,
        profile: ControlProfile,
        capsules_dir: Path | None = None,
        *,
        capsule_paths: Sequence[Path] | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._profile = profile
        self._capsules_dir = capsules_dir if capsules_dir is not None else data_dir / "capsules"
        self._capsule_paths = list(capsule_paths) if capsule_paths is not None else None

    @property
    def capsules_dir(self) -> Path:
        """The directory whose sub-directories are scanned as capsules."""
        return self._capsules_dir

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(self) -> AuditReport:
        """Scan capsules and produce a full :class:`AuditReport`."""
        capsule_dirs = self._find_capsules()
        coverages: list[ControlCoverage] = []

        for control in self._profile.controls:
            cov = self._score_control(control, capsule_dirs)
            coverages.append(cov)

        # Aggregate counters
        total = len(coverages)
        covered = sum(1 for c in coverages if c.status == "covered")
        partial = sum(1 for c in coverages if c.status == "partial")
        missing = sum(1 for c in coverages if c.status == "missing")
        not_captured = sum(1 for c in coverages if c.status == "not_applicable")

        # Weighted overall score — controls not captured by this version are
        # excluded rather than counted as zero (L18).
        scored = [
            (cov, ctrl)
            for cov, ctrl in zip(coverages, self._profile.controls, strict=True)
            if cov.status != "not_applicable"
        ]
        total_weight = sum(ctrl.weight for _, ctrl in scored)
        if total_weight > 0:
            weighted_score = sum(cov.coverage_score * ctrl.weight for cov, ctrl in scored)
            overall_score = weighted_score / total_weight
        else:
            overall_score = 0.0

        seal_verified = any(
            _has_evidence(cd, "seal_signature") for cd in capsule_dirs
        )

        return AuditReport(
            report_id=str(ULID()),
            generated_at=datetime.now(timezone.utc),
            profile_id=self._profile.id,
            profile_name=self._profile.name,
            framework=self._profile.framework,
            data_dir=str(self._data_dir.resolve()),
            capsule_count=len(capsule_dirs),
            total_controls=total,
            covered_controls=covered,
            partial_controls=partial,
            missing_controls=missing,
            not_captured_controls=not_captured,
            overall_score=round(overall_score, 4),
            coverages=coverages,
            evidence_seal_verified=seal_verified,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _find_capsules(self) -> list[Path]:
        """Return sorted list of capsule directories under :attr:`capsules_dir`."""
        if self._capsule_paths is not None:
            # A scan never lists a linked entry, named or not.
            return sorted(p for p in self._capsule_paths if p.is_dir() and not p.is_symlink())
        capsules_root = self._capsules_dir
        if not capsules_root.is_dir():
            return []
        dirs = sorted(
            p for p in capsules_root.iterdir()
            if p.is_dir() and not p.is_symlink()  # a scan never lists a linked entry
        )
        return dirs

    def _check_evidence(self, capsule_dir: Path, req: EvidenceRequirement) -> bool:
        """Check whether *capsule_dir* satisfies *req*."""
        return _has_evidence(capsule_dir, req.evidence_type)

    def _score_control(
        self, control: Control, capsule_dirs: list[Path]
    ) -> ControlCoverage:
        """Compute :class:`ControlCoverage` for *control* across all capsules."""
        if not capsule_dirs:
            return ControlCoverage(
                control_id=control.id,
                control_title=control.title,
                status="missing",
                coverage_score=0.0,
                evidence_found=[],
                evidence_missing=[
                    req.description
                    for req in control.evidence_requirements
                    if req.required
                ],
                capsule_ids=[],
            )

        # For each requirement, check whether *any* capsule satisfies it.
        found_descs: list[str] = []
        missing_descs: list[str] = []
        contributing_capsule_ids: list[str] = []

        # L18: evidence this version never captures, absent from every scanned
        # capsule, is reported as not captured — never scored as a failure.
        uncaptured = [
            r for r in control.evidence_requirements
            if evidence_is_uncaptured(r.evidence_type, capsule_dirs)
        ]
        not_captured_descs = [not_captured_note(r.description) for r in uncaptured]
        uncaptured_required = any(r.required for r in uncaptured)
        scored_reqs = [r for r in control.evidence_requirements if r not in uncaptured]
        if uncaptured and not scored_reqs:
            return ControlCoverage(
                control_id=control.id,
                control_title=control.title,
                status="not_applicable",
                coverage_score=0.0,
                evidence_not_captured=not_captured_descs,
            )

        required_reqs = [r for r in scored_reqs if r.required]
        optional_reqs = [r for r in scored_reqs if not r.required]

        required_satisfied = 0
        optional_satisfied = 0

        for req in required_reqs:
            satisfied_by: list[str] = []
            for cd in capsule_dirs:
                if self._check_evidence(cd, req):
                    satisfied_by.append(cd.name)
            if satisfied_by:
                required_satisfied += 1
                found_descs.append(req.description)
                for cid in satisfied_by:
                    if cid not in contributing_capsule_ids:
                        contributing_capsule_ids.append(cid)
            else:
                missing_descs.append(req.description)

        for req in optional_reqs:
            satisfied_by = []
            for cd in capsule_dirs:
                if self._check_evidence(cd, req):
                    satisfied_by.append(cd.name)
            if satisfied_by:
                optional_satisfied += 1
                found_descs.append(req.description)
                for cid in satisfied_by:
                    if cid not in contributing_capsule_ids:
                        contributing_capsule_ids.append(cid)

        total_required = len(required_reqs)
        total_optional = len(optional_reqs)
        total_reqs = total_required + total_optional

        # Score: required carry full weight; optional carry half weight
        if total_reqs == 0:
            # No requirements at all — trivially covered
            coverage_score = 1.0
            status: str = "covered"
        else:
            required_score = (required_satisfied / total_required) if total_required > 0 else 1.0
            optional_score = (optional_satisfied / total_optional) if total_optional > 0 else 1.0

            # Blend: required requirements dominate (75% weight)
            if total_required > 0 and total_optional > 0:
                coverage_score = 0.75 * required_score + 0.25 * optional_score
            elif total_required > 0:
                coverage_score = required_score
            else:
                coverage_score = optional_score

            # Determine status from required satisfaction
            if required_satisfied == total_required and (
                optional_satisfied == total_optional or total_optional == 0
            ):
                status = "covered"
            elif required_satisfied == total_required:
                # All required met but some optional missing → covered
                status = "covered"
            elif required_satisfied > 0:
                status = "partial"
            else:
                status = "missing"

        if status == "covered" and uncaptured_required:
            # What this version captures is all present, but a required part of
            # the control is not captured at all: never claim full coverage.
            status = "partial"

        return ControlCoverage(
            control_id=control.id,
            control_title=control.title,
            status=status,  # type: ignore[arg-type]
            coverage_score=round(coverage_score, 4),
            evidence_found=found_descs,
            evidence_missing=missing_descs,
            evidence_not_captured=not_captured_descs,
            capsule_ids=contributing_capsule_ids,
        )
