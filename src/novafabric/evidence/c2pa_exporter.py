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
"""C2PA Content Credentials manifest exporter (ADR-0074).

Produces a C2PA v2.3-compatible provenance manifest as a JSON document
from a NovaFabric Run Capsule.  Satisfies EU AI Act Art.50 machine-readable
AI-generated-content disclosure requirement.

The manifest is structurally valid for TSP signing (``nova export-c2pa --sign``
is deferred to the TSP-enrollment phase).  No runtime dependencies beyond
stdlib are introduced.

Spec reference:
  https://c2pa.org/specifications/specifications/2.3/specs/C2PA_Specification.html
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

_CLAIM_GENERATOR = "NovaFabric"
_CLAIM_GENERATOR_VERSION = "0.32.0"


def _load_capsule_manifest(capsule_dir: Path) -> dict[str, Any]:
    manifest_path = capsule_dir / "capsule.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"capsule.yaml not found in {capsule_dir}. Is this a valid capsule directory?"
        )
    with manifest_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"capsule.yaml in {capsule_dir} did not parse as a dict")
    return data


def _extract_model_identity(capsule_dir: Path) -> tuple[str, str]:
    """Return (model_name, provider) from the first model-calls.jsonl record.

    Reads OTel GenAI semconv keys (``gen_ai.request.model`` /
    ``gen_ai.response.model`` / ``gen_ai.system``) written by the wire-level
    hooks, falling back to legacy ``model`` / ``provider`` keys for capsules
    produced by older versions. Returns ``("unknown", "unknown")`` when no
    model call is present.
    """
    model_calls_path = capsule_dir / "model-calls.jsonl"
    model_name = "unknown"
    provider = "unknown"
    if not model_calls_path.exists():
        return model_name, provider
    for raw in model_calls_path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            call = json.loads(raw)
        except json.JSONDecodeError:
            continue
        model_name = (
            call.get("gen_ai.request.model")
            or call.get("gen_ai.response.model")
            or call.get("model")
            or model_name
        )
        provider = (
            call.get("gen_ai.system")
            or call.get("provider")
            or provider
        )
        break
    return model_name, provider


class C2PAManifestExporter:
    """Builds a C2PA v2.3-compatible provenance manifest from a Run Capsule.

    The resulting document is a valid C2PA claim structure ready for optional
    TSP signing.  The ``c2pa.ai.generated`` assertion (value: ``true``) is the
    Art.50(1) machine-readable disclosure field.
    """

    def __init__(self, include_training_mining: bool = False) -> None:
        """Construct the exporter.

        Args:
            include_training_mining: When ``True``, adds a
                ``c2pa.training-mining`` assertion with ``use: notAllowed``.
                Opt-in to avoid inadvertent data-use-permission signals.
        """
        self._include_training_mining = include_training_mining

    def build_manifest(self, capsule_dir: Path) -> dict[str, Any]:
        """Build a C2PA manifest document from the capsule at *capsule_dir*.

        Args:
            capsule_dir: Path to the capsule directory containing
                ``capsule.yaml``.

        Returns:
            A ``dict`` structured as a C2PA v2.3 manifest store.  Write it
            as JSON with ``json.dumps(doc, indent=2)``.

        Raises:
            FileNotFoundError: If *capsule_dir* or ``capsule.yaml`` is missing.
            ValueError: If ``capsule.yaml`` cannot be parsed.
        """
        if not capsule_dir.exists():
            raise FileNotFoundError(f"Capsule directory not found: {capsule_dir}")

        meta = _load_capsule_manifest(capsule_dir)

        run_id: str = meta.get("run_id", capsule_dir.name)
        title: str = meta.get("command", [run_id])[0] if meta.get("command") else run_id

        # Model identity from the recorded model calls (OTel semconv keys, with
        # legacy fallback). This is the "synthetic content" producer.
        model_name, model_provider = _extract_model_identity(capsule_dir)

        manifest_id = f"urn:uuid:{uuid.uuid4()}"
        instance_id = f"xmp:iid:{uuid.uuid4()}"
        now_iso = datetime.now(tz=timezone.utc).isoformat()

        assertions: list[dict[str, Any]] = [
            {
                "label": "c2pa.ai.generated",
                "data": {"value": True},
            },
            {
                "label": "c2pa.metadata",
                "data": {
                    "dc:title": title,
                    "dc:format": "application/json",
                    "xmp:CreateDate": now_iso,
                },
            },
            {
                "label": "novafabric.run",
                "data": {
                    "run_id": run_id,
                    "model": model_name,
                    "provider": model_provider,
                    "created_at": meta.get("created_at", now_iso),
                    "status": meta.get("status", "unknown"),
                    "novafabric_version": meta.get("novafabric_version", "unknown"),
                },
            },
        ]

        # Check for NovaSeal signature reference
        seal_dir = capsule_dir / ".seal"
        if seal_dir.is_dir() and any(seal_dir.iterdir()):
            assertions.append({
                "label": "c2pa.digital-source-type",
                "data": {
                    "value": "trainedAlgorithmicMedia",
                    "novaseal_ref": str(seal_dir.relative_to(capsule_dir)),
                },
            })

        if self._include_training_mining:
            assertions.append({
                "label": "c2pa.training-mining",
                "data": {"use": "notAllowed"},
            })

        manifest_entry: dict[str, Any] = {
            "claim_generator": f"{_CLAIM_GENERATOR}/{_CLAIM_GENERATOR_VERSION}",
            "claim_generator_info": [
                {
                    "name": _CLAIM_GENERATOR,
                    "version": _CLAIM_GENERATOR_VERSION,
                }
            ],
            "title": title,
            "format": "application/json",
            "instance_id": instance_id,
            "assertions": assertions,
        }

        return {
            "manifests": {manifest_id: manifest_entry},
            "active_manifest": manifest_id,
        }

    def export(self, capsule_dir: Path, output_path: Path) -> Path:
        """Write the C2PA manifest JSON to *output_path*.

        Returns:
            *output_path* (the written file).
        """
        doc = self.build_manifest(capsule_dir)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return output_path


def export_c2pa(
    capsule_dir: Path,
    output_path: Path,
    *,
    include_training_mining: bool = False,
) -> Path:
    """Convenience wrapper — export C2PA manifest from *capsule_dir* to *output_path*.

    Args:
        capsule_dir: Path to the capsule directory.
        output_path: Destination ``.json`` file.
        include_training_mining: Include ``c2pa.training-mining: notAllowed``
            assertion (opt-in).

    Returns:
        *output_path* (the written file).
    """
    exporter = C2PAManifestExporter(include_training_mining=include_training_mining)
    return exporter.export(capsule_dir, output_path)
