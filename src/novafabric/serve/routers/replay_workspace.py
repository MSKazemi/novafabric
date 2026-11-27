"""Replay workspace read surface: what a replay of this run would do (issue #269).

``GET /api/runs/{run_id}/replayability`` returns, for one run, the two answers
the dashboard's Replay workspace needs before anyone replays anything:

- ``current`` -- what a **strict** ``mocked`` replay would do with the capsule,
  classified now by this server's ``novafabric`` with
  :func:`novafabric.replay._replayability_summary.assess` over the same members
  ``assess_capsule`` reads: the same predicates ``nova replay`` and
  ``nova replay --dry-run`` use, so the two cannot disagree. Nothing is executed
  and nothing is written.
- ``recorded`` -- the capsule's own ``replayability.json``, the prediction the
  capturing version wrote (absent for capsules written before it existed). Kept
  apart from ``current`` because ``nova replay`` never reads it.

``dashboard_modes`` says which replay modes the dashboard itself can start. It
lists only the modes whose ``POST /api/runs/{run_id}/replay/...`` route exists,
and none of those runs the captured command: ``mocked`` and ``intervention``
re-execute the workload, so the dashboard never starts them and the workspace
shows their command to copy instead. A test pins this against the mounted routes.
``mode_access`` adds, per startable mode, the scope its ``POST`` route requires
(read from :data:`novafabric.serve.authz.ROUTE_SCOPES`) and whether the calling
credential holds it, so the dashboard can say why it cannot start one. It is a
hint: the ``POST`` route enforces the scope itself.

Read-only and fail-open per answer: an error classifying the capsule, or a
malformed ``replayability.json``, is reported in ``current_error`` /
``recorded_error`` rather than failing the request. Every member read is bounded
by :data:`MAX_MEMBER_BYTES`. A call stream above it is **not classified** (a
prefix of the stream would give wrong counts): ``current`` is ``None`` and
``current_over_limit`` names the member, its size and the bound, so the user can
run ``nova replay <capsule> --dry-run`` instead. The handler is a plain ``def``:
FastAPI runs it in its threadpool, so the file reads never block the event loop.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final, Literal

from fastapi import APIRouter, Depends, Request
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

#: Bound on each capsule member this route reads (the call streams, ``replay.yaml``,
#: ``replayability.json``) -- the bound the evidence detail route puts on a member.
MAX_MEMBER_BYTES: Final[int] = 16 * 1024 * 1024

_MODEL_CALLS: Final[str] = "model-calls.jsonl"
_TOOL_CALLS: Final[str] = "tool-calls.jsonl"
_REPLAY_POLICY: Final[str] = "replay.yaml"
_OPERATION: Final[str] = "nova serve replayability"


class OverLimit(BaseModel):
    """A member too large for the dashboard to classify; nothing was classified."""

    member: str
    size_bytes: int
    max_bytes: int


class _OverLimitError(Exception):
    def __init__(self, info: OverLimit) -> None:
        super().__init__(info.member)
        self.info = info


class ModeAccess(BaseModel):
    """Whether the calling credential may start one mode from the dashboard."""

    required_scope: str | None
    held_scope: str | None
    permitted: bool


class ReplayabilityResponse(BaseModel):
    """Body of ``GET /api/runs/{run_id}/replayability``."""

    run_id: str
    #: Strict-mocked prediction computed now (``novafabric.replayability/1``).
    current: dict[str, Any] | None = None
    current_error: str | None = None
    #: Set when a member is over :data:`MAX_MEMBER_BYTES` (``current`` is then ``None``).
    current_over_limit: OverLimit | None = None
    #: The capsule's ``replayability.json`` as written at capture.
    recorded: dict[str, Any] | None = None
    recorded_state: Literal["present", "absent", "invalid"]
    recorded_error: str | None = None
    dashboard_modes: dict[str, bool]
    #: Per startable mode: the scope its route requires and whether the caller holds it.
    mode_access: dict[str, ModeAccess] = {}


def _error_text(exc: BaseException) -> str:
    from novafabric.capture.secrets import redact_secrets_in_text  # noqa: PLC0415

    return redact_secrets_in_text(f"{type(exc).__name__}: {exc}")[:_MAX_ERROR_CHARS]


def _read_member(cdir: Path, name: str) -> str | None:
    """*name* as text, ``None`` when absent; bounded, never following a link.

    A regular file above the bound raises :class:`_OverLimitError` before it is
    opened; the bounded read then refuses a file that grew past it meanwhile, and
    an unusable member (a link, FIFO, directory) raises as ``nova replay`` does.
    """
    from novafabric._capsule_fs import read_required_capsule_member  # noqa: PLC0415

    try:
        st = os.lstat(cdir / name)
    except FileNotFoundError:
        return None
    if stat.S_ISREG(st.st_mode) and st.st_size > MAX_MEMBER_BYTES:
        raise _OverLimitError(
            OverLimit(member=name, size_bytes=st.st_size, max_bytes=MAX_MEMBER_BYTES)
        )
    return read_required_capsule_member(
        cdir / name, operation=_OPERATION, max_bytes=MAX_MEMBER_BYTES
    )


def _jsonl(text: str | None) -> list[Any]:
    """Records of a JSONL stream as the replay engine reads them (bad lines skipped)."""
    records: list[Any] = []
    for line in (text or "").splitlines():
        if line.strip():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _current(cdir: Path) -> tuple[dict[str, Any] | None, str | None, OverLimit | None]:
    """:func:`assess` over the capsule exactly as ``assess_capsule`` runs it, bounded."""
    import yaml  # noqa: PLC0415

    from novafabric.replay._replayability_summary import assess  # noqa: PLC0415

    try:
        manifest = load_capsule_manifest(cdir)
        if not isinstance(manifest, dict):
            return None, "capsule.yaml is not a mapping", None
        policy_text = _read_member(cdir, _REPLAY_POLICY)
        policy = yaml.safe_load(policy_text) if policy_text else None
        summary = assess(
            manifest,
            _jsonl(_read_member(cdir, _MODEL_CALLS)),
            _jsonl(_read_member(cdir, _TOOL_CALLS)),
            replay_policy=policy if isinstance(policy, dict) else None,
        )
    except _OverLimitError as over:
        info = over.info
        return (
            None,
            f"{info.member} is {info.size_bytes} bytes, over the {info.max_bytes}-byte bound "
            "this view classifies; nothing was classified. Run `nova replay <capsule> "
            "--dry-run` for the full answer.",
            info,
        )
    except Exception as exc:  # noqa: BLE001 -- reported to the user, never a 500
        return None, _error_text(exc), None
    return summary.as_dict(), None, None


RecordedState = Literal["present", "absent", "invalid"]


def _recorded(cdir: Path) -> tuple[dict[str, Any] | None, RecordedState, str | None]:
    from novafabric.replay._replayability_summary import (  # noqa: PLC0415
        REPLAYABILITY_REF,
        parse_replayability,
    )

    try:
        text = _read_member(cdir, REPLAYABILITY_REF)
        if text is None:
            return None, "absent", None
        return parse_replayability(text.encode("utf-8")), "present", None
    except _OverLimitError as over:
        return None, "invalid", f"{over.info.member} is over {over.info.max_bytes} bytes"
    except Exception as exc:  # noqa: BLE001 -- malformed or unusable member
        return None, "invalid", _error_text(exc)


def mode_access(held_scope: Any) -> dict[str, ModeAccess]:
    """Per startable mode, its route's required scope against *held_scope* (a ``Scope``)."""
    from novafabric.serve.authz import required_scope, satisfies  # noqa: PLC0415

    out: dict[str, ModeAccess] = {}
    for mode, startable in DASHBOARD_STARTABLE_MODES.items():
        if not startable:
            continue
        required = required_scope("POST", f"/api/runs/{{run_id}}/replay/{MODE_ROUTE[mode]}")
        permitted = (
            required is not None and held_scope is not None and satisfies(held_scope, required)
        )
        out[mode] = ModeAccess(
            required_scope=None if required is None else required.value,
            held_scope=None if held_scope is None else held_scope.value,
            permitted=permitted,
        )
    return out


def replayability_view(run_id: str, cdir: Path, *, held_scope: Any = None) -> dict[str, Any]:
    """The response body for *run_id*, whose capsule is *cdir* (pure read)."""
    current, current_error, over_limit = _current(cdir)
    recorded, recorded_state, recorded_error = _recorded(cdir)
    return ReplayabilityResponse(
        run_id=run_id,
        current=current,
        current_error=current_error,
        current_over_limit=over_limit,
        recorded=recorded,
        recorded_state=recorded_state,
        recorded_error=recorded_error,
        dashboard_modes=dict(DASHBOARD_STARTABLE_MODES),
        mode_access=mode_access(held_scope),
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
    # A plain ``def``: FastAPI runs it in the threadpool, off the event loop.
    def get_replayability(run_id: str, request: Request) -> dict[str, Any]:
        from novafabric.serve.authz import held_credential  # noqa: PLC0415

        cdir = resolve_capsule(run_id, capsule_dir)
        held = held_credential(request)
        return replayability_view(run_id, cdir, held_scope=None if held is None else held.scope)

    return router
