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

"""ORPHAN_PARENT placeholder logic (cap-003, FR-14, FR-15, FR-16).

FR-14: Configurable pending_parent_timeout (default 24h). After timeout, orphan
       child is linked to a synthetic ORPHAN_PARENT placeholder with a deterministic
       run_id derived from the parent_run_id of the orphan child.

FR-15: Idempotently replace orphan placeholder when real parent arrives late.
       Final state has real parent only; child rows unchanged.

FR-16: Emit novafabric.orphan_created OTel semantic-attribute event on placeholder
       creation (best-effort).

Deterministic placeholder run_id: SHA-256("orphan_parent:" + parent_run_id)[:26]
encoded as Crockford Base32 to produce a valid ULID-format string.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from novafabric.capsule._atomic import atomic_replace, write_text_fsync

logger = logging.getLogger(__name__)

# Prometheus counter — optional at runtime (prometheus_client is a dev dep).
try:
    from prometheus_client import Counter as _PCounter
    _orphan_created_total = _PCounter(
        "novafabric_orphan_created_total",
        "Total ORPHAN_PARENT synthetic placeholders created",
        ["reason"],
    )
except Exception:  # noqa: BLE001
    _orphan_created_total = None  # type: ignore[assignment]

# Crockford Base32 alphabet (ULID)
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _make_deterministic_ulid(seed: str) -> str:
    """Derive a deterministic ULID-format string from seed bytes.

    The result is 26 Crockford Base32 characters derived from SHA-256(seed).
    The first character is constrained to [0-7] per ULID spec (high bit of
    the 128-bit integer must be 0 — time component < 2^47).
    """
    digest = hashlib.sha256(seed.encode()).digest()
    # Take 16 bytes (128 bits) for ULID
    val = int.from_bytes(digest[:16], "big")
    # Ensure first char ∈ [0-7]: clear the top 3 bits of the 130-bit encoding space
    # ULID is 5 bits × 26 chars = 130 bits, but max is 2^128-1.
    # The first char must be ≤ 7 (0b00111), so we mask the top bits.
    val = val & ((1 << 125) - 1)  # clear top 3 bits; first char will be ≤ 7

    result = []
    for _ in range(26):
        result.append(_CROCKFORD[val & 0x1F])
        val >>= 5
    return "".join(reversed(result))


def orphan_placeholder_run_id(parent_run_id: str) -> str:
    """Derive the deterministic ORPHAN_PARENT placeholder run_id (FR-14).

    Deterministic so that multiple workers with the same parent_run_id all
    resolve to the same synthetic placeholder.
    """
    return _make_deterministic_ulid(f"orphan_parent:{parent_run_id}")


class OrphanManager:
    """Manages ORPHAN_PARENT placeholder lifecycle in a spool directory (FR-14, FR-15).

    The spool directory contains <run_id>/capsule.json for each capsule.
    """

    def __init__(
        self,
        spool_dir: Path,
        pending_parent_timeout_s: float = 86400.0,
        clock_fn: Any = None,
    ) -> None:
        self._spool = spool_dir
        self._timeout_s = pending_parent_timeout_s
        self._clock_fn = clock_fn or time.time

    def _capsule_path(self, run_id: str) -> Path:
        return self._spool / run_id / "capsule.json"

    def _read_capsule(self, run_id: str) -> dict[str, Any] | None:
        p = self._capsule_path(run_id)
        if not p.exists():
            return None
        result: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
        return result

    def _write_capsule_atomic(self, run_id: str, data: dict[str, Any]) -> Path:
        run_dir = self._spool / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        dest = run_dir / "capsule.json"
        tmp = dest.with_suffix(".tmp")
        write_text_fsync(tmp, json.dumps(data, indent=2))
        atomic_replace(tmp, dest)
        return dest

    def ensure_placeholder(self, parent_run_id: str) -> dict[str, Any]:
        """Create (or return existing) ORPHAN_PARENT placeholder for parent_run_id.

        If the real parent already exists in the spool, returns its manifest instead.
        FR-14 + FR-16 (OTel emit on creation).
        """
        # Check if real parent exists
        real_manifest = self._read_capsule(parent_run_id)
        if real_manifest and not real_manifest.get("is_synthetic"):
            return real_manifest

        placeholder_id = orphan_placeholder_run_id(parent_run_id)

        # Check if placeholder already exists
        existing = self._read_capsule(placeholder_id)
        if existing:
            return existing

        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()

        placeholder: dict[str, Any] = {
            "schema_version": "0.2.0",
            "capsule_role": "PARENT",
            "is_synthetic": True,
            "global_run_id": parent_run_id,  # best-effort from child context
            "run_id": placeholder_id,
            "status": "RUNNING",
            "created_at": now_iso,
            "orphan_reason": "pending_parent_timeout",
            "original_parent_run_id": parent_run_id,
            "children_arrived": 0,
            "children_expected": None,
        }

        self._write_capsule_atomic(placeholder_id, placeholder)

        logger.info(
            "novafabric.orphan: created synthetic ORPHAN_PARENT placeholder "
            "run_id=%s for parent_run_id=%s",
            placeholder_id,
            parent_run_id,
        )

        if _orphan_created_total is not None:
            _orphan_created_total.labels(reason="pending_parent_timeout").inc()

        # FR-16: emit OTel event (best-effort)
        self._emit_orphan_created_event(parent_run_id, placeholder_id)

        return placeholder

    def replace_with_real_parent(
        self,
        real_parent_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        """Idempotently replace orphan placeholder when real parent arrives (FR-15).

        Writes the real parent capsule to its correct run_id location.
        The synthetic placeholder under orphan_placeholder_run_id(parent_run_id)
        remains but is superseded (child rows unchanged).

        Returns the written real parent manifest.
        """
        parent_run_id = real_parent_manifest.get("run_id", "")
        if not parent_run_id:
            raise ValueError("real_parent_manifest must have a run_id field")

        # Write the real parent
        self._write_capsule_atomic(parent_run_id, real_parent_manifest)

        # Mark placeholder as superseded (if it exists)
        placeholder_id = orphan_placeholder_run_id(parent_run_id)
        existing_placeholder = self._read_capsule(placeholder_id)
        if existing_placeholder and existing_placeholder.get("is_synthetic"):
            existing_placeholder["superseded_by"] = parent_run_id
            existing_placeholder["status"] = "RUNNING"  # retain state
            self._write_capsule_atomic(placeholder_id, existing_placeholder)
            logger.info(
                "novafabric.orphan: placeholder %s superseded by real parent %s",
                placeholder_id,
                parent_run_id,
            )

        return real_parent_manifest

    def check_orphan_timeouts(self) -> list[str]:
        """Scan spool for children whose parent hasn't arrived within timeout.

        Returns list of parent_run_id values for which placeholders were created.
        """
        if not self._spool.is_dir():
            return []

        now = self._clock_fn()
        created_placeholders: list[str] = []

        # Collect all child manifests
        for entry in self._spool.iterdir():
            if not entry.is_dir():
                continue
            manifest_path = entry / "capsule.json"
            if not manifest_path.exists():
                continue
            try:
                data: dict[str, Any] = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
            except Exception:
                continue

            parent_run_id = data.get("parent_run_id")
            if not parent_run_id:
                continue

            # Check if real parent exists
            real_parent = self._read_capsule(parent_run_id)
            if real_parent and not real_parent.get("is_synthetic"):
                continue

            # Check timeout
            created_at_str = data.get("created_at", "")
            try:
                from datetime import datetime
                created_epoch = datetime.fromisoformat(
                    created_at_str.replace("Z", "+00:00")
                ).timestamp()
            except Exception:
                continue

            elapsed = now - created_epoch
            if elapsed >= self._timeout_s:
                self.ensure_placeholder(parent_run_id)
                created_placeholders.append(parent_run_id)

        return created_placeholders

    def _emit_orphan_created_event(
        self,
        parent_run_id: str,
        placeholder_id: str,
    ) -> None:
        """Emit novafabric.orphan_created OTel event (FR-16, best-effort)."""
        try:
            from opentelemetry import trace

            tracer = trace.get_tracer("novafabric.capsule.orphan")
            with tracer.start_as_current_span("novafabric.orphan_created") as span:
                span.set_attributes({
                    "novafabric.orphan.parent_run_id": parent_run_id,
                    "novafabric.orphan.placeholder_id": placeholder_id,
                    "novafabric.orphan.reason": "pending_parent_timeout",
                })
        except Exception as exc:
            logger.debug("novafabric.orphan: OTel emit failed (ignored): %s", exc)
