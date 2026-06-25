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
"""AI Bill of Materials (AI-SBOM) exporter using CycloneDX 1.7 ML-BOM format (cap-008).

Generates a CycloneDX 1.7 JSON document (ECMA-424 2nd Edition, October 2025) from
capsule metadata, describing the AI system components: models, datasets, training
parameters, and evaluation results.

CycloneDX 1.7 additions over 1.6:
  - ``metadata.tools`` is now an object ``{components: [...]}`` (was an array in ≤1.4)
  - ``metadata.lifecycles`` documents the BOM phase (``post-build`` for completed runs)
  - ``modelCard.limitations`` documents known model restrictions (relevant for CRA Art.9)
  - ``components`` can include ``type: "data"`` entries for datasets via ``lineage_datasets``

No CycloneDX SDK dependency — the format is plain JSON per the ECMA-424 2nd Edition spec.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class AIBOMComponent(BaseModel):
    """A CycloneDX 1.7 component entry for an AI system element."""
    bom_ref: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: str = "machine-learning-model"
    name: str
    version: str = ""
    description: str = ""
    licenses: list[dict[str, str]] = Field(default_factory=list)
    properties: list[dict[str, str]] = Field(default_factory=list)
    # ML-specific extension
    model_card: dict[str, Any] | None = None


class AIBOMDocument(BaseModel):
    """CycloneDX 1.7 ML-BOM document (ECMA-424 2nd Edition)."""
    bom_format: str = "CycloneDX"
    spec_version: str = "1.7"
    serial_number: str = Field(default_factory=lambda: f"urn:uuid:{uuid.uuid4()}")
    version: int = 1
    metadata: dict[str, Any] = Field(default_factory=dict)
    components: list[AIBOMComponent] = Field(default_factory=list)
    # Derived
    capsule_id: str = ""
    run_id: str = ""
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Exporter
# ---------------------------------------------------------------------------

class AIBOMExporter:
    """Generates a CycloneDX 1.7 AI-SBOM (ML-BOM) from a capsule directory.

    Reads ``capsule.yaml`` to extract model names, provider, and configuration.
    Reads eval results if present to populate performance metrics.
    No external SDK required — output is plain CycloneDX 1.7 JSON.
    """

    def build_aibom(self, capsule_dir: Path) -> AIBOMDocument:
        """Build a CycloneDX ML-BOM from *capsule_dir*.

        Args:
            capsule_dir: Path to an unpacked capsule directory.

        Returns:
            A populated :class:`AIBOMDocument`.
        """
        manifest = self._load_manifest(capsule_dir)
        run_id = manifest.get("run_id", "unknown")

        components: list[AIBOMComponent] = []

        # Primary model component.
        model_name = manifest.get("model") or manifest.get("model_id", "unknown-model")
        provider = manifest.get("provider", "")
        model_version = manifest.get("model_version", "")

        model_props = [
            {"name": "nova:provider", "value": provider},
            {"name": "nova:run_id", "value": run_id},
        ]
        if manifest.get("input_tokens"):
            model_props.append(
                {"name": "nova:input_tokens", "value": str(manifest["input_tokens"])}
            )
        if manifest.get("output_tokens"):
            model_props.append(
                {"name": "nova:output_tokens", "value": str(manifest["output_tokens"])}
            )

        model_card = self._build_model_card(manifest, capsule_dir)
        components.append(
            AIBOMComponent(
                type="machine-learning-model",
                name=model_name,
                version=model_version,
                description=f"AI model captured in NovaFabric run {run_id}",
                properties=model_props,
                model_card=model_card,
            )
        )

        # Tool components.
        for tool in manifest.get("tools", []):
            tool_name = tool if isinstance(tool, str) else tool.get("name", "unknown-tool")
            components.append(
                AIBOMComponent(
                    type="library",
                    name=tool_name,
                    description=f"Tool used in run {run_id}",
                    properties=[{"name": "nova:component_type", "value": "tool"}],
                )
            )

        # Dataset components (CycloneDX 1.7 type: "data") from lineage_datasets.
        for ds in manifest.get("lineage_datasets", []):
            ds_name = ds if isinstance(ds, str) else ds.get("name", "unknown-dataset")
            ds_version = "" if isinstance(ds, str) else str(ds.get("version", ""))
            ds_type = "" if isinstance(ds, str) else ds.get("type", "")
            ds_props = [{"name": "nova:dataset_type", "value": ds_type}] if ds_type else []
            components.append(
                AIBOMComponent(
                    type="data",
                    name=ds_name,
                    version=ds_version,
                    description=f"Dataset referenced in run {run_id}",
                    properties=ds_props,
                )
            )

        from importlib.metadata import version as _pkg_version

        try:
            _nova_version = _pkg_version("novafabric")
        except Exception:
            _nova_version = "0.0.0"

        # CycloneDX 1.5+ requires metadata.tools as {components:[...]} object.
        metadata = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tools": {
                "components": [
                    {
                        "type": "application",
                        "author": "NovaFabric",
                        "name": "novafabric",
                        "version": _nova_version,
                    }
                ]
            },
            # CycloneDX 1.7 lifecycles: document the BOM phase.
            "lifecycles": [{"phase": "post-build"}],
            "component": {
                "type": "application",
                "name": capsule_dir.name,
                "description": "AI agent run capsule",
            },
        }

        return AIBOMDocument(
            metadata=metadata,
            components=components,
            capsule_id=str(capsule_dir.name),
            run_id=run_id,
        )

    def export_json(self, document: AIBOMDocument, output_path: Path) -> Path:
        """Write the CycloneDX ML-BOM to *output_path* as JSON."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "bomFormat": document.bom_format,
            "specVersion": document.spec_version,
            "serialNumber": document.serial_number,
            "version": document.version,
            "metadata": document.metadata,
            "components": [self._component_to_cdx(c) for c in document.components],
        }
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
        return output_path

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _load_manifest(self, capsule_dir: Path) -> dict[str, Any]:
        manifest_path = capsule_dir / "capsule.yaml"
        if not manifest_path.exists():
            return {}
        import yaml

        with open(manifest_path) as fh:
            return yaml.safe_load(fh) or {}

    def _build_model_card(
        self, manifest: dict[str, Any], capsule_dir: Path
    ) -> dict[str, Any] | None:
        """Build a CycloneDX 1.7 modelCard from capsule data."""
        card: dict[str, Any] = {}

        # Quantitative analysis from eval results.
        eval_path = capsule_dir / "eval_result.json"
        if eval_path.exists():
            try:
                eval_data = json.loads(eval_path.read_text())
                metrics = eval_data.get("metrics", [])
                card["quantitativeAnalysis"] = {
                    "performanceMetrics": [
                        {
                            "type": m.get("name", "unknown"),
                            "value": str(m.get("value", "")),
                        }
                        for m in metrics
                    ]
                }
            except Exception:
                pass

        # Considerations from capsule metadata.
        considerations: dict[str, Any] = {}
        if manifest.get("safety_notes"):
            considerations["safetyRisks"] = manifest["safety_notes"]
        if manifest.get("bias_notes"):
            considerations["fairnessAssessments"] = [
                {"groupAtRisk": "see_capsule", "benefits": manifest["bias_notes"]}
            ]
        if considerations:
            card["considerations"] = considerations

        # CycloneDX 1.7 limitations — known restrictions (CRA Art.9 disclosure).
        raw_lims = manifest.get("limitations", [])
        if raw_lims:
            card["limitations"] = [
                lim if isinstance(lim, str) else str(lim) for lim in raw_lims
            ]

        return card if card else None

    def _component_to_cdx(self, comp: AIBOMComponent) -> dict[str, Any]:
        out: dict[str, Any] = {
            "bom-ref": comp.bom_ref,
            "type": comp.type,
            "name": comp.name,
        }
        if comp.version:
            out["version"] = comp.version
        if comp.description:
            out["description"] = comp.description
        if comp.licenses:
            out["licenses"] = comp.licenses
        if comp.properties:
            out["properties"] = comp.properties
        if comp.model_card:
            out["modelCard"] = comp.model_card
        return out
