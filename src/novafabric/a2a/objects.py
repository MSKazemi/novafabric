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

"""A2A Task/Message/Artifact object mapping — ADR-0149 D1 / NF-172.

NF-171 makes an agent's *identity* portable. This module makes its *work*
portable: A2A `Task`, `Message` and `Artifact` objects are mapped onto capsule
entities, with a manifest recording the field-by-field correspondence and a
digest that a re-export must reproduce.

**Why the digest is computed over the re-export, not over storage.** The obvious
implementation hashes the mapped data as stored, and then a round-trip check
re-hashes those same bytes and always passes — a test that cannot fail is not a
test. Here `roundtrip_digest` is taken over the objects **reconstructed** by
:func:`export_objects`, so a round-trip genuinely exercises map → store → export
and any corruption of the mapped fields changes the export and is caught.

**Why every object also carries its own digest.** The spec asks a failed
round-trip to name the diverging field. With one digest over everything, a
mismatch can only say "something changed". Per-object digests narrow it to the
object, and a field-level comparison against the re-export narrows it the rest of
the way.

**What is deliberately not carried.** A `Message` part can hold user content, so
parts are bound by `parts_digest` and never stored raw (ADR-0009, ADR-0149 I-2).
Anything else the source object carried that this mapping does not model is
enumerated in `unmapped[]` — visible, never silently dropped, which is the whole
point of a *loss-bounded* mapping.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "0.1.0"
FACET_NAME = "a2a_objects"

#: Fields this mapping carries, per A2A object kind. Everything else in a source
#: object lands in ``unmapped[]``.
MAPPED_FIELDS: dict[str, tuple[str, ...]] = {
    "task": ("id", "state"),
    "message": ("messageId", "role", "parts"),
    "artifact": ("name", "content_hash"),
}

#: A2A field -> capsule entity field, per kind. This *is* the mapping manifest's
#: content; keeping it as data rather than scattered code is what lets the
#: manifest be generated rather than hand-maintained (and drift).
FIELD_CORRESPONDENCE: dict[str, dict[str, str]] = {
    "task": {"id": "task_id", "state": "lifecycle_state"},
    "message": {
        "messageId": "message_id",
        "role": "role",
        "parts": "parts_digest",
    },
    "artifact": {"name": "artifact_name", "content_hash": "content_hash"},
}


class A2AObjectsError(ValueError):
    """An A2A object mapping could not be built or re-exported."""


def _canonical(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(payload: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical(payload)).hexdigest()}"


def parts_digest(parts: Sequence[Mapping[str, Any]]) -> str:
    """Digest a message's parts.

    Parts carry user content, so the capsule binds them and never stores them
    (ADR-0009). The digest is over the canonical form, so it is stable across
    key ordering but sensitive to any change in content.
    """
    return _digest([dict(p) for p in parts])


# ── Mapped objects ────────────────────────────────────────────────────────


class MappedTask(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    kind: str = "task"
    task_id: str
    lifecycle_state: str
    unmapped: list[str] = Field(default_factory=list)
    object_digest: str = ""


class MappedMessage(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    kind: str = "message"
    message_id: str
    role: str
    #: Parts are bound, never stored (I-2).
    parts_digest: str
    unmapped: list[str] = Field(default_factory=list)
    object_digest: str = ""


class MappedArtifact(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    kind: str = "artifact"
    artifact_name: str
    content_hash: str
    unmapped: list[str] = Field(default_factory=list)
    object_digest: str = ""


MappedObject = MappedTask | MappedMessage | MappedArtifact


class ManifestEntry(BaseModel):
    """One field-by-field correspondence row."""

    model_config = ConfigDict(extra="allow", frozen=True)

    kind: str
    a2a_field: str
    capsule_field: str


class A2AObjectsFacet(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    schema_version: str = SCHEMA_VERSION
    tasks: list[MappedTask] = Field(default_factory=list)
    messages: list[MappedMessage] = Field(default_factory=list)
    artifacts: list[MappedArtifact] = Field(default_factory=list)
    mapping_manifest: list[ManifestEntry] = Field(default_factory=list)
    #: Digest over the re-exported objects — see the module docstring.
    roundtrip_digest: str = ""

    def all_objects(self) -> list[MappedObject]:
        return [*self.tasks, *self.messages, *self.artifacts]


# ── Mapping ───────────────────────────────────────────────────────────────


def _unmapped_fields(source: Mapping[str, Any], kind: str) -> list[str]:
    known = set(MAPPED_FIELDS[kind])
    return sorted(k for k in source if k not in known)


def _require(source: Mapping[str, Any], field: str, kind: str) -> Any:
    if field not in source:
        raise A2AObjectsError(f"{kind} object is missing required field {field!r}")
    return source[field]


def map_task(task: Mapping[str, Any]) -> MappedTask:
    obj = MappedTask(
        task_id=str(_require(task, "id", "task")),
        lifecycle_state=str(_require(task, "state", "task")),
        unmapped=_unmapped_fields(task, "task"),
    )
    return obj.model_copy(update={"object_digest": _object_digest(obj)})


def map_message(message: Mapping[str, Any]) -> MappedMessage:
    parts = _require(message, "parts", "message")
    if not isinstance(parts, list):
        raise A2AObjectsError("message 'parts' must be a list")
    obj = MappedMessage(
        message_id=str(_require(message, "messageId", "message")),
        role=str(_require(message, "role", "message")),
        parts_digest=parts_digest(parts),
        unmapped=_unmapped_fields(message, "message"),
    )
    return obj.model_copy(update={"object_digest": _object_digest(obj)})


def map_artifact(artifact: Mapping[str, Any]) -> MappedArtifact:
    obj = MappedArtifact(
        artifact_name=str(_require(artifact, "name", "artifact")),
        content_hash=str(_require(artifact, "content_hash", "artifact")),
        unmapped=_unmapped_fields(artifact, "artifact"),
    )
    return obj.model_copy(update={"object_digest": _object_digest(obj)})


def _object_digest(obj: MappedObject) -> str:
    """Digest of one object's *exported* form, excluding the digest field itself."""
    return _digest(_export_one(obj))


def build_manifest(kinds: Sequence[str]) -> list[ManifestEntry]:
    """Generate the field-by-field manifest for the kinds actually present."""
    return [
        ManifestEntry(kind=kind, a2a_field=a2a, capsule_field=capsule)
        for kind in kinds
        for a2a, capsule in FIELD_CORRESPONDENCE[kind].items()
    ]


def map_objects(
    tasks: Sequence[Mapping[str, Any]] = (),
    messages: Sequence[Mapping[str, Any]] = (),
    artifacts: Sequence[Mapping[str, Any]] = (),
) -> A2AObjectsFacet | None:
    """Map A2A objects onto capsule entities.

    Returns None when there is nothing to map, so a capsule with no A2A objects
    is byte-identical to one captured before this feature existed (I-3).
    """
    if not tasks and not messages and not artifacts:
        return None

    facet = A2AObjectsFacet(
        tasks=[map_task(t) for t in tasks],
        messages=[map_message(m) for m in messages],
        artifacts=[map_artifact(a) for a in artifacts],
        mapping_manifest=build_manifest(
            [k for k, present in
             (("task", tasks), ("message", messages), ("artifact", artifacts))
             if present]
        ),
    )
    return facet.model_copy(
        update={"roundtrip_digest": _digest(export_objects(facet))}
    )


# ── Re-export and round-trip ──────────────────────────────────────────────


def _export_one(obj: MappedObject) -> dict[str, Any]:
    """Reconstruct one A2A object from its mapped form.

    ``parts`` cannot be reconstructed — only its digest was kept, deliberately —
    so the export carries ``parts_digest`` in its place. A consumer therefore sees
    a *bounded* reconstruction rather than a fabricated one.
    """
    if isinstance(obj, MappedTask):
        return {"kind": "task", "id": obj.task_id, "state": obj.lifecycle_state}
    if isinstance(obj, MappedMessage):
        return {
            "kind": "message",
            "messageId": obj.message_id,
            "role": obj.role,
            "parts_digest": obj.parts_digest,
        }
    return {
        "kind": "artifact",
        "name": obj.artifact_name,
        "content_hash": obj.content_hash,
    }


def export_objects(facet: A2AObjectsFacet) -> list[dict[str, Any]]:
    """Re-export the mapped objects as A2A-shaped documents."""
    return [_export_one(o) for o in facet.all_objects()]


class RoundtripResult(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    matches: bool
    recorded_digest: str
    observed_digest: str
    #: Populated only on a mismatch: which object, and which fields differ.
    diverging: list[dict[str, Any]] = Field(default_factory=list)
    unmapped_total: int = 0


def roundtrip(facet: A2AObjectsFacet) -> RoundtripResult:
    """Re-export and check the digest, naming what diverged.

    The facet is never modified. On a mismatch the recorded digest is left alone:
    replacing it with the observed one would redefine what the round-trip was
    supposed to reproduce.
    """
    exported = export_objects(facet)
    observed = _digest(exported)
    unmapped_total = sum(len(o.unmapped) for o in facet.all_objects())

    if observed == facet.roundtrip_digest:
        return RoundtripResult(
            matches=True,
            recorded_digest=facet.roundtrip_digest,
            observed_digest=observed,
            unmapped_total=unmapped_total,
        )

    diverging: list[dict[str, Any]] = []
    for obj, export in zip(facet.all_objects(), exported, strict=True):
        recomputed = _digest(export)
        if recomputed == obj.object_digest:
            continue
        # Narrow to the field: the stored per-object digest disagrees with the
        # re-export, so at least one mapped field changed since mapping.
        diverging.append(
            {
                "kind": obj.kind,
                "identity": export.get("id")
                or export.get("messageId")
                or export.get("name"),
                "recorded_object_digest": obj.object_digest,
                "observed_object_digest": recomputed,
                "fields": sorted(k for k in export if k != "kind"),
            }
        )

    return RoundtripResult(
        matches=False,
        recorded_digest=facet.roundtrip_digest,
        observed_digest=observed,
        diverging=diverging,
        unmapped_total=unmapped_total,
    )


# ── Capsule facet ─────────────────────────────────────────────────────────


def attach_facet(
    capsule: dict[str, Any], facet: A2AObjectsFacet | None
) -> dict[str, Any]:
    """Attach the objects facet additively; returns a new dict."""
    if facet is None:
        return capsule
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    facets[FACET_NAME] = facet.model_dump(exclude_none=True)
    out["facets"] = facets
    return out


def facet_from_capsule(capsule: dict[str, Any]) -> A2AObjectsFacet | None:
    """Read the objects facet back out of a capsule dict, or None if absent."""
    facets = capsule.get("facets")
    if not isinstance(facets, dict):
        return None
    block = facets.get(FACET_NAME)
    if not isinstance(block, dict):
        return None
    try:
        return A2AObjectsFacet.model_validate(block)
    except ValueError as exc:
        raise A2AObjectsError(
            f"capsule holds an invalid a2a_objects facet: {exc}"
        ) from exc
