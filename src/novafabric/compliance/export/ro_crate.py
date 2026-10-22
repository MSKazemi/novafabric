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
"""RO-Crate v1.1 export for NovaFabric Run Capsules.

Produces a compliant RO-Crate v1.1 ZIP archive containing:
  - ro-crate-metadata.json  (JSON-LD, conformsTo 1.1)
  - Capsule files copied from the source capsule directory

Spec: https://www.researchobject.org/ro-crate/specification/1.1/
No external RO-Crate SDK is required — JSON-LD is hand-crafted.
"""

from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from novafabric import _capsule_fs
from novafabric._capsule_fs import read_capsule_file

_OPERATION = "an RO-Crate export"

# Well-known capsule file names that become File entities in the crate graph.
_CAPSULE_FILES = [
    "model-calls.jsonl",
    "lineage.jsonl",
    "evidence.json",
    "tool-calls.jsonl",
    "trace.jsonl",
    "assets.jsonl",
    "capsule.yaml",
    "redaction-proof.json",
]

# MIME type mapping for known extensions
_MIME_TYPES: dict[str, str] = {
    ".jsonl": "application/x-ndjson",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".tsr": "application/octet-stream",
    ".dsse": "application/json",
    ".zip": "application/zip",
}

_RO_CRATE_CONTEXT = "https://w3id.org/ro/crate/1.1/context"
_RO_CRATE_SPEC = "https://w3id.org/ro/crate/1.1"


def _mime_type(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return _MIME_TYPES.get(suffix, "application/octet-stream")


def _build_metadata(
    capsule_dir: Path,
    file_entities: list[dict[str, Any]],
    run_id: str,
) -> dict[str, Any]:
    """Build the ro-crate-metadata.json JSON-LD document."""
    now = datetime.now(timezone.utc).isoformat()

    # Root dataset entity (the capsule itself)
    root_dataset: dict[str, Any] = {
        "@id": "./",
        "@type": "Dataset",
        "name": f"NovaFabric Run Capsule {run_id}",
        "description": (
            f"Run capsule {run_id} exported from NovaFabric. "
            "Contains AI agent execution evidence for reproducibility and compliance."
        ),
        "datePublished": now,
        "conformsTo": {"@id": _RO_CRATE_SPEC},
        "hasPart": [{"@id": f["@id"]} for f in file_entities],
        "identifier": run_id,
        "creator": {
            "@type": "SoftwareApplication",
            "name": "NovaFabric",
            "url": "https://github.com/MSKazemi/novafabric",
        },
    }

    # Metadata file descriptor (required by spec)
    metadata_descriptor: dict[str, Any] = {
        "@id": "ro-crate-metadata.json",
        "@type": "CreativeWork",
        "about": {"@id": "./"},
        "conformsTo": {"@id": _RO_CRATE_SPEC},
        "dateCreated": now,
    }

    graph: list[dict[str, Any]] = [metadata_descriptor, root_dataset] + file_entities

    return {
        "@context": _RO_CRATE_CONTEXT,
        "@graph": graph,
    }


def export_ro_crate(capsule_dir: Path, output_path: Path) -> Path:
    """Export a Run Capsule as an RO-Crate v1.1 ZIP archive.

    Args:
        capsule_dir: Path to the source capsule directory.  Must exist.
        output_path: Destination ``.zip`` file path.  Parent directories
            are created automatically.

    Returns:
        ``output_path`` (the written ZIP file).

    Raises:
        FileNotFoundError: If ``capsule_dir`` does not exist.
        CapsuleLinkError: The capsule holds a symbolic link, including one
            planted during the export. No archive is written.
    """
    if not capsule_dir.exists():
        raise FileNotFoundError(
            f"Capsule directory not found: {capsule_dir}"
        )
    if not capsule_dir.is_dir():
        raise NotADirectoryError(
            f"Expected a directory, got: {capsule_dir}"
        )

    # Resolve run_id from the manifest, falling back to the directory name.
    #
    # This used to read `capsule_dir.name` alone. On a freshly captured capsule the
    # directory IS the run id, so the result was correct -- by coincidence. It stops
    # being correct the moment the directory is renamed, copied under another name,
    # or extracted from an archive, and `identifier` is precisely the RO-Crate field
    # that is supposed to survive relocation. Found by the F10 fidelity check, which
    # compares the exported document against the capsule's own manifest rather than
    # against a list of required keys (ADR-0256).
    run_id = capsule_dir.name
    manifest_path = capsule_dir / "capsule.yaml"
    if manifest_path.is_file() and not manifest_path.is_symlink():
        try:
            import yaml

            manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
            if isinstance(manifest, dict) and manifest.get("run_id"):
                run_id = str(manifest["run_id"])
        except Exception:  # noqa: BLE001 - a damaged manifest must not fail the export
            pass

    # Never follow a link: a planted ``notes.txt -> ~/.ssh/id_rsa`` used to be
    # zipped as a capsule file. Any link in the capsule refuses the export.
    listing = _capsule_fs.list_capsule(capsule_dir, operation=_OPERATION)

    # Top-level files plus the .seal/ (or seal/) subdirectory, as before.
    selected = [
        rel
        for rel in listing.files
        if "/" not in rel or (rel.count("/") == 1 and rel.split("/")[0] in (".seal", "seal"))
    ]
    payloads: dict[str, bytes] = {
        rel: read_capsule_file(listing.root, rel, operation=_OPERATION) for rel in selected
    }
    file_entities: list[dict[str, Any]] = [
        {
            "@id": rel,
            "@type": "File",
            "name": rel.rsplit("/", 1)[-1],
            "encodingFormat": _mime_type(rel),
            "contentSize": len(data),
        }
        for rel, data in payloads.items()
    ]

    # Build metadata
    metadata = _build_metadata(capsule_dir, file_entities, run_id)
    metadata_bytes = json.dumps(metadata, indent=2, ensure_ascii=False).encode("utf-8")

    # Write ZIP
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("ro-crate-metadata.json", metadata_bytes)
        for rel, data in payloads.items():
            zf.writestr(rel, data)

    return output_path
