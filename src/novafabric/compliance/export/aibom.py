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

# TLP 2.0 distribution markers (NF-056 R5).
TLP_MARKERS = frozenset(
    {"TLP:CLEAR", "TLP:GREEN", "TLP:AMBER", "TLP:AMBER+STRICT", "TLP:RED"}
)


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
    # CycloneDX 1.7 / NF-056 additive fields (emitted only when populated).
    hashes: list[dict[str, str]] = Field(default_factory=list)
    external_references: list[dict[str, str]] = Field(default_factory=list)
    citations: list[dict[str, Any]] = Field(default_factory=list)


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

    def build_aibom(
        self,
        capsule_dir: Path,
        *,
        citations: bool = False,
        tlp: str | None = None,
        model_card_ref: str | None = None,
        include_datasets: bool = True,
    ) -> AIBOMDocument:
        """Build a CycloneDX ML-BOM from *capsule_dir*.

        Args:
            capsule_dir: Path to an unpacked capsule directory.
            citations: NF-056 R4 — bind each model/dataset component to the source
                capsule/evidence digest (and an ADR-0097 inclusion proof when present).
            tlp: NF-056 R5 — a TLP 2.0 distribution marker recorded in
                ``metadata.properties`` (``novafabric:tlp``). Must be one of
                :data:`TLP_MARKERS`.
            model_card_ref: NF-056 R6 — ``auto`` (registry URI derived from the model
                id) or an explicit path/URI; adds a ``model-card`` external reference to
                every ``machine-learning-model`` component. ``None`` = omit.
            include_datasets: emit ``type: "data"`` dataset components from lineage
                (default on; unchanged behavior). ``--no-include-datasets`` suppresses them.

        Returns:
            A populated :class:`AIBOMDocument`.

        With every option at its default the output is byte-for-byte identical to the
        pre-NF-056 exporter (additive, opt-in).
        """
        if tlp is not None and tlp not in TLP_MARKERS:
            raise ValueError(
                f"invalid TLP marker {tlp!r}; expected one of {sorted(TLP_MARKERS)}"
            )

        manifest = self._load_manifest(capsule_dir)
        run_id = manifest.get("run_id", "unknown")

        capsule_digest = self._capsule_digest(capsule_dir) if citations else None
        inclusion_proof = self._inclusion_proof(manifest) if citations else None

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
        model_hashes = self._model_hashes(manifest) if citations else []
        model_ext_refs = (
            self._model_card_ref(model_name, model_version, model_card_ref)
            if model_card_ref
            else []
        )
        model_citations = (
            self._citations(capsule_digest, run_id, "outputs/model", inclusion_proof)
            if citations
            else []
        )
        components.append(
            AIBOMComponent(
                type="machine-learning-model",
                name=model_name,
                version=model_version,
                description=f"AI model captured in NovaFabric run {run_id}",
                properties=model_props,
                model_card=model_card,
                hashes=model_hashes,
                external_references=model_ext_refs,
                citations=model_citations,
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
        if include_datasets:
            for ds in manifest.get("lineage_datasets", []):
                ds_name = ds if isinstance(ds, str) else ds.get("name", "unknown-dataset")
                ds_version = "" if isinstance(ds, str) else str(ds.get("version", ""))
                ds_type = "" if isinstance(ds, str) else ds.get("type", "")
                ds_props = [{"name": "nova:dataset_type", "value": ds_type}] if ds_type else []
                ds_hash = "" if isinstance(ds, str) else str(ds.get("hash", ""))
                ds_hashes = (
                    [{"alg": "SHA-256", "content": ds_hash.removeprefix("sha256:")}]
                    if citations and ds_hash
                    else []
                )
                ds_citations = (
                    self._citations(
                        f"sha256:{ds_hash.removeprefix('sha256:')}" if ds_hash else capsule_digest,
                        run_id,
                        f"datasets/{ds_name}",
                        inclusion_proof,
                    )
                    if citations
                    else []
                )
                components.append(
                    AIBOMComponent(
                        type="data",
                        name=ds_name,
                        version=ds_version,
                        description=f"Dataset referenced in run {run_id}",
                        properties=ds_props,
                        hashes=ds_hashes,
                        citations=ds_citations,
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

        # NF-056 R5: TLP distribution marker in metadata.properties (opt-in).
        if tlp is not None:
            metadata["properties"] = [{"name": "novafabric:tlp", "value": tlp}]

        return AIBOMDocument(
            metadata=metadata,
            components=components,
            capsule_id=str(capsule_dir.name),
            run_id=run_id,
        )

    def to_payload(self, document: AIBOMDocument) -> dict[str, Any]:
        """Return the CycloneDX 1.7 JSON payload dict for *document*."""
        return {
            "$schema": "https://cyclonedx.org/schema/bom-1.7.schema.json",
            "bomFormat": document.bom_format,
            "specVersion": document.spec_version,
            "serialNumber": document.serial_number,
            "version": document.version,
            "metadata": document.metadata,
            "components": [self._component_to_cdx(c) for c in document.components],
        }

    def export_json(self, document: AIBOMDocument, output_path: Path) -> Path:
        """Write the CycloneDX ML-BOM to *output_path* as JSON."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(self.to_payload(document), fh, indent=2, default=str)
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

    def _capsule_digest(self, capsule_dir: Path) -> str:
        """Content-addressed digest of the capsule (NF-056 R4 citation target)."""
        from novafabric.evidence.merkle import capsule_merkle_root

        return capsule_merkle_root(capsule_dir)

    def _inclusion_proof(self, manifest: dict[str, Any]) -> dict[str, Any] | None:
        """ADR-0097 inclusion proof for the capsule digest, if the manifest records one."""
        proof = manifest.get("inclusion_proof") or manifest.get("tlog")
        if isinstance(proof, dict) and proof:
            return proof
        return None

    def _model_hashes(self, manifest: dict[str, Any]) -> list[dict[str, str]]:
        """SHA-256 model hash (NF-056 R2) — emitted only when a digest is recorded."""
        digest = (
            manifest.get("model_digest")
            or manifest.get("model_sha256")
            or manifest.get("model_hash")
        )
        if not digest:
            return []
        return [{"alg": "SHA-256", "content": str(digest).removeprefix("sha256:")}]

    def _model_card_ref(
        self, model_name: str, model_version: str, ref: str
    ) -> list[dict[str, str]]:
        """CycloneDX externalReferences model-card entry (NF-056 R6)."""
        if ref == "auto":
            ver = model_version or "latest"
            url = f"registry://models/{model_name}/{ver}/card.md"
        else:
            url = ref
        return [{"type": "model-card", "url": url}]

    def _citations(
        self,
        digest: str | None,
        run_id: str,
        location_suffix: str,
        inclusion_proof: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """Verifiable-provenance citation binding a component to evidence (NF-056 R4)."""
        if not digest:
            return []
        citation: dict[str, Any] = {
            "content": digest,
            "location": f"capsule://{run_id}/{location_suffix}",
        }
        if inclusion_proof:
            citation["evidence"] = {
                "log": inclusion_proof.get("log", "novafabric-tlog"),
                "treeSize": inclusion_proof.get("tree_size")
                or inclusion_proof.get("treeSize"),
                "proof": inclusion_proof.get("proof") or inclusion_proof.get("proof_uri"),
            }
        return [citation]

    @staticmethod
    def validate(payload: dict[str, Any]) -> list[str]:
        """Structurally validate a CycloneDX 1.7 ML-BOM (NF-056 R1/R2/R5/R6).

        Returns a list of human-readable error strings; an empty list means the BOM
        passes the NovaFabric structural + binding checks. This is a dependency-free
        check of the required fields (not a full upstream-schema validation, which would
        require the optional ``cyclonedx-python-lib``).
        """
        errors: list[str] = []
        if payload.get("bomFormat") != "CycloneDX":
            errors.append("bomFormat must be 'CycloneDX'")
        if payload.get("specVersion") != "1.7":
            errors.append(f"specVersion must be '1.7' (got {payload.get('specVersion')!r})")
        serial = payload.get("serialNumber", "")
        if not isinstance(serial, str) or not serial.startswith("urn:uuid:"):
            errors.append("serialNumber must be a 'urn:uuid:' URN")

        # TLP marker validity (R5).
        for prop in payload.get("metadata", {}).get("properties", []):
            if prop.get("name") == "novafabric:tlp" and prop.get("value") not in TLP_MARKERS:
                errors.append(f"invalid novafabric:tlp marker {prop.get('value')!r}")

        components = payload.get("components", [])
        if not isinstance(components, list):
            errors.append("components must be a list")
            return errors

        for i, comp in enumerate(components):
            ref = comp.get("bom-ref") or f"#{i}"
            if not comp.get("name"):
                errors.append(f"component {ref}: missing name")
            if not comp.get("bom-ref"):
                errors.append(f"component {ref}: missing bom-ref")
            if comp.get("type") == "machine-learning-model":
                for h in comp.get("hashes", []):
                    if h.get("alg") not in {"SHA-256", "SHA-512", "SHA-384"}:
                        errors.append(f"component {ref}: unsupported hash alg {h.get('alg')!r}")
            for cite in comp.get("citations", []):
                if not cite.get("content"):
                    errors.append(f"component {ref}: citation missing 'content' digest")
        return errors

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
        if comp.hashes:
            out["hashes"] = comp.hashes
        if comp.licenses:
            out["licenses"] = comp.licenses
        if comp.external_references:
            out["externalReferences"] = comp.external_references
        if comp.properties:
            out["properties"] = comp.properties
        if comp.model_card:
            out["modelCard"] = comp.model_card
        if comp.citations:
            out["citations"] = comp.citations
        return out
