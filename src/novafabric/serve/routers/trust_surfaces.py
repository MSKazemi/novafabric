"""Trust-surface route group — radar + redaction X-Ray (ADR-0173 / ADR-0174).

Exposes the two per-capsule trust projections that were previously CLI-only,
so a dashboard glyph — or any other consumer — has a data source.

Built by a factory so the caller injects its own auth dependency
(ADR-0183 §3): ``serve`` passes its shared-token ``verify_token`` closure;
``server`` can mount the same routes behind OIDC/RBAC. Landing as a router
rather than inline is the ADR-0183 strangler discipline — a frozen inline
count in ``serve/app.py`` enforces it.

**Same projection as the CLI.** These call the identical builders
``nova trust-radar`` and ``nova redaction-xray`` use. Two code paths
reporting a capsule's trust posture could disagree, and in the subsystem
whose entire job is stating what is *proven*, a disagreement is worse than
exposing nothing at all.

Read-only and derived: both compute from capsule bytes already on disk and
hold no state of their own.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends


def build_trust_surfaces_router(
    verify_token: Callable[..., Any],
    *,
    capsule_dir: Path,
    resolve_capsule: Callable[[str, Path], Path],
) -> APIRouter:
    """Router for ``/api/runs/{run_id}/trust-radar`` and ``…/redaction-xray``.

    *resolve_capsule* is injected rather than imported so this module does not
    depend on ``serve.app`` — which imports this one.
    """
    router = APIRouter(dependencies=[Depends(verify_token)])

    @router.get("/api/runs/{run_id}/trust-radar")
    async def get_trust_radar(run_id: str) -> dict[str, Any]:
        """Trust guarantees for one capsule (ADR-0173).

        A guarantee the capsule cannot evidence is reported ``n/a``, never
        ``fail``: an unsealed capsule is *unverified*, not *failed*, and a
        missing NovaSeal profile means verification could not run rather than
        that it ran and failed.
        """
        from novafabric.trust.capsule_flags import flags_from_capsule
        from novafabric.trust.radar import build_trust_radar

        cdir = resolve_capsule(run_id, capsule_dir)
        radar = build_trust_radar(flags_from_capsule(cdir), capsule_id=run_id)
        return radar.model_dump(mode="json")

    @router.get("/api/runs/{run_id}/redaction-xray")
    async def get_redaction_xray(run_id: str) -> dict[str, Any]:
        """Field-protection overlay for one capsule (ADR-0174).

        Paths and states only — a field **value** is never returned
        (ADR-0009). A capsule captured without the masking pipeline yields an
        empty report rather than a 404: "nothing was scanned" is a real
        answer, not a missing resource.
        """
        from novafabric.masking.xray import (
            build_field_xray,
            field_states_from_findings,
        )

        cdir = resolve_capsule(run_id, capsule_dir)
        proof_path = cdir / "redaction-proof.json"
        findings: list[Any] = []
        if proof_path.is_file():
            try:
                proof = json.loads(proof_path.read_text())
                if isinstance(proof, dict):
                    findings = proof.get("findings") or []
            except (OSError, ValueError):
                # An unreadable proof is reported as "nothing scanned" rather
                # than a 500: a malformed sidecar must not take the capsule
                # detail view down.
                findings = []
        report = build_field_xray(
            field_states_from_findings(findings), capsule_id=run_id
        )
        return report.model_dump(mode="json")

    return router
