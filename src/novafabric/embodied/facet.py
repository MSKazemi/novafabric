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

"""The ``facets.embodied`` block — ADR-0162 P1 (NF-301/302), P2 (NF-303/310), P3 (NF-304/305/308).

P3 adds three more optional objects, each in its own sibling module: the
sim-to-real lineage binding (:mod:`novafabric.embodied.sim2real`, an
unresolvable policy ref recorded as ``unbound: true``), the teleoperation
handoff list (:mod:`novafabric.embodied.teleop`, pseudonymous operator refs,
time-ordered), and per-clock-domain timing evidence
(:mod:`novafabric.embodied.timing`, one entry per domain). All three are
record-only and digest/identifier-only, like everything else here.

P2 adds two optional objects defined in sibling modules and carried here:
the ODD conformance record (:mod:`novafabric.embodied.odd`, ``verdict`` always
null) and the perception→actuation trajectory chain
(:mod:`novafabric.embodied.trajectory`, walked offline, never re-derived). The
I-2 reference-not-bytes boundary they share with P1 lives in
:mod:`novafabric.embodied._boundary`.

Sensor provenance + actuation records (P1):

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

from collections.abc import Callable, Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from novafabric.embodied._boundary import (
    InvalidReferenceError,
    RawPayloadRejectedError,
    _own_fields,
    _validate_ref,
    digest_stream,
    reject_raw_payloads,
    verify_receipt_binding,
)
from novafabric.embodied.odd import OddConformance
from novafabric.embodied.sim2real import Sim2RealLineage
from novafabric.embodied.teleop import TeleopHandoff, build_teleop, check_handoff_sequence
from novafabric.embodied.timing import ClockTiming, build_timing, check_timing_sequence
from novafabric.embodied.trajectory import TrajectoryHop

FACET_NAME = "embodied"
SCHEMA_VERSION = "0.3.0"

#: Cap on operator-chosen free strings (sensor ids, clock domains, command
#: classes, issuers). Generous for an identifier; small enough that no field
#: can smuggle a document past the base64-only payload check.
MAX_FREE_STRING_LEN = 256

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


class MissingIssuerError(Exception):
    """Raised when an actuation record does not name who issued the command.

    ADR-0162 D1: ``issued_by`` is what keeps a command *the agent declared it
    issued* distinguishable from one NovaFabric issued — and NovaFabric issues
    nothing (I-3). A record with no named issuer loses exactly that
    distinction, which is the one this object exists to preserve.
    """


# ── Models ────────────────────────────────────────────────────────────────


class SensorStream(BaseModel):
    """One sensor stream the agent declared it consumed (NF-301).

    A stream is present as its identity (``stream_digest``), its size
    (``frame_count``), and its time base (``clock_domain``). The samples stay
    wherever the operator holds them (I-2).
    """

    model_config = ConfigDict(extra="allow")

    sensor_id: str = Field(min_length=1, max_length=MAX_FREE_STRING_LEN)
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
    clock_domain: str = Field(min_length=1, max_length=MAX_FREE_STRING_LEN)
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
    command_class: str = Field(min_length=1, max_length=MAX_FREE_STRING_LEN)
    #: ``sha256:`` identity of the actuated subsystem.
    target_ref: str
    #: Commands of this class issued. ``0`` is a *recorded zero* — meaningfully
    #: different from the record being absent.
    count: int = Field(ge=0)
    #: The run/agent that declared it issued the command. Required (I-4).
    issued_by: str = Field(max_length=MAX_FREE_STRING_LEN)
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

    ``no_raw_payload`` is always carried; ``odd_verdict_is_null`` is carried
    when an ``odd`` block is (ADR-0162 P2) — both are true by construction,
    because the models cannot represent the alternative. The spec's
    ``sealed_into_root`` belongs to the sealing phase (ADR-0162 P5): emitting it
    ``true`` here would claim a seal that has not happened, and emitting it
    ``false`` would report a finding nobody made. It is absent rather than
    stubbed. So are the NF-310 walk results (``trajectory_acyclic``,
    ``no_broken_parent``): a broken chain is *recordable*, so those are
    findings of :func:`novafabric.embodied.walk_trajectory`, recomputed by every
    reader, never a stored self-attestation.
    """

    model_config = ConfigDict(extra="allow")

    #: Always True when present: the facet cannot be constructed otherwise.
    no_raw_payload: bool = True
    #: True when an ``odd`` block is present (its ``verdict`` cannot be
    #: anything but null); absent otherwise. Derived, never caller-set.
    odd_verdict_is_null: bool | None = None


class EmbodiedFacet(BaseModel):
    """The optional ``facets.embodied`` block (I-1).

    Carries four of ADR-0162's ten objects: sensors (NF-301) and actuation
    (NF-302) from P1, and — optional, ``None`` unless recorded — ODD
    conformance (NF-303, :class:`~novafabric.embodied.odd.OddConformance`) and
    the perception→actuation trajectory chain (NF-310,
    :class:`~novafabric.embodied.trajectory.TrajectoryHop`) from P2.
    P3 adds sim-to-real lineage (NF-304,
    :class:`~novafabric.embodied.sim2real.Sim2RealLineage`), teleop handoffs
    (NF-305, :class:`~novafabric.embodied.teleop.TeleopHandoff`) and
    per-clock-domain timing (NF-308,
    :class:`~novafabric.embodied.timing.ClockTiming`), each optional and
    ``None`` unless recorded. Device identity (NF-309) is **not** here: it was
    deferred out of the P3 slice (it must reuse, not re-specify, the
    ADR-0157 device-identity object) and stays future design — deliberately
    absent rather than stubbed. The same rule governs every optional object: an absent ``odd``
    means no ODD was recorded, never "the safety envelope was checked and
    nothing was found"; an absent ``teleop`` means no handoff was recorded,
    never "no human ever took over" — so each key appears only when its
    evidence does.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = Field(default=SCHEMA_VERSION, max_length=32)
    sensors: list[SensorStream] = Field(default_factory=list)
    actuation: list[ActuationRecord] = Field(default_factory=list)
    #: NF-303 declared ODD + observed excursions, ``verdict: null`` (P2).
    odd: OddConformance | None = None
    #: NF-310 ordered perception→actuation hops (P2). Order is evidence.
    trajectory: list[TrajectoryHop] | None = None
    #: NF-304 sim policy → real deployment binding (P3).
    sim2real: Sim2RealLineage | None = None
    #: NF-305 autonomy↔human handoffs, time-ordered (P3).
    teleop: list[TeleopHandoff] | None = None
    #: NF-308 per-clock-domain timing evidence, one entry per domain (P3).
    timing: list[ClockTiming] | None = None
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
        if self.teleop is not None:
            check_handoff_sequence(self.teleop)
        if self.timing is not None:
            check_timing_sequence(self.timing)
        # Derived from the model, never taken from the input: a caller cannot
        # set it true without an odd block, nor false with one.
        self.verified.odd_verdict_is_null = True if self.odd is not None else None
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
            unbound = artifact is None or not verify_receipt_binding(action_receipt_ref, artifact)
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
    odd: OddConformance | None = None,
    trajectory: Iterable[TrajectoryHop] = (),
    sim2real: Sim2RealLineage | None = None,
    teleop: Iterable[TeleopHandoff] = (),
    timing: Iterable[ClockTiming] = (),
) -> EmbodiedFacet | None:
    """Build the embodied facet, or ``None`` when there is nothing to record.

    Fail-open (I-3): a run with no sensor streams, declared commands, ODD
    record, trajectory hops, sim-to-real binding, teleop handoffs or timing
    entries yields ``None``, not an exception and not an empty facet — see
    :class:`EmbodiedFacet` on why an empty block is worse than no block, and
    the module docstring on absent-is-not-false.

    Sensors are ordered by ``sensor_id`` and commands by ``command_class`` so
    that two captures of the same run produce the same bytes. The input order
    is a collection artefact, not evidence: the facet records *which* streams
    and commands existed, and NF-310's trajectory chain — not list position —
    is where ADR-0162 puts ordering that means something. The trajectory is
    therefore kept in the order given, never sorted; ODD excursions are
    already time-ordered by :func:`novafabric.embodied.build_odd`. Teleop
    handoffs are sorted stably by instant (order is evidence there too) and
    timing entries by clock domain.

    Raises:
        HandoffOrderError: over the teleop cap.
        ClockDomainConflictError: on a duplicate clock domain or over the cap.
    """
    streams = sorted(sensors, key=lambda s: s.sensor_id)
    commands = sorted(actuation, key=lambda a: a.command_class)
    hops = list(trajectory)
    handoffs = build_teleop(teleop)
    clocks = build_timing(timing)
    if (
        not streams
        and not commands
        and odd is None
        and not hops
        and sim2real is None
        and not handoffs
        and not clocks
    ):
        return None
    return EmbodiedFacet(
        sensors=streams,
        actuation=commands,
        odd=odd,
        trajectory=hops or None,
        sim2real=sim2real,
        teleop=handoffs or None,
        timing=clocks or None,
    )


def attach_facet(capsule: dict[str, Any], facet: EmbodiedFacet | None) -> dict[str, Any]:
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
