"""Every ``CapsuleSetBundleBuilder`` output must satisfy the bundle schema.

``subject`` is ``oneOf[Subject, array<Subject> minItems:2]``. A curated
one-capsule bundle used to write ``[Subject]`` -- matching neither arm -- while
``nova verify`` (which never reads ``subject``) passed, so the invalid manifest
shipped silently. ADR-0011 Am.1: one capsule is not a set, so it takes the
single-object form the schema already allows.
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.evidence.bundle import CapsuleSetBundleBuilder
from novafabric.evidence.signing import LocalSigner, generate_keypair

_REPO = Path(__file__).parents[2]
_SCHEMA = json.loads((_REPO / "src/novafabric/schemas/evidence-bundle.schema.json").read_text())
_ROOT_SCHEMA = json.loads((_REPO / "schemas/evidence-bundle.schema.json").read_text())


def _capsule(tmp_path: Path, name: str) -> Path:
    return (
        CaptureOrchestrator(base_dir=tmp_path / f"runs-{name}")
        .run(command=[sys.executable, "-c", "pass"])
        .capsule_dir
    )


def _manifest(zip_path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(zip_path) as zf:
        return json.loads(zf.read("manifest.json"))  # type: ignore[no-any-return]


def build(tmp_path: Path, n: int, curated: bool) -> Path:
    caps = [_capsule(tmp_path, f"m{i}") for i in range(n)]
    priv, _ = generate_keypair(tmp_path / "keys")
    out = tmp_path / "m.zip"
    curation = {"disclosure": "curated by test", "items": n} if curated else None
    CapsuleSetBundleBuilder(caps, LocalSigner(priv), out, curation=curation).build()
    return out


@pytest.mark.parametrize("curated", [False, True], ids=["plain", "curated"])
@pytest.mark.parametrize("n", [1, 2, 5])
def test_subject_validates_against_the_schema_for_every_set_size(
    tmp_path: Path, n: int, curated: bool
) -> None:
    import typer

    from novafabric.cli.verify import _verify_evidence_bundle

    out = build(tmp_path, n, curated)
    manifest = _manifest(out)
    jsonschema.validate(manifest, _SCHEMA)
    # The root v1 target schema shares the Subject definition; its other
    # differences (schema_version pattern) are out of scope here.
    jsonschema.validate(
        manifest["subject"],
        {**_ROOT_SCHEMA["properties"]["subject"], "$defs": _ROOT_SCHEMA["$defs"]},
    )
    if n == 1:
        assert isinstance(manifest["subject"], dict)
    else:
        assert isinstance(manifest["subject"], list) and len(manifest["subject"]) == n
    if curated:
        with zipfile.ZipFile(out) as zf:
            assert "curation.json" in zf.namelist()
    try:
        _verify_evidence_bundle(out)
    except typer.Exit as exc:  # pragma: no cover - failure path
        raise AssertionError(f"nova verify rejected the n={n} bundle: {exc}") from exc
