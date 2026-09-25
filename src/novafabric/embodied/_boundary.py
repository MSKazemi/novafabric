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

"""The reference-not-bytes boundary shared by every ``facets.embodied`` object.

Extracted from :mod:`novafabric.embodied.facet` when ADR-0162 P2 (NF-303 ODD
conformance, NF-310 trajectory chain) added objects that need the same I-2
discipline as the P1 sensor and actuation records. One boundary, one set of
rules: an ODD excursion or a trajectory hop must not be a softer place to
smuggle a frame than a sensor record is.

Pure functions and named exceptions only — no IO, no global state.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

#: The in-mission-boundary line every embodied ``verify``/``show``/``export``
#: output carries (ADR-0162 spec §3 req. 4).
IN_MISSION_BOUNDARY = (
    "NovaFabric records, verifies and exports evidence about an embodied agent's "
    "actions; it never controls, drives, flies or actuates anything, fuses sensors "
    "for control, plans a path, or decides whether an action was safe or in-ODD."
)

#: A reference must be a ``sha256:<64 hex>`` digest.
#:
#: The spec's wider "reference (URI) **or** digest" shape is deferred to a
#: later phase: P1's job is the *binding*, and only a digest binds. A URI names
#: a place a stream was, which an offline verifier cannot check and which says
#: nothing about the bytes — accepting one would let a sensor record claim a
#: binding it does not have, and ``unbound`` would then never fire on an
#: actuation receipt.
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: Field names that name a payload rather than a reference to one.
#:
#: Anchored per-token so ``frame_count``, ``audio_stream_digest`` and
#: ``point_cloud_ref`` — all legitimate — survive, while a bare ``frames``,
#: ``image`` or ``point_cloud`` does not. A raw payload arriving under an
#: innocuous name is caught by the value checks below instead; this catches the
#: case where the value is a *string* the caller believed was harmless.
_PAYLOAD_KEY_RE = re.compile(
    r"^(raw|bytes|blob|payload|data|content|frame|frames|image|images|pixels|"
    r"point_?cloud|pointcloud|pcd|scan|video|audio|samples|waveform|buffer)$"
    r"|_(bytes|blob|payload|buffer|pixels)$",
    re.IGNORECASE,
)

#: Base64 / hex alphabet, including the URL-safe variant.
_B64_RE = re.compile(r"^[A-Za-z0-9+/_-]+={0,2}$")

#: A base64-encoded ``data:`` URI, rejected at any length.
_DATA_URI_RE = re.compile(r"^data:[^,;]*;base64,", re.IGNORECASE)

#: Length above which a pure-base64 string is treated as an inlined payload.
#:
#: A judgement call the ADR does not settle. Below this bound an encoded blob
#: and a legitimate opaque identifier are genuinely indistinguishable, and
#: refusing short ones would reject real refs: the longest legitimate value
#: here is a ``sha256:`` digest at 71 characters, so 256 leaves ~3.5x headroom
#: for operator-chosen sensor ids and manifest refs. Above it, a string that is
#: *entirely* base64 alphabet is overwhelmingly an encoded frame — no sensor id,
#: URI, or clock domain looks like that. The check is deliberately one-sided:
#: a long string that is not pure base64 (prose, a path, JSON) is left alone,
#: because rejecting it would be this module policing content rather than
#: enforcing the reference-not-bytes boundary.
_INLINE_PAYLOAD_MAX_LEN = 256


class RawPayloadRejectedError(Exception):
    """Raised when sensor payload bytes are offered to the facet (I-2).

    Deliberately **not** a ``ValueError``. Pydantic v2 catches ``ValueError``
    inside a validator and folds it into a ``ValidationError`` alongside
    ordinary shape complaints, destroying the named type. A caller who passed
    a camera frame, a point cloud, or an audio buffer has made a *specific*
    mistake with privacy and capsule-size consequences (ADR-0021 §4), and must
    be told that, not handed a generic "input should be a valid string".

    Names the field and the rule that fired — never the value, which is the
    payload this exception exists to keep out of logs as well as capsules.
    """

    def __init__(self, path: str, rule: str) -> None:
        super().__init__(
            f"field {path or '<root>'!r} carries a raw sensor payload "
            f"({rule}); facets.embodied records references, digests and counts "
            "only — never frames, point clouds, video, audio, or control "
            "credentials (ADR-0162 I-2, ADR-0125, ADR-0021 §4)"
        )
        self.path = path
        self.rule = rule


class InvalidReferenceError(Exception):
    """Raised when a reference is not a ``sha256:`` digest.

    Not a ``ValueError``, for the reason given on
    :class:`RawPayloadRejectedError`.
    """


# ── The raw-payload boundary (I-2) ────────────────────────────────────────


def _check_scalar(value: Any, path: str) -> None:
    """Raise if a single value is (or plausibly encodes) a sensor payload."""
    # Binary buffers, in every shape the stdlib hands a caller who read a frame.
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise RawPayloadRejectedError(path, f"{type(value).__name__} buffer")

    # Array-likes, duck-typed rather than isinstance-checked. A camera frame
    # normally arrives as a numpy ndarray, a torch tensor, or a PIL image, and
    # importing any of them merely to recognise one would add a heavyweight
    # runtime dependency to a module that records digests (ADR-0024). Every one
    # of them exposes `tobytes`, or `shape` + `dtype`, or the array interface —
    # and no reference, count, or identifier this facet legitimately holds does.
    if not isinstance(value, (str, int, float, bool, type(None))):
        if hasattr(value, "__array_interface__") or hasattr(value, "__cuda_array_interface__"):
            raise RawPayloadRejectedError(path, "array-interface object")
        if hasattr(value, "shape") and hasattr(value, "dtype"):
            raise RawPayloadRejectedError(path, "array-like object")
        if callable(getattr(value, "tobytes", None)):
            raise RawPayloadRejectedError(path, "buffer-exporting object")

    if isinstance(value, str):
        if _DATA_URI_RE.match(value):
            # Unambiguous at any length: a base64 data URI *is* an inlined
            # payload, so no length bound applies.
            raise RawPayloadRejectedError(path, "base64 data: URI")
        if len(value) > _INLINE_PAYLOAD_MAX_LEN and _B64_RE.match(value):
            raise RawPayloadRejectedError(path, f"base64-shaped string of {len(value)} characters")


def reject_raw_payloads(value: Any, *, path: str = "") -> None:
    """Walk ``value`` and raise on the first raw sensor payload found (I-2).

    Walks keys as well as values: a payload can arrive either as bytes under a
    harmless name, or as an innocent-looking value under a name that announces
    it is a payload (``{"frames": "<huge b64>"}`` trips both; ``{"image": ""}``
    trips only the key rule, and should).

    Raises:
        RawPayloadRejectedError: naming the field and the rule, never the value.
    """
    if isinstance(value, Mapping):
        for key, child in value.items():
            name = str(key)
            child_path = f"{path}.{name}" if path else name
            if _PAYLOAD_KEY_RE.search(name):
                raise RawPayloadRejectedError(child_path, "payload-named field")
            reject_raw_payloads(child, path=child_path)
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            reject_raw_payloads(item, path=f"{path}[{index}]")
        return
    _check_scalar(value, path)


def _own_fields(model: BaseModel) -> dict[str, Any]:
    """Declared fields plus ``extra`` ones, with values un-serialised.

    Used instead of ``model_dump()`` because dumping is exactly what must not
    be attempted on an unrecognised object: pydantic would warn or coerce, and
    the value we most need to inspect is the one it cannot serialise.
    """
    return {**model.__dict__, **(model.__pydantic_extra__ or {})}


# ── References ────────────────────────────────────────────────────────────


def digest_stream(content: str | bytes) -> str:
    """Return the ``sha256:`` digest of a stream segment or receipt.

    The **only** function here that accepts bytes, and it retains none of
    them: it exists so a caller has a correct way to produce a ``stream_digest``
    or an ``action_receipt_ref`` without inventing one — the alternative being
    a caller who reaches for the frame itself. The form matches every other
    digest in the capsule, so a verifier does not have to know which subsystem
    wrote it.
    """
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def verify_receipt_binding(ref: str | None, artifact: str | bytes) -> bool:
    """Re-verify a reference against the artifact it claims to bind.

    Returns False for a missing reference. An unbound record is not
    "trivially valid" — it is the case a verifier exists to surface, and
    returning True would pass exactly the records with nothing to check.
    """
    if not ref:
        return False
    return ref == digest_stream(artifact)


def _validate_ref(value: str | None) -> str | None:
    if value is None:
        return None
    reject_raw_payloads(value)
    if not _DIGEST_RE.match(value):
        raise InvalidReferenceError(
            f"reference {value!r} is not a 'sha256:<64 hex>' digest; sensor "
            "streams and action receipts are bound by digest and held "
            "elsewhere — the bytes are never stored here (ADR-0162 D1, I-2)"
        )
    return value


__all__ = [
    "IN_MISSION_BOUNDARY",
    "InvalidReferenceError",
    "RawPayloadRejectedError",
    "digest_stream",
    "reject_raw_payloads",
    "verify_receipt_binding",
]
