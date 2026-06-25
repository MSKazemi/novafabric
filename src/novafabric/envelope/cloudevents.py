"""CloudEvents v1.0 structured-mode interop for EventEnvelope (ADR-0081, gap-009).

Pure, additive mapping at the serialization boundary. The internal
``EventEnvelope`` model is unchanged; these functions translate to/from the
CloudEvents JSON structured-mode object so NovaFabric evidence events route
through any CloudEvents-aware broker (Kafka/NATS/HTTP) without body parsing.

The attribute contract mirrors ``collector/pkg/envelope/cloudevents_mapping.go``
byte-for-byte (conformant ``[a-z0-9]`` extension names) so a Python producer and
the Go collector interoperate over the same topic.

Spec anchor: src-205 (CNCF CloudEvents v1.0.2).
"""

from __future__ import annotations

import re
from typing import Any

from novafabric.envelope.models import EventEnvelope

_SPEC_VERSION = "1.0"
_DATA_CONTENT_TYPE = "application/json"
_DATA_SCHEMA = "https://novafabric.io/schemas/event-envelope/1"
_SOURCE_PREFIX = "nova://agent/"

# CloudEvents reserved (context) attribute names handled explicitly; everything
# else in a CloudEvents object is an extension attribute.
_RESERVED_ATTRS = frozenset({
    "id",
    "source",
    "type",
    "specversion",
    "time",
    "datacontenttype",
    "dataschema",
    "subject",
    "data",
})

# Mapping of nullable EventEnvelope fields -> CloudEvents extension name.
# Order is stable for deterministic output.
_OPTIONAL_EXT_MAP: tuple[tuple[str, str], ...] = (
    ("parent_run_id", "novaparentrun"),
    ("cluster_id", "novacluster"),
    ("tenant_id", "novatenant"),
    ("emitter_node_id", "novaemitter"),
    ("payload_hash", "novapayloadhash"),
)
# Batch fields carry dot-aliases on the model; handled via to_wire() below.
_BATCH_EXT_MAP: tuple[tuple[str, str], ...] = (
    ("nova.batch.signature", "novabatchsig"),
    ("nova.batch.signing_key_id", "novabatchkid"),
)

# All extension names this mapper owns. Anything else on an incoming CloudEvent
# is an unknown/foreign extension and must be preserved across a round-trip.
_KNOWN_EXTENSIONS = frozenset(
    {"traceparent", "novarunglobal", "novarun"}
    | {ext for _, ext in _OPTIONAL_EXT_MAP}
    | {ext for _, ext in _BATCH_EXT_MAP}
)

# Stash key in the envelope's ``extra`` block for foreign extensions.
_FOREIGN_EXT_KEY = "cloudevents_extensions"

_TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")


def to_cloudevents(envelope: EventEnvelope) -> dict[str, Any]:
    """Render an ``EventEnvelope`` as a CloudEvents v1.0 structured-mode object.

    Returns a plain ``dict`` (JSON-serializable). Required CloudEvents context
    attributes (``id``, ``source``, ``type``, ``specversion``) are always
    present. Null envelope fields are omitted. Any foreign CloudEvents
    extensions previously captured (via :func:`from_cloudevents`) are re-emitted.
    """
    wire = envelope.to_wire()

    ce: dict[str, Any] = {
        "specversion": _SPEC_VERSION,
        "id": wire["event_id"],
        "source": f"{_SOURCE_PREFIX}{wire['agent_id']}",
        "type": wire["event_type"],
        "time": wire["started_at"],
        "datacontenttype": _DATA_CONTENT_TYPE,
        "dataschema": _DATA_SCHEMA,
    }

    trace_id = wire.get("trace_id")
    span_id = wire.get("span_id")
    if trace_id and span_id:
        ce["traceparent"] = f"00-{trace_id}-{span_id}-01"

    # Required run-identifier extensions (always present on a valid envelope).
    ce["novarunglobal"] = wire["global_run_id"]
    ce["novarun"] = wire["run_id"]

    # Optional extensions — omitted when their source field is null.
    for field, ext in _OPTIONAL_EXT_MAP:
        value = wire.get(field)
        if value is not None:
            ce[ext] = value
    for field, ext in _BATCH_EXT_MAP:
        value = wire.get(field)
        if value is not None:
            ce[ext] = value

    # Re-emit foreign extensions captured on a prior from_cloudevents().
    foreign = wire.get(_FOREIGN_EXT_KEY)
    if isinstance(foreign, dict):
        for name, value in foreign.items():
            if name not in ce:
                ce[name] = value

    payload = wire.get("payload")
    if payload is not None:
        ce["data"] = payload

    return ce


def from_cloudevents(event: dict[str, Any]) -> EventEnvelope:
    """Reconstruct an ``EventEnvelope`` from a CloudEvents structured-mode object.

    Foreign extension attributes (not owned by this mapper) are preserved under
    the envelope's ``cloudevents_extensions`` extra block so a subsequent
    :func:`to_cloudevents` does not lose broker-injected metadata.

    Raises ``ValueError`` if a required attribute is missing or the ``source``
    does not match ``nova://agent/<id>``.
    """
    for required in ("id", "source", "type"):
        if required not in event or event[required] in (None, ""):
            raise ValueError(f"CloudEvent missing required attribute {required!r}")

    source = str(event["source"])
    if not source.startswith(_SOURCE_PREFIX):
        raise ValueError(
            f"unexpected CloudEvent source {source!r} (want {_SOURCE_PREFIX}<id>)"
        )
    agent_id = source[len(_SOURCE_PREFIX):]

    data: dict[str, Any] = {
        "envelope_version": "1",
        "schema_version": "event-envelope/1",
        "event_id": event["id"],
        "agent_id": agent_id,
        "event_type": event["type"],
        "started_at": event["time"],
        "global_run_id": event["novarunglobal"],
        "run_id": event["novarun"],
        # Null-by-default optionals; overwritten below when present.
        "parent_run_id": None,
        "cluster_id": None,
        "tenant_id": None,
    }

    traceparent = event.get("traceparent")
    if traceparent:
        match = _TRACEPARENT_RE.match(str(traceparent))
        if match:
            data["trace_id"], data["span_id"] = match.group(1), match.group(2)

    for field, ext in _OPTIONAL_EXT_MAP:
        if ext in event and event[ext] is not None:
            data[field] = event[ext]

    if "novabatchsig" in event and event["novabatchsig"] is not None:
        data["nova.batch.signature"] = event["novabatchsig"]
    if "novabatchkid" in event and event["novabatchkid"] is not None:
        data["nova.batch.signing_key_id"] = event["novabatchkid"]

    if "data" in event and event["data"] is not None:
        data["payload"] = event["data"]

    # Preserve foreign extension attributes (anything not reserved or owned).
    foreign = {
        name: value
        for name, value in event.items()
        if name not in _RESERVED_ATTRS and name not in _KNOWN_EXTENSIONS
    }
    if foreign:
        data[_FOREIGN_EXT_KEY] = foreign

    return EventEnvelope(**data)
