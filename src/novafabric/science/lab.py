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

"""Lab-experiment lineage and instrument provenance — ADR-0164 D3/P3 (NF-322, NF-329).

A :class:`LabExperiment` records an experiment's *declared* provenance — which
kind of lab ran it (``self_driving | cloud_lab | manual | simulation``), the
protocol digest, the instruments it used, the lab's run id, whether it was a
simulation or a physical run (``sim_to_real``), and the outcome digest. Each
instrument it names is an :class:`InstrumentRecord` (NF-329): declared id,
class, firmware digest, calibration-record digest and timestamp, manufacturer
reference. ``instrument_refs`` are the instruments' ``record_digest`` values, so
an experiment binds *exactly* the instrument records it names — change a
firmware digest and the reference stops resolving.

Both ride inside ``facets.science_provenance`` next to the P1 DAG and the P2
receipt, under :data:`LAB_KEY` and :data:`INSTRUMENTS_KEY`, through the P1
facet's ``extra="allow"`` — no schema change (the same pattern as
:data:`novafabric.science.reproducibility.RECEIPT_KEY`).

Three rules shape everything here:

- **Bind, don't run (I-4).** NovaFabric never dispatches to, schedules, or
  controls a lab or an instrument. Every field is a producer declaration; the
  verifier checks the declarations are *coherent* (references resolve,
  calibration precedes the experiment, digests re-derive), never that the
  experiment was sound. :class:`LabVerification` carries ``controls_lab=False``,
  ``reads_telemetry=False`` and ``verdict=None`` as fixed values.
- **No telemetry, no raw data (I-2).** Only digests, short identifiers,
  timestamps and counts. :func:`check_no_payload` walks every block — including
  ``extra`` keys — before any field is parsed and rejects bytes, array-likes,
  over-long strings, credential-shaped strings, and any key that names a
  telemetry / raw-data / secret payload (keys are normalised first, so
  ``Raw-Data``, ``raw_data`` and ``RAWDATA`` are one key).
- **Fail closed on verify.** An unresolved instrument, a calibration after the
  experiment, an unknown experiment time, or a digest that does not re-derive
  each fail verification — an unperformed check is never reported as passed.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from novafabric._hashutil import sha256_prefixed
from novafabric.science.provenance import (
    FACET_NAME,
    PayloadCaptureError,
    ScienceProvenanceError,
)
from novafabric.science.reproducibility import IN_MISSION_BOUNDARY

__all__ = [
    "INSTRUMENTS_KEY",
    "LAB_BOUNDARY",
    "LAB_KEY",
    "LAB_KINDS",
    "LAB_SCHEMA_VERSION",
    "MAX_INSTRUMENTS",
    "SIM_TO_REAL",
    "DuplicateInstrumentError",
    "InstrumentRecord",
    "InstrumentTelemetryError",
    "InvalidCalibrationTimestampError",
    "InvalidLabRecordError",
    "LabExperiment",
    "LabKind",
    "LabProvenance",
    "LabProvenanceError",
    "LabVerification",
    "SimToReal",
    "UnknownLabKindError",
    "attach_lab",
    "build_instrument_record",
    "build_lab_experiment",
    "check_no_payload",
    "lab_from_capsule",
    "lab_from_facet_block",
    "verify_lab",
]

#: Keys inside ``facets.science_provenance``. The P1 facet model is
#: ``extra="allow"``, which is what lets both land without a schema change.
LAB_KEY = "lab_experiment"
INSTRUMENTS_KEY = "instrument_provenance"
LAB_SCHEMA_VERSION = "0.1.0"

#: Domain tags hashed into each digest so an instrument record's digest can
#: never collide with a lab-experiment digest over a similar-looking dict.
_INSTRUMENT_DOMAIN = "novafabric.science.instrument_provenance/v1"
_LAB_DOMAIN = "novafabric.science.lab_experiment/v1"

#: The boundary line every ``nova science lab|instrument`` output carries.
LAB_BOUNDARY = (
    IN_MISSION_BOUNDARY + " It never dispatches to, schedules or controls a lab or "
    "instrument, and never reads instrument telemetry — only declared digests and "
    "references."
)

LabKind = Literal["self_driving", "cloud_lab", "manual", "simulation"]
SimToReal = Literal["sim", "real", "hybrid"]
LAB_KINDS: tuple[str, ...] = ("self_driving", "cloud_lab", "manual", "simulation")
SIM_TO_REAL: tuple[str, ...] = ("sim", "real", "hybrid")

#: Bound on instruments per experiment and records per facet — an offline
#: verifier walking an untrusted capsule must do bounded work.
MAX_INSTRUMENTS = 256
#: Cap on every free string (identifiers, refs, extra values). A digest is 71
#: characters; 256 leaves room for a URI-shaped manufacturer ref and nothing
#: that looks like an inlined document.
MAX_STRING_LENGTH = 256
#: Bounds on the payload walk over ``extra`` content.
_MAX_DEPTH = 8
_MAX_WALK_ITEMS = 4096

#: Strict digest form; matched with ``fullmatch`` so a trailing newline fails.
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
#: A printable single-line identifier (no control characters).
_IDENTIFIER_RE = re.compile(r"[^\x00-\x1f\x7f]+")

#: Substrings of a *normalised* key that name a payload rather than a reference
#: to one. Normalisation (NFKC, casefold, drop every non-alphanumeric) runs
#: first, so separators and case cannot split a marker.
_PAYLOAD_KEY_MARKERS: tuple[str, ...] = (
    "telemetry",
    "rawdata",
    "rawbytes",
    "payload",
    "reading",
    "measurement",
    "timeseries",
    "waveform",
    "spectrum",
    "spectra",
    "sensordata",
    "datastream",
    "sample",
    "dataset",
    "blob",
    "frames",
    "image",
    "pixels",
)
#: Credential markers — never exempt, not even under a ``_digest`` name: a hash
#: of a low-entropy secret is brute-forceable, so it is still a secret.
_SECRET_KEY_MARKERS: tuple[str, ...] = (
    "secret",
    "password",
    "passwd",
    "token",
    "apikey",
    "credential",
    "privatekey",
    "bearer",
    "cookie",
)
#: Credential shapes in a *value*, matched case-insensitively as substrings.
_SECRET_VALUE_MARKERS: tuple[str, ...] = (
    "-----begin",
    "private key",
    "bearer ",
    "password=",
    "passwd=",
    "secret=",
    "token=",
    "apikey=",
    "api_key=",
)
_DATA_URI_RE = re.compile(r"data:[^,;]*;base64,", re.IGNORECASE)


# ── Errors ────────────────────────────────────────────────────────────────


class LabProvenanceError(ScienceProvenanceError):
    """Base class for every NF-322 / NF-329 error.

    Not a ``ValueError`` (inherits :class:`ScienceProvenanceError`), so pydantic
    propagates it by name instead of folding it into a generic
    ``ValidationError``.
    """


class InvalidLabRecordError(LabProvenanceError):
    """Raised when a lab / instrument field has the wrong shape."""


class UnknownLabKindError(LabProvenanceError):
    """Raised when ``lab_kind`` or ``sim_to_real`` is outside its closed set."""


class InvalidCalibrationTimestampError(LabProvenanceError):
    """Raised when a timestamp is not an offset-aware ISO-8601 instant."""


class DuplicateInstrumentError(LabProvenanceError):
    """Raised when two instrument records (or two refs) share one digest."""


class InstrumentTelemetryError(PayloadCaptureError):
    """Raised when telemetry, raw data, or a credential reaches a lab block (I-2).

    Names the field path and the rule that fired — never the value, which is
    the payload this exception exists to keep out of logs as well as capsules.
    """

    def __init__(self, path: str, rule: str) -> None:
        self.path = path
        self.rule = rule
        super().__init__(
            f"field {path or '<root>'!r} is rejected ({rule}); lab and instrument "
            "provenance records digests, references and counts only — never "
            "instrument telemetry, raw data, or credentials (ADR-0164 I-2)"
        )


# ── Payload boundary (I-2) ────────────────────────────────────────────────


def _normalise_key(key: str) -> str:
    """NFKC-fold, casefold, and drop non-alphanumerics: ``Raw-Data`` → ``rawdata``."""
    folded = unicodedata.normalize("NFKC", key).casefold()
    return "".join(ch for ch in folded if ch.isalnum())


def _exempt_by_shape(norm_key: str, value: object) -> bool:
    """A marker key is allowed when it holds a *reference to*, not the payload.

    ``telemetry_digest: sha256:…`` and ``sample_count: 12`` are references and
    counts — exactly what the spec allows. The name alone is not enough: the
    value must actually be a digest / a non-negative integer.
    """
    if norm_key.endswith("digest"):
        return isinstance(value, str) and _DIGEST_RE.fullmatch(value) is not None
    if norm_key.endswith("count"):
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    return False


def _check_key(key: object, value: object, path: str) -> None:
    if not isinstance(key, str):
        raise InstrumentTelemetryError(path, "non-string key")
    if len(key) > MAX_STRING_LENGTH:
        raise InstrumentTelemetryError(path, "over-long key")
    norm = _normalise_key(key)
    for marker in _SECRET_KEY_MARKERS:
        if marker in norm:
            raise InstrumentTelemetryError(path, f"credential-named key ({marker})")
    for marker in _PAYLOAD_KEY_MARKERS:
        if marker in norm and not _exempt_by_shape(norm, value):
            raise InstrumentTelemetryError(path, f"payload-named key ({marker})")


def _check_scalar(value: object, path: str) -> None:
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise InstrumentTelemetryError(path, f"{type(value).__name__} buffer")
    if isinstance(value, (bool, int, float)) or value is None:
        return
    if isinstance(value, datetime):
        return
    if isinstance(value, str):
        if len(value) > MAX_STRING_LENGTH:
            raise InstrumentTelemetryError(
                path, f"{len(value)}-char string over the {MAX_STRING_LENGTH}-char cap"
            )
        lowered = value.casefold()
        if _DATA_URI_RE.match(value):
            raise InstrumentTelemetryError(path, "base64 data: URI")
        for marker in _SECRET_VALUE_MARKERS:
            if marker in lowered:
                raise InstrumentTelemetryError(path, "credential-shaped value")
        from novafabric.capture.secrets import scan_text_rule_ids

        rules = scan_text_rule_ids(value)
        if rules:
            raise InstrumentTelemetryError(path, f"secret pattern {rules[0]}")
        return
    # Array-likes (numpy / torch / PIL) duck-typed, never imported (ADR-0024).
    raise InstrumentTelemetryError(path, f"non-JSON value of type {type(value).__name__}")


def check_no_payload(block: object, *, path: str = "") -> None:
    """Reject telemetry, raw data, or credentials anywhere in ``block`` (I-2).

    Iterative, depth- and size-bounded: this runs inside offline verifiers on
    capsules they did not produce. Keys are normalised before marker matching;
    every string is length-capped and scanned with the ADR-0009 rule pack.

    Raises:
        InstrumentTelemetryError: on the first offending field, naming its path.
    """
    stack: list[tuple[object, str, int]] = [(block, path, 0)]
    seen = 0
    while stack:
        value, where, depth = stack.pop()
        seen += 1
        if seen > _MAX_WALK_ITEMS:
            raise InstrumentTelemetryError(path, f"more than {_MAX_WALK_ITEMS} values")
        if depth > _MAX_DEPTH:
            raise InstrumentTelemetryError(where, f"nesting deeper than {_MAX_DEPTH}")
        if isinstance(value, Mapping):
            for key, child in value.items():
                child_path = f"{where}.{key}" if where else str(key)
                _check_key(key, child, child_path)
                stack.append((child, child_path, depth + 1))
        elif isinstance(value, (list, tuple)):
            if len(value) > MAX_INSTRUMENTS:
                raise InstrumentTelemetryError(
                    where, f"list of {len(value)} items over the {MAX_INSTRUMENTS} cap"
                )
            for i, child in enumerate(value):
                stack.append((child, f"{where}[{i}]", depth + 1))
        else:
            _check_scalar(value, where)


# ── Field validators ──────────────────────────────────────────────────────


def _digest(value: object, *, field: str) -> str:
    """Return ``value`` if it is exactly ``sha256:<64 lower-case hex>``."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise InstrumentTelemetryError(field, f"{type(value).__name__} buffer")
    if not isinstance(value, str):
        raise InvalidLabRecordError(f"{field} must be a string digest")
    if len(value) > MAX_STRING_LENGTH:
        raise InstrumentTelemetryError(field, f"{len(value)}-char string in a digest field")
    if _DIGEST_RE.fullmatch(value) is None:
        raise InvalidLabRecordError(f"{field} must be a content digest 'sha256:<64 hex>'")
    return value


def _identifier(value: object, *, field: str) -> str:
    """Return ``value`` if it is a short, printable, single-line label."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise InstrumentTelemetryError(field, f"{type(value).__name__} buffer")
    if not isinstance(value, str) or not value.strip():
        raise InvalidLabRecordError(f"{field} must be a non-empty string identifier")
    if len(value) > MAX_STRING_LENGTH:
        raise InstrumentTelemetryError(field, f"{len(value)}-char identifier")
    if _IDENTIFIER_RE.fullmatch(value) is None:
        raise InvalidLabRecordError(f"{field} must not contain control characters")
    return value


def _parse_instant(value: object, *, field: str) -> datetime:
    """Parse an offset-aware ISO-8601 instant; naive or malformed input raises."""
    if isinstance(value, datetime):
        parsed = value
    else:
        if not isinstance(value, str) or not value.strip() or len(value) > 64:
            raise InvalidCalibrationTimestampError(
                f"{field} must be an ISO-8601 timestamp string with a UTC offset"
            )
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise InvalidCalibrationTimestampError(f"{field} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidCalibrationTimestampError(
            f"{field} has no UTC offset; a naive time cannot be ordered against the experiment"
        )
    return parsed


def _timestamp(value: object, *, field: str) -> str:
    """Validate and normalise a timestamp to its stored string form."""
    parsed = _parse_instant(value, field=field)
    # A YAML loader may hand back a datetime; store the ISO form. Strings are
    # kept exactly as declared so the sealed bytes are the producer's.
    return parsed.isoformat() if isinstance(value, datetime) else str(value)


def _canonical_digest(body: dict[str, Any], *, domain: str) -> str:
    blob = json.dumps(
        {"domain": domain, "body": body}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256_prefixed(blob)


# ── Objects ───────────────────────────────────────────────────────────────


class InstrumentRecord(BaseModel):
    """One ``facets.science_provenance.instrument_provenance[]`` entry (NF-329).

    Every field is a *declaration*: NovaFabric never contacts the instrument,
    reads its telemetry, or checks that the calibration was any good.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = LAB_SCHEMA_VERSION
    instrument_id: str
    instrument_class: str
    firmware_digest: str
    #: Digest of the declared calibration record — never the record itself.
    calibration_ref: str
    calibration_timestamp: str
    manufacturer_ref: str
    #: Content digest of this record (every field but itself, extras included).
    #: This is what a :class:`LabExperiment` names in ``instrument_refs``.
    record_digest: str

    @model_validator(mode="before")
    @classmethod
    def _boundary(cls, data: object) -> object:
        check_no_payload(data, path=INSTRUMENTS_KEY)
        return data

    @field_validator(
        "schema_version", "instrument_id", "instrument_class", "manufacturer_ref", mode="before"
    )
    @classmethod
    def _check_identifier(cls, v: object, info: ValidationInfo) -> str:
        return _identifier(v, field=str(info.field_name))

    @field_validator("firmware_digest", "calibration_ref", "record_digest", mode="before")
    @classmethod
    def _check_digest(cls, v: object, info: ValidationInfo) -> str:
        return _digest(v, field=str(info.field_name))

    @field_validator("calibration_timestamp", mode="before")
    @classmethod
    def _check_ts(cls, v: object) -> str:
        return _timestamp(v, field="calibration_timestamp")

    def recompute_digest(self) -> str:
        """Recompute :attr:`record_digest` from this record's own content."""
        body = self.model_dump(mode="json", exclude_none=True, exclude={"record_digest"})
        return _canonical_digest(body, domain=_INSTRUMENT_DOMAIN)

    @property
    def calibrated_at(self) -> datetime:
        """The calibration instant, parsed for comparison only."""
        return _parse_instant(self.calibration_timestamp, field="calibration_timestamp")


class LabExperiment(BaseModel):
    """The ``facets.science_provenance.lab_experiment`` block (NF-322)."""

    model_config = ConfigDict(extra="allow")

    schema_version: str = LAB_SCHEMA_VERSION
    lab_kind: LabKind
    #: Digest of the declared protocol / job script — never the protocol.
    protocol_ref: str
    #: ``record_digest`` values of the NF-329 instrument records used.
    instrument_refs: list[str] = Field(default_factory=list)
    #: The lab's own run / job identifier (a label, not a NovaFabric run id).
    run_id: str
    sim_to_real: SimToReal
    outcome_digest: str
    #: When the experiment started (offset-aware). Optional extension: when
    #: absent the verifier falls back to the capsule's ``created_at``.
    started_at: str | None = None
    #: Optional digest of the ``experiment_run`` / ``experiment_design`` node in
    #: the P1 lineage this experiment realises — the binding into the DAG.
    node_ref: str | None = None
    #: The sealed science root, when the facet records one. Hashed in.
    capsule_root: str | None = None
    experiment_digest: str

    @model_validator(mode="before")
    @classmethod
    def _boundary(cls, data: object) -> object:
        check_no_payload(data, path=LAB_KEY)
        return data

    @field_validator("lab_kind", mode="before")
    @classmethod
    def _check_kind(cls, v: object) -> object:
        if v not in LAB_KINDS:
            raise UnknownLabKindError(f"lab_kind must be one of {', '.join(LAB_KINDS)}")
        return v

    @field_validator("sim_to_real", mode="before")
    @classmethod
    def _check_sim(cls, v: object) -> object:
        if v not in SIM_TO_REAL:
            raise UnknownLabKindError(f"sim_to_real must be one of {', '.join(SIM_TO_REAL)}")
        return v

    @field_validator("schema_version", "run_id", mode="before")
    @classmethod
    def _check_identifier(cls, v: object, info: ValidationInfo) -> str:
        return _identifier(v, field=str(info.field_name))

    @field_validator("protocol_ref", "outcome_digest", "experiment_digest", mode="before")
    @classmethod
    def _check_digest(cls, v: object, info: ValidationInfo) -> str:
        return _digest(v, field=str(info.field_name))

    @field_validator("node_ref", "capsule_root", mode="before")
    @classmethod
    def _check_optional_digest(cls, v: object, info: ValidationInfo) -> str | None:
        return None if v is None else _digest(v, field=str(info.field_name))

    @field_validator("started_at", mode="before")
    @classmethod
    def _check_started(cls, v: object) -> str | None:
        return None if v is None else _timestamp(v, field="started_at")

    @field_validator("instrument_refs", mode="before")
    @classmethod
    def _check_refs(cls, v: object) -> list[str]:
        if v is None:
            return []
        if not isinstance(v, (list, tuple)):
            raise InvalidLabRecordError("instrument_refs must be a list of digests")
        refs = [_digest(r, field="instrument_refs") for r in v]
        if len(set(refs)) != len(refs):
            raise DuplicateInstrumentError("instrument_refs names the same instrument twice")
        return refs

    def recompute_digest(self) -> str:
        """Recompute :attr:`experiment_digest` from this block's own content."""
        body = self.model_dump(mode="json", exclude_none=True, exclude={"experiment_digest"})
        return _canonical_digest(body, domain=_LAB_DOMAIN)


@dataclass(frozen=True)
class LabProvenance:
    """A lab experiment together with the instrument records in the same facet."""

    experiment: LabExperiment | None
    instruments: tuple[InstrumentRecord, ...]

    @property
    def has_material(self) -> bool:
        """True when there is anything at all to record."""
        return self.experiment is not None or bool(self.instruments)


class LabVerification(BaseModel):
    """What :func:`verify_lab` checked — and, explicitly, what it never does.

    ``controls_lab``, ``reads_telemetry`` and ``verdict`` are fixed values so
    every serialised verification *shows* the record-only boundary (I-4).
    """

    model_config = ConfigDict(frozen=True)

    experiment_present: bool
    experiment_digest_ok: bool
    instrument_digests_ok: bool
    tampered_instruments: list[str]
    instrument_refs_resolve: bool
    unresolved_instrument_refs: list[str]
    unreferenced_instruments: list[str]
    experiment_time: str | None
    experiment_time_source: Literal["lab_experiment.started_at", "capsule.created_at"] | None
    calibration_not_after_experiment: bool
    calibrated_after_experiment: list[str]
    sim_to_real_consistent: bool
    #: ``None`` when the experiment names no lineage node (nothing to check).
    lineage_node_resolves: bool | None
    controls_lab: Literal[False] = False
    reads_telemetry: Literal[False] = False
    verdict: None = None

    @property
    def ok(self) -> bool:
        """True when every performed check passed (fail-closed)."""
        return (
            self.experiment_present
            and self.experiment_digest_ok
            and self.instrument_digests_ok
            and self.instrument_refs_resolve
            and self.calibration_not_after_experiment
            and self.sim_to_real_consistent
            and self.lineage_node_resolves is not False
        )


# ── Build ─────────────────────────────────────────────────────────────────


def build_instrument_record(
    *,
    instrument_id: str,
    instrument_class: str,
    firmware_digest: str,
    calibration_ref: str,
    calibration_timestamp: str,
    manufacturer_ref: str,
) -> InstrumentRecord:
    """Build an NF-329 instrument record with its ``record_digest`` computed.

    Raises:
        LabProvenanceError: a field has the wrong shape.
        InstrumentTelemetryError: a payload or credential reached a field (I-2).
    """
    fields: dict[str, Any] = {
        "schema_version": LAB_SCHEMA_VERSION,
        "instrument_id": instrument_id,
        "instrument_class": instrument_class,
        "firmware_digest": firmware_digest,
        "calibration_ref": calibration_ref,
        "calibration_timestamp": calibration_timestamp,
        "manufacturer_ref": manufacturer_ref,
    }
    # Validate once with a placeholder digest, then seal the normalised body.
    draft = InstrumentRecord.model_validate({**fields, "record_digest": "sha256:" + "0" * 64})
    return draft.model_copy(update={"record_digest": draft.recompute_digest()})


def build_lab_experiment(
    *,
    lab_kind: str,
    protocol_ref: str,
    run_id: str,
    sim_to_real: str,
    outcome_digest: str,
    instrument_refs: Sequence[str] = (),
    started_at: str | None = None,
    node_ref: str | None = None,
    capsule_root: str | None = None,
) -> LabExperiment:
    """Build an NF-322 lab-experiment block with its ``experiment_digest`` computed.

    Records declarations only; nothing is dispatched anywhere (I-4).

    Raises:
        UnknownLabKindError: ``lab_kind`` / ``sim_to_real`` outside its closed set.
        LabProvenanceError: a field has the wrong shape.
        InstrumentTelemetryError: a payload or credential reached a field (I-2).
    """
    fields: dict[str, Any] = {
        "schema_version": LAB_SCHEMA_VERSION,
        "lab_kind": lab_kind,
        "protocol_ref": protocol_ref,
        "instrument_refs": list(instrument_refs),
        "run_id": run_id,
        "sim_to_real": sim_to_real,
        "outcome_digest": outcome_digest,
        "started_at": started_at,
        "node_ref": node_ref,
        "capsule_root": capsule_root,
    }
    fields = {k: v for k, v in fields.items() if v is not None}
    draft = LabExperiment.model_validate({**fields, "experiment_digest": "sha256:" + "0" * 64})
    return draft.model_copy(update={"experiment_digest": draft.recompute_digest()})


# ── Capsule attach / read ─────────────────────────────────────────────────


def attach_lab(
    capsule: dict[str, Any],
    experiment: LabExperiment | None,
    instruments: Sequence[InstrumentRecord] = (),
) -> dict[str, Any]:
    """Attach lab / instrument provenance under ``facets.science_provenance``.

    Additive: preserves the P1 DAG, the P2 receipt, and every other facet. With
    nothing to record the capsule is returned unchanged (I-1, I-3). Returns a
    new dict; the input is not mutated.

    Raises:
        DuplicateInstrumentError: two instrument records share a digest.
    """
    if experiment is None and not instruments:
        return capsule
    if len(instruments) > MAX_INSTRUMENTS:
        raise InvalidLabRecordError(f"more than {MAX_INSTRUMENTS} instrument records")
    digests = [r.record_digest for r in instruments]
    if len(set(digests)) != len(digests):
        raise DuplicateInstrumentError("two instrument records share one record_digest")
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    science = dict(facets.get(FACET_NAME) or {})
    if experiment is not None:
        science[LAB_KEY] = experiment.model_dump(mode="json", exclude_none=True)
    if instruments:
        ordered = sorted(instruments, key=lambda r: r.record_digest)
        science[INSTRUMENTS_KEY] = [r.model_dump(mode="json", exclude_none=True) for r in ordered]
    facets[FACET_NAME] = science
    out["facets"] = facets
    return out


def lab_from_facet_block(science: Mapping[str, Any]) -> LabProvenance:
    """Parse the lab / instrument blocks out of a ``science_provenance`` mapping.

    Raises:
        pydantic.ValidationError / LabProvenanceError: a block is present but
            malformed — a present-but-broken record is a finding, not absence.
    """
    raw_exp = science.get(LAB_KEY)
    raw_instruments = science.get(INSTRUMENTS_KEY)
    experiment = None
    if raw_exp is not None:
        if not isinstance(raw_exp, Mapping):
            raise InvalidLabRecordError(f"{LAB_KEY} must be a mapping")
        experiment = LabExperiment.model_validate(dict(raw_exp))
    instruments: list[InstrumentRecord] = []
    if raw_instruments is not None:
        if not isinstance(raw_instruments, list):
            raise InvalidLabRecordError(f"{INSTRUMENTS_KEY} must be a list")
        if len(raw_instruments) > MAX_INSTRUMENTS:
            raise InvalidLabRecordError(f"more than {MAX_INSTRUMENTS} instrument records")
        for item in raw_instruments:
            if not isinstance(item, Mapping):
                raise InvalidLabRecordError(f"{INSTRUMENTS_KEY} entries must be mappings")
            instruments.append(InstrumentRecord.model_validate(dict(item)))
        digests = [r.record_digest for r in instruments]
        if len(set(digests)) != len(digests):
            raise DuplicateInstrumentError("two instrument records share one record_digest")
    return LabProvenance(experiment=experiment, instruments=tuple(instruments))


def lab_from_capsule(capsule: Mapping[str, Any]) -> LabProvenance | None:
    """Read lab / instrument provenance from a capsule dict; None when absent (I-3)."""
    facets = capsule.get("facets")
    if not isinstance(facets, Mapping):
        return None
    science = facets.get(FACET_NAME)
    if not isinstance(science, Mapping):
        return None
    lab = lab_from_facet_block(science)
    return lab if lab.has_material else None


# ── Verify ────────────────────────────────────────────────────────────────


def _experiment_time(
    experiment: LabExperiment, capsule: Mapping[str, Any] | None
) -> tuple[datetime | None, str | None]:
    if experiment.started_at is not None:
        return (
            _parse_instant(experiment.started_at, field="started_at"),
            "lab_experiment.started_at",
        )
    created = capsule.get("created_at") if capsule is not None else None
    if created is None:
        return None, None
    try:
        return _parse_instant(created, field="created_at"), "capsule.created_at"
    except LabProvenanceError:
        return None, None


def _lineage_node_resolves(
    experiment: LabExperiment, capsule: Mapping[str, Any] | None
) -> bool | None:
    if experiment.node_ref is None:
        return None
    facets = capsule.get("facets") if capsule is not None else None
    science = facets.get(FACET_NAME) if isinstance(facets, Mapping) else None
    nodes = science.get("hypothesis_experiment_result") if isinstance(science, Mapping) else None
    if not isinstance(nodes, list):
        return False
    for node in nodes:
        if (
            isinstance(node, Mapping)
            and node.get("node_digest") == experiment.node_ref
            and node.get("kind") in ("experiment_design", "experiment_run")
        ):
            return True
    return False


def verify_lab(lab: LabProvenance, *, capsule: Mapping[str, Any] | None = None) -> LabVerification:
    """Offline-verify declared lab / instrument provenance without raising.

    Checks, fail-closed: an experiment is present; its digest and every
    instrument digest re-derive; every ``instrument_ref`` resolves to an
    instrument record; no calibration is after the experiment (experiment time
    is ``started_at`` or else the capsule's ``created_at`` — unknown fails);
    ``lab_kind: simulation`` is not declared ``sim_to_real: real``; and an
    optional ``node_ref`` resolves to an experiment node of the P1 lineage.
    Pure comparison: no network, no instrument, no lab.
    """
    by_digest = {r.record_digest: r for r in lab.instruments}
    tampered = sorted(
        r.instrument_id for r in lab.instruments if r.recompute_digest() != r.record_digest
    )
    exp = lab.experiment
    if exp is None:
        return LabVerification(
            experiment_present=False,
            experiment_digest_ok=False,
            instrument_digests_ok=not tampered,
            tampered_instruments=tampered,
            instrument_refs_resolve=False,
            unresolved_instrument_refs=[],
            unreferenced_instruments=sorted(r.instrument_id for r in lab.instruments),
            experiment_time=None,
            experiment_time_source=None,
            calibration_not_after_experiment=False,
            calibrated_after_experiment=[],
            sim_to_real_consistent=False,
            lineage_node_resolves=None,
        )
    unresolved = [ref for ref in exp.instrument_refs if ref not in by_digest]
    referenced = set(exp.instrument_refs)
    unreferenced = sorted(
        r.instrument_id for r in lab.instruments if r.record_digest not in referenced
    )
    when, source = _experiment_time(exp, capsule)
    late: list[str] = []
    if when is not None:
        late = sorted(
            by_digest[ref].instrument_id
            for ref in exp.instrument_refs
            if ref in by_digest and by_digest[ref].calibrated_at > when
        )
    # Without a known experiment time the ordering cannot be established; with
    # no instruments there is nothing to order.
    calibration_ok = (not exp.instrument_refs or when is not None) and not late
    return LabVerification(
        experiment_present=True,
        experiment_digest_ok=exp.recompute_digest() == exp.experiment_digest,
        instrument_digests_ok=not tampered,
        tampered_instruments=tampered,
        instrument_refs_resolve=not unresolved,
        unresolved_instrument_refs=unresolved,
        unreferenced_instruments=unreferenced,
        experiment_time=when.isoformat() if when is not None else None,
        experiment_time_source=source,  # type: ignore[arg-type]
        calibration_not_after_experiment=calibration_ok,
        calibrated_after_experiment=late,
        sim_to_real_consistent=not (exp.lab_kind == "simulation" and exp.sim_to_real == "real"),
        lineage_node_resolves=_lineage_node_resolves(exp, capsule),
    )
