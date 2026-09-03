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

"""C2PA / Content-Credentials hard binding to a captured media blob (ADR-0148 D1).

NF-161 binds a provenance manifest to the exact ``content_hash`` of an ADR-0125
``MediaPart`` — not a manifest sitting loosely beside a file, but a **hard binding**:
the hash the manifest itself claims must equal the hash of the bytes NovaFabric holds.
NF-163 does the same for media the agent *produced*, and cross-links the run-level
NF-094 Art. 50 receipt (``compliance/export/art50_marking.py``) by digest, so the
per-artifact receipt and the single run receipt are provably the same evidence.

**What NovaFabric is claiming, and what it is not.** A written entry says: *this
manifest, with this digest, claims this hash; the blob we hold hashes to this; here is
whether those two agree.* It does not say the content is authentic, unmanipulated,
watermarked, or Art. 50-compliant (ADR-0148 I-4). Every one of those is a claim about
the world; this module only ever reports a comparison it actually performed.

Four boundaries shape every field below.

**⚠ ``cert_chain_ok`` is never inferred from a signature being present.** A manifest
carrying ``signature_info`` looks verified and is not: verifying an X.509 chain needs a
trust store and a chain verifier, and NovaFabric has neither offline and adds no
dependency for one (ADR-0024). So the verdict stays ``None`` and
:attr:`BindingVerdict.cert_chain_reason` says why in a machine-readable token. A
fabricated ``true`` here would be worse than the gap it papers over — it would put a
verification NovaFabric never performed into signed evidence. (Same defect class as the
NF-171 ``signature_ok`` field.)

**⚠ A binding checked against stored bytes is stronger evidence than one checked
against a recorded field.** Byte capture is opt-in (ADR-0125 D2), so most capsules hold
a ``content_hash`` and no blob. Comparing the manifest's claim to that recorded hash is
still worth doing — it catches a manifest bound to different content — but it inherits
whatever the capture path recorded, whereas re-hashing the blob is independent of it.
The two are not the same evidence, so they never serialise the same way:
:attr:`MediaProvenanceEntry.bound_against` says which one was used.

**Absent is not false.** A media part with no manifest produces **no entry** — never an
entry with ``hard_binding_ok: false``. A false binding means *we compared and they
disagreed*; no entry means *we found nothing to compare*. Collapsing those two would
turn every uncaptured manifest into a provenance failure.

**Fail-open (ADR-0148 I-3).** No manifests ⇒ :func:`build_facet` returns ``None`` and
the capsule is byte-identical to one captured before this feature existed. Malformed
manifest documents are skipped, never raised — this is a reader, not a validator.

**Where manifests come from.** Manifests are discovered as JSON sidecars next to the
capsule's media (see :func:`discover_sidecar_manifests`). Extracting an *embedded*
JUMBF manifest from image bytes is **not implemented** and needs a C2PA library; a
capsule whose media carries only embedded manifests therefore yields no entries, and
:attr:`MediaProvenanceFacet.media_parts_scanned` is what keeps that legible as "nothing
was found" rather than "nothing was there".
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from novafabric.capture.media import iter_media_parts

SCHEMA_VERSION = "0.1.0"
FACET_NAME = "media_provenance"

#: Filename suffix of a sidecar provenance manifest, e.g. ``outputs/<sha256>.c2pa.json``.
SIDECAR_SUFFIX = ".c2pa.json"

#: C2PA assertion label carrying the hard binding — the hash of the asset the manifest
#: is about. Fixed by the C2PA specification; NovaFabric reads it, never mints it.
HARD_BINDING_LABEL = "c2pa.hash.data"

#: Reason ``cert_chain_ok`` is ``None``. A token, not prose, so a consumer can branch on
#: it and a future verifier can introduce a second reason without breaking readers.
NO_CERT_VERIFIER = "no_offline_cert_chain_verifier"

_CONTENT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

Direction = Literal["input", "output"]
ManifestKind = Literal["c2pa", "content_credentials"]
BoundAgainst = Literal["blob_bytes", "recorded_hash"]


def _sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    """Digest of *manifest* over its canonical JSON form.

    Canonical means sorted keys and compact separators, so two byte-different
    serialisations of the same manifest produce the same digest — the digest identifies
    the *manifest*, not one file's whitespace.
    """
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return f"sha256:{_sha256_hex(canonical.encode('utf-8'))}"


class ManifestSigner(BaseModel):
    """Public identity of a manifest's signer. Never a key, never a secret."""

    model_config = ConfigDict(extra="forbid")

    subject: str | None = None
    cert_fingerprint: str | None = None


class BindingVerdict(BaseModel):
    """The three checks NF-161 requires, each reporting only what was performed."""

    model_config = ConfigDict(extra="forbid")

    active_manifest_ok: bool = Field(
        description="The document names an active manifest and that manifest resolves."
    )
    hard_binding_ok: bool | None = Field(
        default=None,
        description=(
            "The hash the manifest claims equals the hash NovaFabric holds. ``None`` "
            "when the manifest declares no hard binding — that is a missing claim, not "
            "a failed one."
        ),
    )
    cert_chain_ok: bool | None = Field(
        default=None,
        description=(
            "Always ``None``: no offline X.509 chain verifier ships here. Never "
            "inferred from a signature being present."
        ),
    )
    cert_chain_reason: str | None = Field(
        default=NO_CERT_VERIFIER,
        description="Why ``cert_chain_ok`` is unknown. Machine-readable token.",
    )


class MediaProvenanceEntry(BaseModel):
    """One manifest bound to one media blob."""

    model_config = ConfigDict(extra="forbid")

    bound_content_hash: str
    direction: Direction
    manifest_kind: ManifestKind
    manifest_digest: str
    bound_against: BoundAgainst = Field(
        description=(
            "``blob_bytes`` when the comparison re-hashed the stored blob, "
            "``recorded_hash`` when it used the capsule's recorded ``content_hash``. "
            "The first is independent evidence; the second inherits the capture path."
        )
    )
    signer: ManifestSigner | None = None
    verified: BindingVerdict
    manifest_bytes_captured: bool = Field(
        default=False,
        description=(
            "Manifest bytes are retained only under the ADR-0125 opt-in; otherwise the "
            "digest above is the whole record of them."
        ),
    )
    # NF-163 — output-direction only.
    producing_model: str | None = None
    producing_run_id: str | None = None
    art50_marking_claimed: bool | None = None
    nf094_receipt_digest: str | None = None


class MediaProvenanceFacet(BaseModel):
    """``facets.media_provenance`` — additive, optional, absent when empty."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    entries: list[MediaProvenanceEntry] = Field(default_factory=list)
    media_parts_scanned: int = Field(
        default=0,
        description=(
            "How many MediaParts were looked at. Travels with the answer: an empty "
            "entry list from 0 parts and from 300 are different claims."
        ),
    )
    manifests_found: int = Field(
        default=0, description="How many provenance manifests were discovered."
    )


class ParsedManifest(BaseModel):
    """What NovaFabric can read out of a provenance manifest document."""

    model_config = ConfigDict(extra="forbid")

    kind: ManifestKind
    digest: str
    active_manifest_ok: bool
    claimed_hash: str | None = Field(
        default=None,
        description="The hash the manifest's hard-binding assertion claims, if any.",
    )
    signer: ManifestSigner | None = None
    document: dict[str, Any] = Field(default_factory=dict, exclude=True)


def normalise_content_hash(value: Any) -> str | None:
    """Accept ``sha256:<hex>`` or a bare 64-char hex digest; reject anything else."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", candidate):
        candidate = f"sha256:{candidate}"
    return candidate if _CONTENT_HASH_RE.match(candidate) else None


def _extract_signer(entry: Mapping[str, Any]) -> ManifestSigner | None:
    info = entry.get("signature_info")
    if not isinstance(info, Mapping):
        return None
    subject = info.get("issuer") or info.get("subject")
    fingerprint = normalise_content_hash(info.get("cert_fingerprint"))
    if subject is None and fingerprint is None:
        return None
    return ManifestSigner(
        subject=str(subject) if subject is not None else None,
        cert_fingerprint=fingerprint,
    )


def parse_manifest(document: Any) -> ParsedManifest | None:
    """Parse a C2PA / Content-Credentials manifest store.

    Reads the ``{"active_manifest": <id>, "manifests": {<id>: {...}}}`` shape that
    ``evidence/c2pa_exporter.py`` emits and ``compliance/export/art50_marking.py``
    already reads — the same shape, not a second one.

    Returns ``None`` for anything that is not a manifest store at all. A store whose
    ``active_manifest`` does not resolve is **not** ``None``: it parses with
    ``active_manifest_ok=False``, because "we read it and its active manifest is
    dangling" is a finding, while ``None`` means "this was not a manifest".
    """
    if not isinstance(document, Mapping):
        return None
    manifests = document.get("manifests")
    if not isinstance(manifests, Mapping):
        return None

    kind: ManifestKind = (
        "content_credentials"
        if str(document.get("manifest_kind", "")).lower() == "content_credentials"
        else "c2pa"
    )
    active_id = document.get("active_manifest")
    entry = manifests.get(active_id) if isinstance(active_id, str) else None
    active_ok = isinstance(entry, Mapping)

    claimed: str | None = None
    signer: ManifestSigner | None = None
    if isinstance(entry, Mapping):
        signer = _extract_signer(entry)
        assertions = entry.get("assertions")
        if isinstance(assertions, list):
            for assertion in assertions:
                if not isinstance(assertion, Mapping):
                    continue
                if assertion.get("label") != HARD_BINDING_LABEL:
                    continue
                data = assertion.get("data")
                if isinstance(data, Mapping):
                    claimed = normalise_content_hash(data.get("hash"))
                if claimed is not None:
                    break

    return ParsedManifest(
        kind=kind,
        digest=manifest_digest(document),
        active_manifest_ok=active_ok,
        claimed_hash=claimed,
        signer=signer,
        document=dict(document),
    )


def bind_manifest(
    *,
    content_hash: str,
    document: Any,
    direction: Direction = "input",
    blob_bytes: bytes | None = None,
    capture_manifest_bytes: bool = False,
    producing_model: str | None = None,
    producing_run_id: str | None = None,
    art50_marking_claimed: bool | None = None,
    nf094_receipt_digest: str | None = None,
) -> MediaProvenanceEntry | None:
    """Bind one manifest to one media blob, or return ``None`` if it is not a manifest.

    When *blob_bytes* is supplied the hard binding is checked by re-hashing those bytes
    — evidence independent of what the capture path recorded. Otherwise it is checked
    against *content_hash*, and ``bound_against`` records the weaker basis.

    A manifest that declares no hard binding yields ``hard_binding_ok=None``, not
    ``False``: a missing claim and a broken claim are different facts.
    """
    parsed = parse_manifest(document)
    if parsed is None:
        return None

    recorded = normalise_content_hash(content_hash)
    if recorded is None:
        return None

    if blob_bytes is not None:
        observed = f"sha256:{_sha256_hex(blob_bytes)}"
        bound_against: BoundAgainst = "blob_bytes"
    else:
        observed = recorded
        bound_against = "recorded_hash"

    hard_ok: bool | None = None
    if parsed.claimed_hash is not None:
        hard_ok = parsed.claimed_hash == observed

    return MediaProvenanceEntry(
        bound_content_hash=recorded,
        direction=direction,
        manifest_kind=parsed.kind,
        manifest_digest=parsed.digest,
        bound_against=bound_against,
        signer=parsed.signer,
        verified=BindingVerdict(
            active_manifest_ok=parsed.active_manifest_ok,
            hard_binding_ok=hard_ok,
            cert_chain_ok=None,
            cert_chain_reason=NO_CERT_VERIFIER,
        ),
        manifest_bytes_captured=bool(capture_manifest_bytes),
        producing_model=producing_model if direction == "output" else None,
        producing_run_id=producing_run_id if direction == "output" else None,
        art50_marking_claimed=(
            art50_marking_claimed if direction == "output" else None
        ),
        nf094_receipt_digest=nf094_receipt_digest if direction == "output" else None,
    )


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def sidecar_path_for(capsule_dir: Path, content_hash: str) -> Path:
    """Where a sidecar manifest for *content_hash* is looked for."""
    normalised = normalise_content_hash(content_hash) or content_hash
    return capsule_dir / "outputs" / f"{normalised.split(':', 1)[-1]}{SIDECAR_SUFFIX}"


def discover_sidecar_manifests(capsule_dir: Path) -> dict[str, Any]:
    """Map ``content_hash`` to the manifest document sitting beside its blob.

    A sidecar is ``outputs/<sha256-hex>.c2pa.json``. Unparseable files are skipped —
    a broken sidecar must not stop the readable ones from binding.
    """
    found: dict[str, Any] = {}
    outputs = capsule_dir / "outputs"
    if not outputs.is_dir():
        return found
    for path in sorted(outputs.glob(f"*{SIDECAR_SUFFIX}")):
        stem = path.name[: -len(SIDECAR_SUFFIX)]
        content_hash = normalise_content_hash(stem)
        if content_hash is None:
            continue
        document = _read_json(path)
        if document is not None:
            found[content_hash] = document
    return found


def _blob_bytes_for(capsule_dir: Path, media: Mapping[str, Any]) -> bytes | None:
    blob_ref = media.get("blob_ref")
    if not isinstance(blob_ref, str):
        return None
    path = capsule_dir / blob_ref
    try:
        return path.read_bytes()
    except OSError:
        return None


def build_facet(
    capsule_dir: Path,
    *,
    manifests: Mapping[str, Any] | None = None,
    output_hashes: Iterable[str] = (),
    producing_model: str | None = None,
    producing_run_id: str | None = None,
    art50_marking_claimed: bool | None = None,
    nf094_receipt_digest: str | None = None,
    capture_manifest_bytes: bool = False,
) -> MediaProvenanceFacet | None:
    """Bind every discoverable manifest in *capsule_dir* to its media part.

    *manifests* overrides sidecar discovery (used by the CLI's ``--manifest``). Hashes
    listed in *output_hashes* are bound as ``direction: output`` and carry the NF-163
    producing-model / NF-094 cross-link fields.

    Returns ``None`` when nothing bound — an empty facet in a sealed capsule would read
    as "we looked and there is no provenance", a claim this function cannot make for a
    capsule whose manifests may simply be embedded rather than sidecarred.
    """
    discovered = dict(manifests) if manifests is not None else (
        discover_sidecar_manifests(capsule_dir)
    )
    outputs = {h for h in (normalise_content_hash(x) for x in output_hashes) if h is not None}

    entries: list[MediaProvenanceEntry] = []
    seen: set[tuple[str, str]] = set()
    scanned = 0
    for _call_id, media in iter_media_parts(capsule_dir):
        scanned += 1
        content_hash = normalise_content_hash(media.get("content_hash"))
        if content_hash is None or content_hash not in discovered:
            continue
        direction: Direction = "output" if content_hash in outputs else "input"
        entry = bind_manifest(
            content_hash=content_hash,
            document=discovered[content_hash],
            direction=direction,
            blob_bytes=_blob_bytes_for(capsule_dir, media),
            capture_manifest_bytes=capture_manifest_bytes,
            producing_model=producing_model,
            producing_run_id=producing_run_id,
            art50_marking_claimed=art50_marking_claimed,
            nf094_receipt_digest=nf094_receipt_digest,
        )
        if entry is None:
            continue
        key = (entry.bound_content_hash, entry.direction)
        if key in seen:
            continue
        seen.add(key)
        entries.append(entry)

    if not entries:
        return None
    return MediaProvenanceFacet(
        entries=entries,
        media_parts_scanned=scanned,
        manifests_found=len(discovered),
    )


def attach_facet(
    capsule: dict[str, Any], facet: MediaProvenanceFacet
) -> dict[str, Any]:
    """Attach *facet* under ``facets.media_provenance``, mutating and returning *capsule*."""
    facets = capsule.setdefault("facets", {})
    if not isinstance(facets, dict):  # pragma: no cover - defensive
        raise TypeError("capsule 'facets' must be a mapping")
    facets[FACET_NAME] = facet.model_dump(exclude_none=True)
    return capsule


def facet_from_capsule(capsule: Mapping[str, Any]) -> MediaProvenanceFacet | None:
    """Read the facet back, or ``None`` when the capsule carries none."""
    facets = capsule.get("facets")
    if not isinstance(facets, Mapping):
        return None
    body = facets.get(FACET_NAME)
    if not isinstance(body, Mapping):
        return None
    try:
        return MediaProvenanceFacet.model_validate(dict(body))
    except ValueError:
        return None


def output_entries(facet: MediaProvenanceFacet) -> list[MediaProvenanceEntry]:
    """The NF-163 output-direction entries — the per-artifact Art. 50 receipts."""
    return [e for e in facet.entries if e.direction == "output"]


def unverified_bindings(facet: MediaProvenanceFacet) -> list[MediaProvenanceEntry]:
    """Entries whose hard binding failed, or which declared none.

    Both are returned because both mean *the binding is not established* — but the
    caller can still tell them apart on ``verified.hard_binding_ok`` (``False`` vs
    ``None``), which is why they are not collapsed into a boolean here.
    """
    return [e for e in facet.entries if e.verified.hard_binding_ok is not True]
