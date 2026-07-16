"""Multi-modal capture — content-addressed media on model calls (ADR-0125).

When a model call's messages carry inline media (base64 image/audio/video/
document blocks), the capture layer replaces the inline bytes with a
content-addressed **MediaPart** reference block (``design/spec/
multimodal-capture-v0.md``): the part records ``media_type``, a
``sha256:<hex>`` ``content_hash`` over the raw decoded bytes, ``byte_size``,
``redacted``, and an optional ``blob_ref`` into the capsule's existing
``outputs/`` content-addressed blob store.

Contract (ADR-0125 D1–D4):

- **Never inlined bytes on the record.** A recognized inline-base64 media part
  is always rewritten to a reference; the base64 payload never lands in
  ``model-calls.jsonl``.
- **Byte capture is opt-in** (ADR-0021 §4 privacy-by-default). By default only
  reference metadata is recorded (``blob_ref: null``); the bytes are hashed at
  the boundary and discarded. ``nova capture --capture-media`` (env
  ``NOVAFABRIC_CAPTURE_MEDIA=1``) enables byte storage.
- **Bounded.** A per-part size cap (default 10 MiB; ``NOVAFABRIC_MEDIA_MAX_BYTES``
  override) degrades an oversized part to reference-only — base64 above the cap
  is never inlined and never stored.
- **Dedup by hash.** Identical bytes across parts/messages/calls share one blob
  under ``outputs/<hex>.<ext>``.
- **Fail-open.** Any error in the media path leaves the part untouched and
  never blocks the captured workload.

Parts that reference media by URL (no inline bytes at the boundary) are left
untouched — NovaFabric never fetches content just to hash it (spec §Edge cases).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterator

#: Opt-in gate for byte capture (ADR-0125 D2). Set by the orchestrator when
#: ``nova capture --capture-media`` is passed; read in the workload subprocess
#: where the hooks (and therefore the CapsuleWriter funnel) run.
MEDIA_CAPTURE_ENV = "NOVAFABRIC_CAPTURE_MEDIA"

#: Per-part size cap override (bytes). Bounded capture per ADR-0125 D4.
MEDIA_MAX_BYTES_ENV = "NOVAFABRIC_MEDIA_MAX_BYTES"

#: Default per-part cap: 10 MiB. Content above the cap is recorded
#: reference-only (hash + size, no bytes) — an allowed D4 outcome.
DEFAULT_MEDIA_MAX_BYTES = 10 * 1024 * 1024

#: Canonical MediaPart type domain (model-call-v1 ContentPart media kinds).
MEDIA_TYPES: tuple[str, ...] = ("image", "audio", "video", "document")

_ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
_CONTENT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_DATA_URL_RE = re.compile(r"^data:([a-z]+/[A-Za-z0-9.+-]+);base64,(.*)$", re.DOTALL)

#: media_type → blob filename extension (fallback: ``bin``).
_EXT_BY_MEDIA_TYPE: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/ogg": "ogg",
    "audio/flac": "flac",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "application/pdf": "pdf",
}

#: OpenAI ``input_audio.format`` → IANA media type.
_AUDIO_FORMAT_MEDIA_TYPE: dict[str, str] = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
    "pcm16": "audio/L16",
}

_MEDIA_TYPE_RE = re.compile(r"^[a-z]+/[A-Za-z0-9.+-]+$")


def media_capture_enabled() -> bool:
    """True when byte capture is opted in via ``NOVAFABRIC_CAPTURE_MEDIA=1``."""
    return os.environ.get(MEDIA_CAPTURE_ENV) == "1"


def media_max_bytes() -> int:
    """Per-part size cap in bytes (bounded capture, ADR-0125 D4)."""
    raw = os.environ.get(MEDIA_MAX_BYTES_ENV)
    if raw is not None:
        try:
            value = int(raw)
            if value >= 0:
                return value
        except ValueError:
            pass
    return DEFAULT_MEDIA_MAX_BYTES


def blob_ext_for(media_type: str) -> str:
    """Blob filename extension for an IANA media type (fallback ``bin``)."""
    return _EXT_BY_MEDIA_TYPE.get(media_type, "bin")


def _b64decode(data: str) -> bytes | None:
    """Strictly decode base64; ``None`` when the payload is not valid base64."""
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return None


def _extract_inline_media(part: dict[str, Any]) -> tuple[str, str, bytes] | None:
    """Return ``(type, media_type, raw_bytes)`` for a recognized inline part.

    Recognized shapes (all carry the bytes inline as base64):

    - Anthropic / canonical model-call-v1: ``{"type": "image|audio|video|
      document", "source": {"type"|"kind": "base64"|"inline", "media_type": …,
      "data": …}}``
    - OpenAI vision: ``{"type": "image_url", "image_url": {"url": "data:…"}}``
      (or ``"image_url"`` as a bare data-URL string)
    - OpenAI audio-in: ``{"type": "input_audio", "input_audio":
      {"data": …, "format": "wav"|"mp3"|…}}``

    URL-referenced parts (no inline bytes) return ``None`` — never fetched.
    """
    ptype = part.get("type")

    if ptype in MEDIA_TYPES:
        source = part.get("source")
        if isinstance(source, dict) and isinstance(source.get("data"), str):
            kind = source.get("type") or source.get("kind")
            media_type = source.get("media_type")
            if kind in ("base64", "inline") and isinstance(media_type, str):
                if _MEDIA_TYPE_RE.match(media_type):
                    raw = _b64decode(source["data"])
                    if raw is not None:
                        return str(ptype), media_type, raw
        return None

    if ptype == "image_url":
        image_url = part.get("image_url")
        url = image_url.get("url") if isinstance(image_url, dict) else image_url
        if isinstance(url, str):
            match = _DATA_URL_RE.match(url)
            if match is not None:
                raw = _b64decode(match.group(2))
                if raw is not None:
                    return "image", match.group(1), raw
        return None

    if ptype == "input_audio":
        input_audio = part.get("input_audio")
        if isinstance(input_audio, dict) and isinstance(input_audio.get("data"), str):
            media_type = _AUDIO_FORMAT_MEDIA_TYPE.get(str(input_audio.get("format")))
            if media_type is not None:
                raw = _b64decode(input_audio["data"])
                if raw is not None:
                    return "audio", media_type, raw
        return None

    return None


def _store_blob(
    capsule_dir: Path, hex_digest: str, raw: bytes, media_type: str
) -> str | None:
    """Write *raw* content-addressed under ``outputs/`` (dedup; atomic).

    Returns the ``outputs/<hex>.<ext>`` blob ref, or ``None`` when storage
    fails (the part degrades to reference-only — capture never blocks).
    """
    rel = f"outputs/{hex_digest}.{blob_ext_for(media_type)}"
    path = capsule_dir / rel
    try:
        if path.exists():
            return rel  # dedup: identical bytes already stored once
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, path)
        return rel
    except OSError:
        return None


def _media_reference_part(
    kind: str,
    media_type: str,
    raw: bytes,
    capsule_dir: Path,
    capture_bytes: bool,
) -> dict[str, Any]:
    """Build the normalized ContentPart carrying a MediaPart reference block."""
    hex_digest = hashlib.sha256(raw).hexdigest()
    blob_ref: str | None = None
    if capture_bytes and len(raw) <= media_max_bytes():
        blob_ref = _store_blob(capsule_dir, hex_digest, raw, media_type)
    return {
        "type": kind,
        "media": {
            "type": kind,
            "media_type": media_type,
            "content_hash": f"sha256:{hex_digest}",
            "byte_size": len(raw),
            "redacted": False,
            "blob_ref": blob_ref,
            "source_hint": {"kind": "inline"},
        },
    }


def _process_content(
    content: Any, capsule_dir: Path, capture_bytes: bool
) -> list[Any] | None:
    """Rewrite inline media parts in a message content list.

    Returns a new list when at least one part was rewritten; ``None`` when
    nothing changed (so absent media leaves the record byte-identical).
    Never mutates the input (parts may be caller-owned SDK kwargs).
    """
    if not isinstance(content, list):
        return None
    changed = False
    out: list[Any] = []
    for part in content:
        if isinstance(part, dict):
            try:
                found = _extract_inline_media(part)
            except Exception:  # noqa: BLE001 — fail-open per part
                found = None
            if found is not None:
                kind, media_type, raw = found
                try:
                    part = _media_reference_part(
                        kind, media_type, raw, capsule_dir, capture_bytes
                    )
                    changed = True
                except Exception:  # noqa: BLE001 — leave the part untouched
                    pass
        out.append(part)
    return out if changed else None


def _process_messages(
    messages: Any, capsule_dir: Path, capture_bytes: bool
) -> list[Any] | None:
    """Rewrite inline media across a message list; ``None`` when unchanged."""
    if not isinstance(messages, list):
        return None
    changed = False
    out: list[Any] = []
    for message in messages:
        if isinstance(message, dict):
            new_content = _process_content(
                message.get("content"), capsule_dir, capture_bytes
            )
            if new_content is not None:
                message = {**message, "content": new_content}
                changed = True
        out.append(message)
    return out if changed else None


def annotate_model_call_media(
    record: dict[str, Any],
    capsule_dir: Path,
    capture_bytes: bool | None = None,
) -> None:
    """Rewrite inline media in a model-call record to MediaPart references.

    Walks ``gen_ai.request.messages`` and ``gen_ai.response.choices[].message``.
    *capture_bytes* defaults to the ``NOVAFABRIC_CAPTURE_MEDIA`` opt-in.
    A record with no inline media is left **byte-identical**. Never raises
    into the workload (per-part fail-open; the caller additionally guards).
    """
    if capture_bytes is None:
        capture_bytes = media_capture_enabled()

    new_messages = _process_messages(
        record.get("gen_ai.request.messages"), capsule_dir, capture_bytes
    )
    if new_messages is not None:
        record["gen_ai.request.messages"] = new_messages

    choices = record.get("gen_ai.response.choices")
    if isinstance(choices, list):
        for i, choice in enumerate(choices):
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if not isinstance(message, dict):
                continue
            new_content = _process_content(
                message.get("content"), capsule_dir, capture_bytes
            )
            if new_content is not None:
                choices[i] = {
                    **choice,
                    "message": {**message, "content": new_content},
                }


def _iter_record_media(record: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield every MediaPart block embedded in one model-call record."""
    contents: list[Any] = []
    messages = record.get("gen_ai.request.messages")
    if isinstance(messages, list):
        contents.extend(
            m.get("content") for m in messages if isinstance(m, dict)
        )
    choices = record.get("gen_ai.response.choices")
    if isinstance(choices, list):
        for choice in choices:
            if isinstance(choice, dict) and isinstance(choice.get("message"), dict):
                contents.append(choice["message"].get("content"))
    for content in contents:
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("media"), dict):
                yield part["media"]


def iter_media_parts(capsule_dir: Path) -> Iterator[tuple[str | None, dict[str, Any]]]:
    """Yield ``(model_call_id, media_block)`` for every MediaPart in a capsule.

    Read surface over ``model-calls.jsonl``; tolerant of malformed lines
    (skipped — this is a reader, not a validator).
    """
    path = capsule_dir / "model-calls.jsonl"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        call_id = record.get("model_call_id")
        call_id = call_id if isinstance(call_id, str) else None
        for media in _iter_record_media(record):
            yield call_id, media


def collect_media_artifacts(capsule_dir: Path) -> list[dict[str, Any]]:
    """Manifest ``Artifact`` entries for every stored media blob (deduped).

    Listing the blob's ``content_hash`` in the sealed capsule manifest makes
    the blob part of the NovaSeal signing scope: post-hoc blob tampering is
    detectable against the sealed hash (``nova validate`` recomputes it).
    """
    artifacts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for call_id, media in iter_media_parts(capsule_dir):
        blob_ref = media.get("blob_ref")
        content_hash = media.get("content_hash")
        if not isinstance(blob_ref, str) or not isinstance(content_hash, str):
            continue
        if not _CONTENT_HASH_RE.match(content_hash) or content_hash in seen:
            continue
        seen.add(content_hash)
        artifacts.append({
            "name": f"{media.get('type', 'media')}-{content_hash[7:19]}",
            "path": blob_ref,
            "content_hash": content_hash,
            "size_bytes": int(media.get("byte_size", 0)),
            "media_type": str(media.get("media_type", "application/octet-stream")),
            "produced_by_call": (
                call_id if call_id and _ULID_RE.match(call_id) else None
            ),
        })
    return artifacts


def verify_media_blobs(capsule_dir: Path) -> list[str]:
    """Integrity-check every MediaPart in a capsule (used by ``nova validate``).

    Checks, per media block: schema validity against the graduated
    ``media-part.schema.json``; blob existence when ``blob_ref`` is set; and a
    recomputed sha256 of the stored bytes against the recorded
    ``content_hash`` — a mismatch is an integrity error (same rule as any
    Artifact; spec §Edge cases "Corrupt or truncated capture").
    """
    errors: list[str] = []
    validator = _media_part_validator()
    for call_id, media in iter_media_parts(capsule_dir):
        where = f"model-calls.jsonl (model_call_id={call_id or '?'})"
        for err in validator.iter_errors(media):
            errors.append(f"{where}: media: {err.message}")
        blob_ref = media.get("blob_ref")
        if not isinstance(blob_ref, str):
            continue
        blob_path = capsule_dir / blob_ref
        if not blob_path.is_file():
            errors.append(f"{where}: missing media blob: {blob_ref}")
            continue
        recorded = media.get("content_hash")
        actual = "sha256:" + hashlib.sha256(blob_path.read_bytes()).hexdigest()
        if recorded != actual:
            errors.append(
                f"{where}: media blob integrity error: {blob_ref} "
                f"recorded {recorded} != stored {actual}"
            )
    return errors


def _media_part_validator() -> Any:
    """Draft 2020-12 validator over the packaged ``media-part.schema.json``."""
    import jsonschema  # type: ignore[import-untyped]

    schema_path = (
        Path(__file__).parents[1] / "schemas" / "media-part.schema.json"
    )
    schema = json.loads(schema_path.read_text())
    return jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    )
