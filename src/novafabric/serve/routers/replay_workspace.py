"""Replay workspace read surface: what a replay of this run would do (issue #269).

``GET /api/runs/{run_id}/replayability`` returns, for one run, the two answers
the dashboard's Replay workspace needs before anyone replays anything:

- ``current`` -- what a **strict** ``mocked`` replay would do with the capsule,
  classified now by this server's ``novafabric`` with
  :func:`novafabric.replay._replayability_summary.assess_capsule`: the same
  predicates ``nova replay`` and ``nova replay --dry-run`` use, so the two cannot
  disagree. Nothing is executed and nothing is written.
- ``recorded`` -- the capsule's own ``replayability.json``, the prediction the
  capturing version wrote (absent for capsules written before it existed). Kept
  apart from ``current`` because ``nova replay`` never reads it.

``dashboard_modes`` says which replay modes the dashboard itself can start. It
lists only the modes whose ``POST /api/runs/{run_id}/replay/...`` route exists,
and none of those runs the captured command: ``mocked`` and ``intervention``
re-execute the workload, so the dashboard never starts them and the workspace
shows their command to copy instead. A test pins this against the mounted routes.

Read-only and fail-open per answer: an error classifying the capsule, or a
malformed ``replayability.json``, is reported in ``current_error`` /
``recorded_error`` rather than failing the request.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from novafabric.serve.capsule_loader import load_capsule_manifest

#: Replay modes the dashboard can start, by the name the workspace uses. ``True``
#: only for modes with a ``POST /api/runs/{run_id}/replay/<route>`` endpoint, none
#: of which re-runs the captured command. ``mocked`` and ``intervention`` execute
#: the workload: never started from the dashboard (copy the CLI command instead).
DASHBOARD_STARTABLE_MODES: Final[Mapping[str, bool]] = {
    "forensic": True,
    "dry_run": True,
    "semantic": True,
    "exact": True,
    "mocked": False,
    "intervention": False,
}

#: The ``POST`` route suffix each startable mode uses.
MODE_ROUTE: Final[Mapping[str, str]] = {
    "forensic": "forensic",
    "dry_run": "dry-run",
    "semantic": "semantic",
    "exact": "exact",
}

#: Longest error text returned; the message is redacted before it is cut.
_MAX_ERROR_CHARS: Final[int] = 300


class ReplayabilityResponse(BaseModel):
    """Body of ``GET /api/runs/{run_id}/replayability``."""

    run_id: str
    #: Strict-mocked prediction computed now (``novafabric.replayability/1``).
    current: dict[str, Any] | None = None
    current_error: str | None = None
    #: The capsule's ``replayability.json`` as written at capture.
    recorded: dict[str, Any] | None = None
    recorded_state: Literal["present", "absent", "invalid"]
    recorded_error: str | None = None
    dashboard_modes: dict[str, bool]


def _error_text(exc: BaseException) -> str:
    from novafabric.capture.secrets import redact_secrets_in_text  # noqa: PLC0415

    return redact_secrets_in_text(f"{type(exc).__name__}: {exc}")[:_MAX_ERROR_CHARS]


def _current(cdir: Path) -> tuple[dict[str, Any] | None, str | None]:
    from novafabric.replay._replayability_summary import assess_capsule  # noqa: PLC0415

    try:
        manifest = load_capsule_manifest(cdir)
        if not isinstance(manifest, dict):
            return None, "capsule.yaml is not a mapping"
        summary = assess_capsule(cdir, manifest, operation="nova serve replayability")
    except Exception as exc:  # noqa: BLE001 -- reported to the user, never a 500
        return None, _error_text(exc)
    return summary.as_dict(), None


RecordedState = Literal["present", "absent", "invalid"]


def _recorded(cdir: Path) -> tuple[dict[str, Any] | None, RecordedState, str | None]:
    from novafabric.replay._replayability_summary import read_replayability  # noqa: PLC0415

    try:
        doc = read_replayability(cdir)
    except Exception as exc:  # noqa: BLE001 -- malformed or unusable member
        return None, "invalid", _error_text(exc)
    if doc is None:
        return None, "absent", None
    return doc, "present", None


def replayability_view(run_id: str, cdir: Path) -> dict[str, Any]:
    """The response body for *run_id*, whose capsule is *cdir* (pure read)."""
    current, current_error = _current(cdir)
    recorded, recorded_state, recorded_error = _recorded(cdir)
    return ReplayabilityResponse(
        run_id=run_id,
        current=current,
        current_error=current_error,
        recorded=recorded,
        recorded_state=recorded_state,
        recorded_error=recorded_error,
        dashboard_modes=dict(DASHBOARD_STARTABLE_MODES),
    ).model_dump(mode="json")


def build_replay_workspace_router(
    verify_token: Callable[..., Any],
    *,
    capsule_dir: Path,
    resolve_capsule: Callable[[str, Path], Path],
) -> APIRouter:
    """Router for ``GET /api/runs/{run_id}/replayability``.

    *resolve_capsule* is injected so this module does not import ``serve.app``
    (which imports this one) -- the ``forensics`` / ``trust_surfaces`` pattern.
    """
    router = APIRouter(dependencies=[Depends(verify_token)], tags=["replay"])

    @router.get(
        "/api/runs/{run_id}/replayability",
        operation_id="dashboardGetReplayability",
        responses={200: {"model": ReplayabilityResponse}},
        response_model=None,
    )
    async def get_replayability(run_id: str) -> dict[str, Any]:
        cdir = resolve_capsule(run_id, capsule_dir)
        return replayability_view(run_id, cdir)

    return router
