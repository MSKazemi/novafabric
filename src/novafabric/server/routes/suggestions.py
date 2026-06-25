"""Suggestions resource — /v0/runs/suggest-register.

Returns asset registration suggestions aggregated across recent capsules.
Used by the dashboard Registry empty-state panel.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from novafabric.server.auth import AuthContext
from novafabric.server.deps import get_capsule_dir, get_db_path
from novafabric.server.rbac import Role, require_role

router = APIRouter(prefix="/runs", tags=["suggestions"])


@router.get("/suggest-register", response_model=None)
async def get_registration_suggestions(
    capsule_limit: int = Query(default=10, ge=1, le=50),
    capsule_dir: Annotated[Path, Depends(get_capsule_dir)] = None,  # type: ignore[assignment]
    db_path: Annotated[Path | None, Depends(get_db_path)] = None,
    _auth: Annotated[AuthContext, Depends(require_role(Role.reader))] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Aggregate asset registration suggestions across the most recent capsules.

    Skips assets already present in the registry.
    """
    from novafabric.registry.suggestion_engine import SuggestionEngine

    engine = SuggestionEngine()
    suggestions = engine.analyze_recent(
        capsule_dir, db_path=db_path, limit=capsule_limit
    )
    return {
        "total_capsules_analyzed": capsule_limit,
        "suggestions": [
            {
                "asset_type": s.asset_type,
                "detected_name": s.detected_name,
                "detected_version": s.detected_version,
                "confidence": s.confidence,
                "call_count": s.call_count,
                "draft_spec_yaml": s.draft_spec_yaml,
                "warnings": s.warnings,
            }
            for s in suggestions
        ],
    }
