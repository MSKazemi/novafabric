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

"""Sensor provenance + actuation records — ADR-0162 P1 (NF-301/NF-302).

Records what an embodied agent's *body* did, as the run capsule already
records what its brain did: which sensor streams it declared it consumed, and
which commands it declared it issued. References, digests and counts only —
never a frame, a point cloud, a waveform, or a control credential.

Four invariants from ADR-0162 shape every choice in this module:

- **I-1 Additive-first.** The facet lives in optional ``facets.embodied``. A
  run with no embodied material produces no facet, and a capsule without one
  is byte-identical to one captured before this feature existed.
- **I-2 No raw payloads.** A sensor stream is present as a ``stream_digest``
  and a ``frame_count``, and as nothing else. Frames, point clouds, video,
  audio, drive-by-wire bytes and private keys never enter the capsule through
  this path (ADR-0125 reference-not-bytes, ADR-0021 §4, ADR-0009).
- **I-3 Record-only / fail-open.** NovaFabric records that a command was
  declared. It never issues, replays, executes, gates, or blocks one, and it
  never sits in a control or actuation hot path. Nothing here can delay or
  fail a physical workload.
- **I-4 Declared is not observed.** An actuation record is the agent's own
  claim about a command it says it issued. Only a *bound* action-receipt
  (ADR-0093) makes it anything more.

**Declared is not confirmed — the physical-world reason this matters.** In a
software capsule, over-reading a record costs an auditor an incorrect
inference. Here it can mean recording a commanded motion as though a robot
were known to have performed it. A record whose ``action_receipt_ref`` does
not resolve is marked ``unbound`` and :func:`is_confirmed` returns ``False``
for it — never dropped, never quietly promoted to confirmation. NovaFabric is
deliberately outside the control loop (ADR-0162 D1), so a declared command is
the *most* it can know; treating it as more would be the module inventing an
observation of the physical world that nothing made.

**Absent is not false.** No ``sensors`` entry means the stream was *not
recorded* — never that no sensor was present and never that nothing was
observed. No ``actuation`` entry means no command was recorded, not that the
agent commanded nothing. That is why :func:`build_facet` returns ``None``
rather than an empty facet: an empty ``sensors`` list in a sealed root reads
as "we looked at the body and it did nothing", which is a claim about a
collection process this module cannot make on the caller's behalf.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FACET_NAME = "embodied"
SCHEMA_VERSION = "0.1.0"

#: Sensor modalities enumerated by NF-301. Closed, unlike ``command_class``
#: below, because the spec fixes this list normatively and provides ``other``
#: as the escape hatch — so an unfamiliar sensor has a defined home and does
#: not need the set reopened.
Modality = Literal[
    "camera",
    "lidar",
    "radar",
    "imu",
    "gps",
    "depth",
    "tactile",
    "audio",
    "other",
]

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


class MissingIssuerError(Exception):
    """Raised when an actuation record does not name who issued the command.

    ADR-0162 D1: ``issued_by`` is what keeps a command *the agent declared it
    issued* distinguishable from one NovaFabric issued — and NovaFabric issues
    nothing (I-3). A record with no named issuer loses exactly that
    distinction, which is the one this object exists to preserve.
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
        if hasattr(value, "__array_interface__") or hasattr(
            value, "__cuda_array_interface__"
        ):
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
            raise RawPayloadRejectedError(
                path, f"base64-shaped string of {len(value)} characters"
            )


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


# ── Models ────────────────────────────────────────────────────────────────


class SensorStream(BaseModel):
    """One sensor stream the agent declared it consumed (NF-301).

    A stream is present as its identity (``stream_digest``), its size
    (``frame_count``), and its time base (``clock_domain``). The samples stay
    wherever the operator holds them (I-2).
    """

    model_config = ConfigDict(extra="allow")

    sensor_id: str
    modality: Modality
    #: Frames/samples in the referenced segment. Required with **no default**:
    #: defaulting to 0 would write "no frames observed" for a stream nobody
    #: counted, which is precisely the absent-is-not-false confusion this
    #: facet must not create. A caller who did not count cannot honestly emit
    #: a stream record, and should emit none.
    frame_count: int = Field(ge=0)
    #: ``sha256:`` of the stream/segment, held elsewhere — never inlined.
    stream_digest: str
    #: Which clock the frames are timestamped against. Recorded, never
    #: disciplined: NovaFabric steers no clock (NF-308).
    clock_domain: str
    #: Digest of a sensor-signed C2PA manifest, when the sensor signs at
    #: capture. Optional because most sensors do not — and marking its absence
    #: as a finding would report on the hardware, not on the evidence.
    c2pa_manifest_ref: str | None = None

    @field_validator("stream_digest", "c2pa_manifest_ref", mode="before")
    @classmethod
    def _check_refs(cls, value: Any) -> Any:
        # `mode="before"` so that a caller who passed the frame *itself* where
        # a digest belongs gets RawPayloadRejectedError naming their mistake,
        # rather than pydantic's generic "input should be a valid string".
        if value is None or isinstance(value, str):
            return _validate_ref(value)
        reject_raw_payloads(value, path="stream_digest")
        return value

    @model_validator(mode="after")
    def _reject_payloads(self) -> SensorStream:
        reject_raw_payloads(_own_fields(self))
        return self


class ActuationRecord(BaseModel):
    """One command the agent **declared it issued** (NF-302).

    Not a command NovaFabric issued, and not a command anything observed a
    body perform — unless ``action_receipt_ref`` is bound (see
    :func:`is_confirmed`). NovaFabric emits, replays, and executes nothing
    (I-3).
    """

    model_config = ConfigDict(extra="allow")

    #: Coarse declared class (``motion``, ``grip``, ``throttle``, …). Free-form,
    #: unlike :data:`Modality`: the spec gives these as examples ("e.g."), and
    #: a closed set here would force a real robot's command into the wrong
    #: bucket — a worse evidential outcome than an unfamiliar label the
    #: consumer can map. Never drive-by-wire bytes; the payload walk enforces
    #: that structurally rather than by convention.
    command_class: str
    #: ``sha256:`` identity of the actuated subsystem.
    target_ref: str
    #: Commands of this class issued. ``0`` is a *recorded zero* — meaningfully
    #: different from the record being absent.
    count: int = Field(ge=0)
    #: The run/agent that declared it issued the command. Required (I-4).
    issued_by: str
    #: ADR-0093 action-receipt digest. Optional: a declared command with no
    #: receipt is a real and revealing state of the evidence.
    action_receipt_ref: str | None = None
    #: True when no receipt is bound, or when the bound one did not resolve.
    #: Recorded, never fatal, never dropped.
    unbound: bool = True

    @field_validator("target_ref", "action_receipt_ref", mode="before")
    @classmethod
    def _check_refs(cls, value: Any) -> Any:
        if value is None or isinstance(value, str):
            return _validate_ref(value)
        reject_raw_payloads(value, path="target_ref")
        return value

    @field_validator("issued_by")
    @classmethod
    def _require_issuer(cls, value: str) -> str:
        if not value.strip():
            raise MissingIssuerError(
                "actuation record has no issued_by; a command with no named "
                "issuer cannot be told apart from one NovaFabric issued, and "
                "NovaFabric issues nothing (ADR-0162 D1, I-3/I-4)"
            )
        return value

    @model_validator(mode="after")
    def _force_unbound_without_receipt(self) -> ActuationRecord:
        """No receipt ⇒ ``unbound``, on every construction path.

        Enforced in the model rather than only in :func:`build_actuation` so
        that ``model_validate`` of untrusted JSON, ``model_copy(update=…)``,
        and direct instantiation cannot produce a record that claims a binding
        it does not have. A receipt-less command marked ``unbound: false``
        would read, to anyone reconstructing a physical incident, as a
        *confirmed* motion — the single most dangerous field combination this
        module could permit.
        """
        if self.action_receipt_ref is None:
            self.unbound = True
        reject_raw_payloads(_own_fields(self))
        return self


def is_confirmed(record: ActuationRecord) -> bool:
    """Return True only for a command with a *bound* action-receipt.

    Reads the recorded binding back out; it does not judge whether the
    physical action was correct, safe, or in-ODD (I-3). A ``False`` here means
    "NovaFabric holds no receipt for this", never "the command did not
    happen" — the agent declared it either way, and that declaration is what
    the record preserves.
    """
    return record.action_receipt_ref is not None and not record.unbound


class VerifiedBlock(BaseModel):
    """What construction of this facet structurally guarantees.

    Only ``no_raw_payload`` is carried. The spec's ``sealed_into_root`` belongs
    to the sealing phase (ADR-0162 P5): emitting it ``true`` here would claim a
    seal that has not happened, and emitting it ``false`` would report a
    finding nobody made. It is absent rather than stubbed.
    """

    model_config = ConfigDict(extra="allow")

    #: Always True when present: the facet cannot be constructed otherwise.
    no_raw_payload: bool = True


class EmbodiedFacet(BaseModel):
    """The optional ``facets.embodied`` block (I-1).

    P1 carries two of ADR-0162's ten objects. ODD conformance (NF-303),
    sim-to-real (NF-304), teleop (NF-305), timing (NF-308), device identity
    (NF-309) and the trajectory chain (NF-310) arrive in later phases and are
    deliberately absent here rather than stubbed: an empty ``odd`` object in a
    sealed root would read as "the safety envelope was checked and nothing was
    found", which is a far worse error than a missing key.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    sensors: list[SensorStream] = Field(default_factory=list)
    actuation: list[ActuationRecord] = Field(default_factory=list)
    verified: VerifiedBlock = Field(default_factory=VerifiedBlock)

    @model_validator(mode="after")
    def _reject_payloads(self) -> EmbodiedFacet:
        """Enforce I-2 across the whole facet, including ``extra`` fields.

        The sub-models each run this too, but a payload can also arrive as an
        extra key on the facet itself (``EmbodiedFacet(frames=[…])``), which no
        sub-model would ever see. ``extra="allow"`` is mandated for these
        models, so the structural "digests and counts only" discipline needs a
        backstop on the open part of the shape.
        """
        reject_raw_payloads(_own_fields(self))
        return self


# ── Assembly ──────────────────────────────────────────────────────────────


def build_actuation(
    *,
    command_class: str,
    target_ref: str,
    count: int,
    issued_by: str,
    action_receipt_ref: str | None = None,
    resolver: Callable[[str], str | bytes | None] | None = None,
) -> ActuationRecord:
    """Record a declared command, binding its action-receipt where possible.

    ``resolver`` is asked to produce the bytes the receipt digest names. The
    record is marked ``unbound`` when the resolver returns ``None`` (the
    receipt cannot be produced) **or** returns bytes whose digest differs (the
    ref names something else). Both are the same fact to anyone reconstructing
    a physical event: the command has no verifiable confirmation behind it.
    Calling the second case "bound" because a lookup succeeded would be the
    more dangerous error of the two.

    With a receipt ref but no ``resolver`` the record is left ``unbound=False``:
    the caller has asserted a binding this function was given no way to check,
    and inventing an ``unbound`` mark for an unchecked ref would report a
    finding nobody made. With **no** receipt ref at all the record is
    ``unbound=True`` — nothing was even claimed to bind it.

    Note the asymmetry with the sibling ADR-0170 ``build_incident_loss``, which
    *raises* on a missing ref: an incident-loss record without its DFIR bundle
    has nothing to record, whereas a declared command without a receipt is a
    complete and common observation. Making it fatal here would mean refusing
    to record the commands of every robot whose control plane emits no
    receipts, which is most of them (I-3, fail-open).

    Raises:
        MissingIssuerError: if ``issued_by`` is empty.
        InvalidReferenceError: if a ref is not a ``sha256:`` digest.
        RawPayloadRejectedError: if any argument carries sensor bytes.
    """
    unbound = True
    if action_receipt_ref is not None:
        unbound = False
        if resolver is not None:
            try:
                artifact = resolver(action_receipt_ref)
            except Exception:  # noqa: BLE001 - see below
                # A resolver that raised told us nothing about the receipt,
                # only about itself. Fail-open (I-3): record the ref as
                # unresolved rather than failing a physical workload's capsule
                # over a lookup error.
                artifact = None
            unbound = artifact is None or not verify_receipt_binding(
                action_receipt_ref, artifact
            )
    return ActuationRecord(
        command_class=command_class,
        target_ref=target_ref,
        count=count,
        issued_by=issued_by,
        action_receipt_ref=action_receipt_ref,
        unbound=unbound,
    )


def build_facet(
    *,
    sensors: Iterable[SensorStream] = (),
    actuation: Iterable[ActuationRecord] = (),
) -> EmbodiedFacet | None:
    """Build the embodied facet, or ``None`` when there is nothing to record.

    Fail-open (I-3): a run with neither sensor streams nor declared commands
    yields ``None``, not an exception and not an empty facet — see
    :class:`EmbodiedFacet` on why an empty block is worse than no block, and
    the module docstring on absent-is-not-false.

    Sensors are ordered by ``sensor_id`` and commands by ``command_class`` so
    that two captures of the same run produce the same bytes. The input order
    is a collection artefact, not evidence: the facet records *which* streams
    and commands existed, and NF-310's trajectory chain — not list position —
    is where ADR-0162 puts ordering that means something.
    """
    streams = sorted(sensors, key=lambda s: s.sensor_id)
    commands = sorted(actuation, key=lambda a: a.command_class)
    if not streams and not commands:
        return None
    return EmbodiedFacet(sensors=streams, actuation=commands)


def attach_facet(
    capsule: dict[str, Any], facet: EmbodiedFacet | None
) -> dict[str, Any]:
    """Attach the embodied facet to a capsule dict, additively.

    Writes nothing when there is no facet: a run with no embodied material
    must be byte-identical to one captured before this feature existed (I-1).
    Returns a new dict; the input is not mutated.

    ``exclude_none`` drops unset optional fields but never ``unbound``, which
    is a required bool — a command's *lack* of confirmation cannot be
    optimised out of the sealed record (I-4).
    """
    if facet is None:
        return capsule
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    facets[FACET_NAME] = facet.model_dump(exclude_none=True)
    out["facets"] = facets
    return out


__all__ = [
    "FACET_NAME",
    "SCHEMA_VERSION",
    "ActuationRecord",
    "EmbodiedFacet",
    "InvalidReferenceError",
    "MissingIssuerError",
    "Modality",
    "RawPayloadRejectedError",
    "SensorStream",
    "VerifiedBlock",
    "attach_facet",
    "build_actuation",
    "build_facet",
    "digest_stream",
    "is_confirmed",
    "reject_raw_payloads",
    "verify_receipt_binding",
]
