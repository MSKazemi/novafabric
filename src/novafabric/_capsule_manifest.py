"""Read a capsule directory's manifest the way the seal binds it.

A sealed capsule's authoritative manifest is the payload of ``.seal/manifest.dsse``
(ADR-0251 §2): ``capsule.yaml`` is only a copy, compared with that payload at verify
time. Readers that also accept a ``capsule.json`` manifest — the query indexer, the
in-toto statement builder, ``nova serve``'s children route, the agent graph — read
whichever file they found first, sealed or not. An adversarial review (2026-10-23)
renamed a sealed capsule's ``capsule.yaml`` to ``capsule.json`` and edited it: those
readers served the forged ``status`` while ``nova verify`` exited 0.

:func:`read_capsule_manifest` is the one reader for them. When ``.seal/manifest.dsse``
exists, the manifest *is* its payload and no on-disk manifest is consulted, so a
substituted file cannot change what is served. The signature is not checked here —
:func:`novafabric.trust.novaseal.capsule_verify.verify_capsule` does that — so a forged
envelope reads as its own payload, exactly as an edited ``capsule.yaml`` on an unsealed
capsule does; what changed is that verification fails for both.

Nothing here follows a link or reads without a size bound.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from novafabric._capsule_fs import CapsuleEntryError, read_capsule_file

__all__ = [
    "MANIFEST_NAMES",
    "SEALED_ENVELOPE",
    "SEAL_MANIFEST_NAME_KEY",
    "CapsuleManifest",
    "CapsuleManifestError",
    "manifest_view",
    "parse_manifest_bytes",
    "read_capsule_manifest",
    "sealed_manifest_name",
    "signed_payload",
]

#: Every file name a NovaFabric reader accepts as a capsule manifest, in the order
#: readers prefer them. A manifest-named file the seal does not cover fails
#: verification (ADR-0251 amendment, 2026-10-23).
MANIFEST_NAMES: tuple[str, ...] = ("capsule.yaml", "capsule.json")

#: The signed envelope whose payload is a sealed capsule's manifest.
SEALED_ENVELOPE = ".seal/manifest.dsse"

#: Member of the reserved ``_novaseal`` payload block naming the manifest file the
#: capsule was sealed with (written by ``capture.finalize.seal_capsule``).
SEAL_MANIFEST_NAME_KEY = "manifest"

_MAX_ENVELOPE_BYTES = 16 * 1024 * 1024
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024


class CapsuleManifestError(ValueError):
    """A manifest (sealed or on disk) is present but cannot be used.

    Raised for a linked, special, oversized, undecodable or non-mapping manifest —
    never for an absent one. ``rel`` is the capsule-relative file at fault.
    """

    def __init__(self, message: str, *, rel: str) -> None:
        super().__init__(message)
        self.rel = rel


@dataclass(frozen=True)
class CapsuleManifest:
    """A capsule manifest and where it was read from.

    ``source`` is :data:`SEALED_ENVELOPE` for a sealed capsule, else the manifest
    file name (``capsule.yaml`` / ``capsule.json``).
    """

    data: dict[str, Any]
    source: str

    @property
    def sealed(self) -> bool:
        """True when the manifest is the signed payload, not an on-disk copy."""
        return self.source == SEALED_ENVELOPE


def parse_manifest_bytes(name: str, raw: bytes) -> object:
    """Parse manifest file *name*'s bytes: JSON for ``*.json``, YAML otherwise."""
    text = raw.decode("utf-8")
    return json.loads(text) if name.endswith(".json") else yaml.safe_load(text)


def signed_payload(dsse_bytes: bytes) -> dict[str, Any]:
    """The decoded JSON payload of a DSSE envelope, which must be an object.

    Raises:
        CapsuleManifestError: the envelope or its payload cannot be decoded, or the
            payload is not a JSON object.
    """
    from novafabric.trust.novaseal.envelope import extract_payload  # noqa: PLC0415

    try:
        payload = json.loads(extract_payload(dsse_bytes))
    except Exception as exc:  # noqa: BLE001 — any decode failure is one error kind
        raise CapsuleManifestError(
            f"cannot decode the signed payload: {exc}", rel=SEALED_ENVELOPE
        ) from exc
    if not isinstance(payload, dict):
        raise CapsuleManifestError(
            "the signed payload is not a manifest object", rel=SEALED_ENVELOPE
        )
    return payload


def manifest_view(payload: dict[str, Any]) -> dict[str, Any]:
    """*payload* without the reserved seal-metadata block (never part of a manifest)."""
    from novafabric.trust.novaseal.envelope import SEAL_METADATA_KEY  # noqa: PLC0415

    return {k: v for k, v in payload.items() if k != SEAL_METADATA_KEY}


def sealed_manifest_name(payload: dict[str, Any]) -> str | None:
    """The manifest file the seal was made over, or None when it cannot be known.

    A payload that records it (``_novaseal.manifest``) names it. Before that was
    recorded, a payload carrying ``evidence_digests`` was sealed by the shared
    finalize path, which writes ``capsule.yaml`` and leaves exactly that file out of
    the digest map because it carries the map (ADR-0251 §1) — so it names
    ``capsule.yaml``. Anything else (a pre-ADR-0251 seal, the object store's
    one-field manifest) names no file.
    """
    from novafabric.trust.novaseal.envelope import SEAL_METADATA_KEY  # noqa: PLC0415

    meta = payload.get(SEAL_METADATA_KEY)
    recorded = meta.get(SEAL_MANIFEST_NAME_KEY) if isinstance(meta, dict) else None
    if isinstance(recorded, str):
        return recorded
    if isinstance(payload.get("evidence_digests"), dict):
        return MANIFEST_NAMES[0]
    return None


def _entry_exists(capsule_dir: Path, rel: str) -> bool:
    path = capsule_dir / rel
    return path.exists() or path.is_symlink()


def _read_sealed(capsule_dir: Path, operation: str) -> CapsuleManifest:
    try:
        raw = read_capsule_file(
            capsule_dir, SEALED_ENVELOPE, operation=operation, max_bytes=_MAX_ENVELOPE_BYTES
        )
    except (OSError, CapsuleEntryError) as exc:
        raise CapsuleManifestError(
            f"{SEALED_ENVELOPE} not read: {exc}", rel=SEALED_ENVELOPE
        ) from exc
    return CapsuleManifest(manifest_view(signed_payload(raw)), SEALED_ENVELOPE)


def _read_on_disk(capsule_dir: Path, name: str, operation: str) -> CapsuleManifest:
    try:
        raw = read_capsule_file(
            capsule_dir, name, operation=operation, max_bytes=_MAX_MANIFEST_BYTES
        )
        data = parse_manifest_bytes(name, raw)
    except (OSError, CapsuleEntryError, ValueError, yaml.YAMLError) as exc:
        raise CapsuleManifestError(f"{name} not read: {exc}", rel=name) from exc
    if not isinstance(data, dict):
        raise CapsuleManifestError(f"{name} is not a mapping", rel=name)
    return CapsuleManifest(data, name)


def read_capsule_manifest(
    capsule_dir: Path, *, operation: str = "this operation"
) -> CapsuleManifest | None:
    """The manifest of *capsule_dir*: the signed payload when sealed, else on disk.

    Returns None when the directory has neither ``.seal/manifest.dsse`` nor a
    manifest file. A sealed capsule never falls back to an on-disk manifest, even
    when its envelope is unusable: that raises instead.

    Raises:
        CapsuleManifestError: the manifest that applies is present but unusable.
    """
    if _entry_exists(capsule_dir, SEALED_ENVELOPE):
        return _read_sealed(capsule_dir, operation)
    for name in MANIFEST_NAMES:
        if _entry_exists(capsule_dir, name):
            return _read_on_disk(capsule_dir, name, operation)
    return None
