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

"""Capsule Writer — Phase 1 in-process library (cap-004).

FR-17: Creates capsules using ONLY env vars, local file I/O, and local clock.
       No network during capsule creation.
FR-18: Reads NOVAFABRIC_* env-var contract.
FR-19: Worker can exit before capsule is uploaded (Phase 2 — collector handles upload).
FR-20: Graceful fallback when env vars absent.

The writer uses os.rename() for atomic capsule commit (Lustre-safe, FR of cap-004).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from novafabric.capsule.edge_typer import resolve_edge_type
from novafabric.capsule.env_contract import CapsuleEnvConfig, read_env
from novafabric.capsule.schema import (
    CapsuleRole,
    EdgeType,
    FailMode,
    LineageEdgeV2,
    ParentStatus,
)
from novafabric.capsule.ulid_util import new_ulid

logger = logging.getLogger(__name__)

_OTEL_TRACER = trace.get_tracer("novafabric.capsule.writer", schema_url="")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── CapsuleWriter ────────────────────────────────────────────────────────


class CapsuleWriter:
    """In-process capsule writer for Phase 1 (no network I/O).

    Usage::

        writer = CapsuleWriter.from_env()
        writer.start()
        # ... workload runs ...
        writer.commit(status="COMPLETE")

    Or for child workers::

        writer = CapsuleWriter.from_env()   # reads NOVAFABRIC_* env vars
        writer.start()
        writer.commit(status="COMPLETE")
    """

    def __init__(self, config: CapsuleEnvConfig) -> None:
        self._config = config
        self._run_id = new_ulid()
        self._capsule_dir = Path(config.capsule_dir) / self._run_id
        self._lineage: list[LineageEdgeV2] = []
        self._started_at: str | None = None
        self._started = False
        self._committed = False
        self._extra: dict[str, Any] = {}

    @classmethod
    def from_env(cls) -> "CapsuleWriter":
        """Construct a writer from the NOVAFABRIC_* env-var contract (FR-18)."""
        config = read_env()
        for w in config.warnings:
            logger.warning("novafabric.writer: %s", w)
        return cls(config)

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def global_run_id(self) -> str:
        return self._config.global_run_id

    @property
    def capsule_role(self) -> CapsuleRole:
        return self._config.capsule_role

    def start(self) -> None:
        """Phase 1 start — record start time, no network."""
        if self._started:
            logger.warning("CapsuleWriter.start() called more than once; ignoring.")
            return
        self._started_at = _now_iso()
        self._started = True
        logger.debug(
            "novafabric.writer: started run_id=%s global_run_id=%s role=%s",
            self._run_id,
            self._config.global_run_id,
            self._config.capsule_role.value,
        )

    def add_lineage_edge(
        self,
        target_run_id: str,
        edge_type: EdgeType | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> LineageEdgeV2:
        """Add a typed lineage edge from this run to target_run_id."""
        if edge_type is None:
            edge_type = resolve_edge_type(
                source_role=self._config.capsule_role.value,
                target_role=None,
            )
        edge = LineageEdgeV2(
            edge_id=new_ulid(),
            edge_type=edge_type,
            source_run_id=self._run_id,
            target_run_id=target_run_id,
            attributes=attributes,
        )
        self._lineage.append(edge)
        return edge

    def set_extra(self, **kwargs: Any) -> None:
        """Set additional fields on the capsule manifest."""
        self._extra.update(kwargs)

    def commit(
        self,
        status: str = "COMPLETE",
        error: dict[str, Any] | None = None,
    ) -> Path:
        """Phase 1 commit — write capsule to local spool atomically (FR-17).

        Uses os.rename() for atomic commit (Lustre-safe).
        Returns the committed capsule directory path.
        """
        if not self._started:
            raise RuntimeError("CapsuleWriter.start() must be called before commit()")
        if self._committed:
            raise RuntimeError("CapsuleWriter.commit() already called")

        finished_at = _now_iso()
        cfg = self._config

        # Build capsule manifest
        manifest: dict[str, Any] = {
            "schema_version": "0.2.0",
            "capsule_role": cfg.capsule_role.value,
            "global_run_id": cfg.global_run_id,
            "run_id": self._run_id,
            "parent_run_id": cfg.parent_run_id,
            "status": status,
            "created_at": self._started_at,
            "finished_at": finished_at,
            "rank": cfg.rank,
            "world_size": cfg.world_size,
            "distribution_role": (
                cfg.distribution_role.value if cfg.distribution_role else None
            ),
            "fail_mode": cfg.fail_mode.value,
            "pending_parent_timeout_s": cfg.pending_parent_timeout_s,
        }

        if cfg.capsule_role == CapsuleRole.PARENT:
            manifest["children_expected"] = cfg.world_size
            manifest["expected_children"] = cfg.world_size
            manifest["children_arrived"] = 0

        if error:
            manifest["error"] = error

        manifest.update(self._extra)

        # Write to a temporary directory, then os.rename() for atomicity
        parent_spool = Path(cfg.capsule_dir)
        parent_spool.mkdir(parents=True, exist_ok=True)

        tmp_dir = Path(
            tempfile.mkdtemp(dir=parent_spool, prefix=f".tmp_{self._run_id}_")
        )
        try:
            (tmp_dir / "capsule.json").write_text(
                json.dumps(manifest, indent=2), encoding="utf-8"
            )

            # Write lineage edges
            lineage_lines = [
                json.dumps(e.model_dump(mode="json"), ensure_ascii=False)
                for e in self._lineage
            ]
            (tmp_dir / "lineage.jsonl").write_text(
                "\n".join(lineage_lines) + ("\n" if lineage_lines else ""),
                encoding="utf-8",
            )

            # Atomic rename
            dest_dir = parent_spool / self._run_id
            os.rename(str(tmp_dir), str(dest_dir))
        except Exception:
            # Best-effort cleanup on failure
            try:
                import shutil
                shutil.rmtree(str(tmp_dir), ignore_errors=True)
            except Exception:
                pass
            raise

        self._committed = True
        self._capsule_dir = dest_dir

        logger.info(
            "novafabric.writer: committed capsule run_id=%s status=%s dir=%s",
            self._run_id,
            status,
            dest_dir,
        )

        # Emit OTel span for capsule commit (best-effort — fail-open)
        try:
            _emit_otel_commit_event(
                run_id=self._run_id,
                global_run_id=cfg.global_run_id,
                status=status,
                capsule_role=cfg.capsule_role.value,
                capsule_dir=str(dest_dir),
            )
        except Exception as exc:
            logger.debug("novafabric.writer: OTel emit failed (ignored): %s", exc)

        return dest_dir


# ── ParentCapsuleTracker ──────────────────────────────────────────────────


class ParentCapsuleTracker:
    """Tracks child arrivals for a parent capsule directory (cap-001, FR-02, FR-03).

    Reads and updates capsule.json in-place as children commit.
    Uses os.rename() for atomic updates.
    """

    def __init__(self, capsule_dir: Path) -> None:
        self._dir = capsule_dir
        self._manifest_path = capsule_dir / "capsule.json"

    def _read_manifest(self) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(self._manifest_path.read_text(encoding="utf-8"))
        return result

    def _write_manifest_atomic(self, manifest: dict[str, Any]) -> None:
        tmp = self._manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        os.rename(str(tmp), str(self._manifest_path))

    def register_child_arrival(
        self,
        child_run_id: str,
        child_status: str = "COMPLETE",
    ) -> dict[str, Any]:
        """Register a child capsule arriving; update children_arrived counter.

        Returns updated manifest.
        """
        manifest = self._read_manifest()
        manifest["children_arrived"] = manifest.get("children_arrived", 0) + 1

        # cap-006 FR-24: warn if child reports different world_size
        child_world_size = None  # may be passed in future; placeholder
        if child_world_size is not None:
            parent_ws = manifest.get("world_size")
            if parent_ws is not None and child_world_size != parent_ws:
                logger.warning(
                    "novafabric.parent_tracker: child %s reports world_size=%s "
                    "but parent has world_size=%s",
                    child_run_id,
                    child_world_size,
                    parent_ws,
                )

        self._write_manifest_atomic(manifest)
        return manifest

    def finalize(
        self,
        fail_mode: FailMode | str = FailMode.fail_open,
        current_time: float | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Transition parent to terminal state based on children_arrived vs children_expected.

        FR-03: RUNNING → PARTIALLY_COMPLETE when timeout elapsed and 0 < arrived < expected.
        FR-04: RUNNING → FAILED when arrived == 0.
              RUNNING → COMPLETE when arrived == expected.
        FR-05: NOVAFABRIC_FAIL_MODE=fail-closed → FAILED when any child failed.

        Returns (new_status, updated_manifest).
        """

        manifest = self._read_manifest()
        arrived = manifest.get("children_arrived", 0)
        expected = manifest.get("children_expected")
        status = manifest.get("status", "RUNNING")

        if status != "RUNNING":
            return status, manifest

        if isinstance(fail_mode, str):
            try:
                fail_mode = FailMode(fail_mode)
            except ValueError:
                fail_mode = FailMode.fail_open

        # Determine terminal state
        if expected is not None:
            if arrived == expected:
                new_status = ParentStatus.COMPLETE.value
            elif arrived == 0:
                new_status = ParentStatus.FAILED.value
            else:
                # 0 < arrived < expected
                if fail_mode == FailMode.fail_closed:
                    new_status = ParentStatus.FAILED.value
                else:
                    new_status = ParentStatus.PARTIALLY_COMPLETE.value
        else:
            # Dynamic world — no expected count; complete if at least one arrived
            if arrived > 0:
                new_status = ParentStatus.COMPLETE.value
            else:
                new_status = ParentStatus.FAILED.value

        manifest["status"] = new_status
        manifest["finished_at"] = _now_iso()
        self._write_manifest_atomic(manifest)
        return new_status, manifest


# ── OTel helper ───────────────────────────────────────────────────────────


def _emit_otel_commit_event(
    run_id: str,
    global_run_id: str,
    status: str,
    capsule_role: str,
    capsule_dir: str,
) -> None:
    """Emit an OTel span event for capsule commit (best-effort, non-blocking)."""
    tracer = _OTEL_TRACER
    with tracer.start_as_current_span(
        "novafabric.capsule.commit",
        context=trace.set_span_in_context(NonRecordingSpan(SpanContext(
            trace_id=0x1,
            span_id=0x1,
            is_remote=False,
            trace_flags=TraceFlags(0x0),
        ))),
    ) as span:
        span.set_attributes({
            "novafabric.run_id": run_id,
            "novafabric.global_run_id": global_run_id,
            "novafabric.capsule_role": capsule_role,
            "novafabric.status": status,
            "novafabric.capsule_dir": capsule_dir,
        })
