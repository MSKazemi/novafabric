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

"""Schema validator + state-machine for distributed capsule runs (cap-001, FR-01–FR-06).

FR-01: Schema validator accepts parent with status == "PARTIALLY_COMPLETE".
FR-03: State transition: RUNNING → PARTIALLY_COMPLETE when timeout elapsed and
       0 < children_arrived < children_expected.
FR-04: boundary transitions: 0 → FAILED, all → COMPLETE.
FR-05: fail_mode=fail-closed → FAILED when any child failed.
FR-06: `nova validate --distributed <parent-run-id>` exit codes 0/1/2.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import jsonschema  # type: ignore[import-untyped]

from novafabric.capsule.schema import FailMode, ParentStatus

logger = logging.getLogger(__name__)

# ── Schema loading ────────────────────────────────────────────────────────

_SCHEMA_PATH = (
    Path(__file__).parents[3] / "schemas" / "parent_child_capsule_v1.schema.json"
)


def _load_schema() -> dict[str, Any]:
    result: dict[str, Any] = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return result


def validate_capsule_json(data: dict[str, Any]) -> None:
    """Validate a capsule manifest dict against the JSON Schema 2020-12 schema.

    Raises jsonschema.ValidationError on failure.
    """
    schema = _load_schema()
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema)
    errors = list(validator.iter_errors(data))
    if errors:
        # Report the first error
        raise jsonschema.ValidationError(
            f"Capsule validation failed: {errors[0].message}",
            validator=errors[0].validator,
            path=errors[0].absolute_path,
            cause=errors[0],
        )


# ── Distributed validate ──────────────────────────────────────────────────


@dataclass
class DistributedValidationResult:
    """Result of `nova validate --distributed <parent-run-id>` (FR-06)."""

    parent_run_id: str
    status: str
    children_expected: int | None
    children_arrived: int
    exit_code: int  # 0=COMPLETE, 1=FAILED, 2=PARTIALLY_COMPLETE
    message: str


class DistributedCapsuleValidator:
    """Validates a distributed run's capsule state-machine.

    Pluggable clock for deterministic testing (monotonic_time_fn).
    """

    def __init__(
        self,
        capsule_dir: Path,
        monotonic_time_fn: Callable[[], float] | None = None,
    ) -> None:
        self._dir = capsule_dir
        self._time_fn = monotonic_time_fn or time.monotonic

    def _read_manifest(self) -> dict[str, Any]:
        manifest_path = self._dir / "capsule.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"capsule.json not found in {self._dir}")
        result: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
        return result

    def validate_distributed(
        self,
        parent_run_id: str,
    ) -> DistributedValidationResult:
        """Validate the distributed capsule state and return exit code.

        Exit codes (FR-06):
          0 → COMPLETE
          1 → FAILED
          2 → PARTIALLY_COMPLETE
        """
        manifest = self._read_manifest()
        status = manifest.get("status", "RUNNING")
        children_expected: int | None = manifest.get("children_expected")
        children_arrived: int = manifest.get("children_arrived", 0)

        # Map status to exit code
        if status == ParentStatus.COMPLETE.value:
            exit_code = 0
            message = f"COMPLETE: {children_arrived}/{children_expected} children arrived"
        elif status == ParentStatus.PARTIALLY_COMPLETE.value:
            exit_code = 2
            message = (
                f"PARTIALLY_COMPLETE: {children_arrived}/{children_expected} children arrived"
            )
        else:
            exit_code = 1
            message = f"FAILED: {children_arrived}/{children_expected} children arrived"

        return DistributedValidationResult(
            parent_run_id=parent_run_id,
            status=status,
            children_expected=children_expected,
            children_arrived=children_arrived,
            exit_code=exit_code,
            message=message,
        )

    def check_timeout_and_finalize(
        self,
        created_epoch_s: float,
        fail_mode: FailMode | str = FailMode.fail_open,
    ) -> str | None:
        """Check if pending_parent_timeout has elapsed and finalize if so.

        Returns the new status string, or None if no transition occurred.
        """
        manifest = self._read_manifest()
        if manifest.get("status", "RUNNING") != "RUNNING":
            return None

        timeout_s = float(manifest.get("pending_parent_timeout_s", 86400.0))
        now = self._time_fn()
        elapsed = now - created_epoch_s

        if elapsed < timeout_s:
            return None

        # Timeout elapsed — transition
        from novafabric.capsule.writer import ParentCapsuleTracker
        tracker = ParentCapsuleTracker(self._dir)
        new_status, _ = tracker.finalize(fail_mode=fail_mode)
        return new_status
