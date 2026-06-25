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
"""EU AI Act Annex IV technical documentation exporter (cap-002).

Regulation (EU) 2024/1689, Annex IV — 15 mandatory elements.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .models import (
    COMPLETENESS_COMPLETE,
    COMPLETENESS_MISSING,
    COMPLETENESS_PARTIAL,
    POPULATION_CAPSULE,
    POPULATION_OPERATOR,
    AnnexIVDocument,
    AnnexIVElement,
)

_MAPPING_PATH = Path(__file__).parent / "annex_iv_mapping.yaml"


def _load_mapping() -> list[dict[str, Any]]:
    with open(_MAPPING_PATH) as f:
        data = yaml.safe_load(f)
    result: list[dict[str, Any]] = data["elements"]
    return result


def _resolve(obj: Any, path: str) -> Any:
    """Walk dot-notation *path* into *obj*; return ``None`` if not found."""
    parts = re.split(r"\.", path)
    cur = obj
    for part in parts:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _populate_template(template: str, manifest: dict[str, Any]) -> str:
    """Replace ``{key}`` placeholders in *template* with values from *manifest*."""
    def replacer(m: re.Match[str]) -> str:
        key = m.group(1)
        val = _resolve(manifest, key)
        return str(val) if val is not None else f"<{key}:not_available>"

    return re.sub(r"\{([^}]+)\}", replacer, template)


def _assess_completeness(
    manifest: dict[str, Any],
    required_paths: list[str],
    optional_paths: list[str],
    population_method: str,
) -> str:
    """Determine completeness status for an element."""
    if population_method == POPULATION_OPERATOR:
        return COMPLETENESS_MISSING  # must be supplied by operator

    if not required_paths:
        # No required fields — check optional
        if not optional_paths:
            return COMPLETENESS_COMPLETE
        present = sum(1 for p in optional_paths if _resolve(manifest, p) is not None)
        if present == 0:
            return COMPLETENESS_PARTIAL
        return COMPLETENESS_COMPLETE

    missing_required = [p for p in required_paths if _resolve(manifest, p) is None]
    if not missing_required:
        return COMPLETENESS_COMPLETE

    present_optional = sum(1 for p in optional_paths if _resolve(manifest, p) is not None)
    if present_optional > 0:
        return COMPLETENESS_PARTIAL
    return COMPLETENESS_MISSING


class AnnexIVExporter:
    """Builds an :class:`AnnexIVDocument` from a capsule store.

    Reads the capsule manifest and associated JSONL files to populate
    the 15 mandatory EU AI Act Annex IV elements.

    Elements marked ``operator_declared`` are populated with a template
    instructing the operator to fill in the missing information.
    """

    def __init__(self, mapping_path: Path | None = None) -> None:
        self._mapping: list[dict[str, Any]] = _load_mapping()
        if mapping_path is not None:
            with open(mapping_path) as f:
                self._mapping = yaml.safe_load(f)["elements"]

    def build_annex_iv_document(
        self,
        deployment_id: str,
        capsule_dir: Path,
        operator_overrides: dict[int, str] | None = None,
    ) -> AnnexIVDocument:
        """Build and return an :class:`AnnexIVDocument`.

        Args:
            deployment_id: Operator-assigned deployment identifier.
            capsule_dir: Path to the run capsule directory.
            operator_overrides: Optional mapping of element_id -> content string
                                 for operator-declared elements.

        Returns:
            A complete :class:`AnnexIVDocument` with all 15 elements.
        """
        manifest = self._load_manifest(capsule_dir)
        overrides = operator_overrides or {}
        elements: list[AnnexIVElement] = []

        for spec in self._mapping:
            elem_id = spec["element_id"]
            method = spec.get("population_method", POPULATION_CAPSULE)
            required = spec.get("required_field_paths", [])
            optional = spec.get("optional_field_paths", [])
            template = spec.get("description_template", "")

            completeness = _assess_completeness(manifest, required, optional, method)

            if elem_id in overrides:
                content = overrides[elem_id]
                completeness = COMPLETENESS_COMPLETE
            elif method == POPULATION_OPERATOR:
                content = (
                    f"[OPERATOR REQUIRED] {spec['element_title']}: "
                    f"{_populate_template(template, manifest)}"
                )
            else:
                content = _populate_template(template, manifest) if template else ""

            # Gather evidence refs (capsule IDs, eval refs etc.)
            evidence_refs = self._gather_evidence_refs(manifest, spec)

            elements.append(
                AnnexIVElement(
                    element_id=elem_id,
                    element_title=spec["element_title"],
                    content=content,
                    evidence_refs=evidence_refs,
                    population_method=method,
                    completeness_flag=completeness,
                )
            )

        return AnnexIVDocument(
            deployment_id=deployment_id,
            generated_at=datetime.now(timezone.utc).isoformat(),
            elements=elements,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_manifest(self, capsule_dir: Path) -> dict[str, Any]:
        manifest_path = capsule_dir / "capsule.yaml"
        if not manifest_path.exists():
            raise FileNotFoundError(
                f"capsule.yaml not found in {capsule_dir}. "
                "Ensure the capsule directory is valid."
            )
        with open(manifest_path) as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise ValueError(
                f"capsule.yaml in {capsule_dir} did not parse as a dict"
            )
        return data

    def _gather_evidence_refs(
        self, manifest: dict[str, Any], spec: dict[str, Any]
    ) -> list[str]:
        """Collect references to evidence artefacts for the element."""
        refs: list[str] = []
        run_id = manifest.get("run_id")
        if run_id:
            refs.append(f"capsule:{run_id}")

        all_paths = spec.get("required_field_paths", []) + spec.get("optional_field_paths", [])
        for path in all_paths:
            val = _resolve(manifest, path)
            if val and isinstance(val, str) and ("ref" in path or "_ref" in path):
                refs.append(f"file:{val}")
        return refs
