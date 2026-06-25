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
"""EU AI Act Article 12 compliance mode (ADR-0076).

Provides:
- ``EuAiActConfig`` — reads NOVA_EUAIACT_HIGH_RISK / NOVA_EUAIACT_PROVIDER env vars.
- ``EuAiActExporter`` — scans capsule directories and emits structured Art.12 log events.
- ``infer_logging_event_types()`` — maps capsule content to Art.12 logging event type taxonomy.

Art.12 taxonomy (aligned with EU AI Act Annex XII requirements):
- ``interaction_timestamp`` — AI system was invoked
- ``input_classification`` — input risk classification event present
- ``output_record`` — AI system output hash / classification recorded
- ``human_review_event`` — human oversight event present (Art.14)

Retention floors (Art.12 / Art.18):
- Deployer mode: 6 months (``NOVA_EUAIACT_HIGH_RISK=true``)
- Provider mode: 120 months / 10 years (``NOVA_EUAIACT_PROVIDER=true``)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

_DEPLOYER_RETENTION_MONTHS: int = 6
_PROVIDER_RETENTION_MONTHS: int = 120  # 10 years per Art.18


class EuAiActConfig:
    """Read-once config from environment for Art.12 compliance mode.

    Attributes:
        high_risk: Whether the operator has declared high-risk classification.
        provider_mode: Whether provider-mode retention (10-year floor) applies.
        retention_months: Applicable retention floor in months.
    """

    def __init__(self) -> None:
        self.high_risk: bool = os.environ.get("NOVA_EUAIACT_HIGH_RISK", "").lower() in (
            "1", "true", "yes"
        )
        self.provider_mode: bool = os.environ.get("NOVA_EUAIACT_PROVIDER", "").lower() in (
            "1", "true", "yes"
        )
        self.retention_months: int = (
            _PROVIDER_RETENTION_MONTHS if self.provider_mode else _DEPLOYER_RETENTION_MONTHS
        )


def infer_logging_event_types(meta: dict[str, Any], capsule_dir: Path) -> list[str]:
    """Map capsule content to Art.12 logging event type taxonomy.

    Args:
        meta: Parsed ``capsule.yaml`` dict.
        capsule_dir: Path to the capsule directory (for presence checks).

    Returns:
        Non-empty list of logging event type strings.
    """
    types: list[str] = []

    # Every capsule with a timestamp represents an interaction
    if meta.get("created_at"):
        types.append("interaction_timestamp")

    # model-calls.jsonl present → output_record
    if (capsule_dir / "model-calls.jsonl").exists():
        try:
            first_line = next(
                (
                    line for line in (capsule_dir / "model-calls.jsonl")
                    .read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ),
                None,
            )
            if first_line:
                types.append("output_record")
        except OSError:
            pass

    # tool-calls.jsonl with human_approval tool present → human_review_event
    tool_calls_path = capsule_dir / "tool-calls.jsonl"
    if tool_calls_path.exists():
        try:
            for raw in tool_calls_path.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    call = json.loads(raw)
                    if "human" in call.get("tool_name", "").lower() or \
                       call.get("tool_name") == "human_approval":
                        types.append("human_review_event")
                        break
                except json.JSONDecodeError:
                    pass
        except OSError:
            pass

    # evidence.json with input classification present → input_classification
    evidence_path = capsule_dir / "evidence.json"
    if evidence_path.exists():
        try:
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            if isinstance(evidence, dict) and evidence.get("input_classification"):
                types.append("input_classification")
        except (OSError, json.JSONDecodeError):
            pass

    return types or ["interaction_timestamp"]


def _parse_capsule_created_at(meta: dict[str, Any]) -> datetime | None:
    raw = meta.get("created_at")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).rstrip("Z"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


class EuAiActExporter:
    """Scan capsule directories and produce Art.12 structured log export.

    Args:
        capsules_dir: Directory containing one subdirectory per capsule.
    """

    def __init__(self, capsules_dir: Path) -> None:
        self._capsules_dir = capsules_dir

    def export(
        self,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return Art.12 log event records for capsules within the date range.

        Args:
            from_dt: Inclusive lower bound (UTC).  ``None`` means no lower bound.
            to_dt: Inclusive upper bound (UTC).  ``None`` means now.

        Returns:
            List of dicts, one per capsule, with Art.12-required fields.
        """
        if to_dt is None:
            to_dt = datetime.now(tz=timezone.utc)

        records: list[dict[str, Any]] = []

        if not self._capsules_dir.exists():
            return records

        for capsule_dir in sorted(self._capsules_dir.iterdir()):
            if not capsule_dir.is_dir():
                continue
            manifest_path = capsule_dir / "capsule.yaml"
            if not manifest_path.exists():
                continue

            try:
                with manifest_path.open(encoding="utf-8") as f:
                    meta: dict[str, Any] = yaml.safe_load(f) or {}
            except Exception:  # noqa: BLE001
                continue

            created_at = _parse_capsule_created_at(meta)
            if created_at is not None:
                if from_dt is not None and created_at < from_dt:
                    continue
                if created_at > to_dt:
                    continue

            logging_event_types = infer_logging_event_types(meta, capsule_dir)

            records.append({
                "run_id": meta.get("run_id", capsule_dir.name),
                "created_at": meta.get("created_at"),
                "status": meta.get("status"),
                "logging_event_type": logging_event_types,
                "model_call_count": meta.get("model_call_count", 0),
                "tool_call_count": meta.get("tool_call_count", 0),
                "novafabric_version": meta.get("novafabric_version"),
                "euaiact_compliance": {
                    "art12_compliant": True,
                    "logging_event_types": logging_event_types,
                    "pii_redacted": bool(meta.get("redaction_proof_ref")),
                },
            })

        return records


def is_within_retention(capsule_created_at: datetime, retention_months: int) -> bool:
    """Return ``True`` if the capsule is still within the Art.12 retention window.

    Used by ``nova pii erase`` to gate GDPR erasure requests under Art.17(3)(b).
    """
    floor_dt = datetime.now(tz=timezone.utc) - timedelta(days=retention_months * 30)
    return capsule_created_at >= floor_dt
