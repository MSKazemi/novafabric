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

"""Typed models for ``export-manifest.json`` (ADR-0141, spec batch-blob-export-v0).

Mirrors ``schemas/export-manifest.schema.json`` (closed at the top level and per
member; arbitrary keys only inside ``extensions``).
"""

from __future__ import annotations

from typing import Any, Final, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

#: Frozen manifest schema version (spec batch-blob-export-v0).
SCHEMA_VERSION: Final = "0.1.0"

#: DSSE payload type of the signed export-manifest payload.
MANIFEST_PAYLOAD_TYPE = "application/vnd.novafabric.export-manifest+json"

#: Name of the manifest object written last at the destination.
MANIFEST_FILENAME = "export-manifest.json"


class ExportMember(BaseModel):
    """One batch member: a capsule referenced by id + content hash + size."""

    model_config = ConfigDict(extra="forbid")

    capsule_id: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    size: int = Field(ge=0)


class WormIntent(BaseModel):
    """Recorded ``--worm`` intent; enforcement is destination-side (ADR-0031)."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["governance", "compliance"]
    retain_until: str


class Producer(BaseModel):
    """The writer's identity, for forward-compat."""

    model_config = ConfigDict(extra="forbid")

    tool: Literal["novafabric"] = "novafabric"
    version: str = Field(min_length=1)


class DsseSignatureEntry(BaseModel):
    """One ``{keyid, sig}`` entry of a DSSE envelope."""

    model_config = ConfigDict(extra="allow")

    keyid: str = Field(min_length=1)
    sig: str = Field(min_length=1)


class DsseEnvelope(BaseModel):
    """NovaSeal / DSSE envelope over the completeness-relevant payload.

    ``payload`` (the base64 canonical payload) is optional in the schema: the
    verifier reconstructs the payload from the manifest fields, so a detached
    envelope verifies too.
    """

    model_config = ConfigDict(extra="allow")

    payloadType: str = Field(min_length=1)
    signatures: list[DsseSignatureEntry] = Field(min_length=1)
    payload: Optional[str] = None


class ExportManifest(BaseModel):
    """The signed pointer manifest — authority on batch membership and integrity."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0"] = SCHEMA_VERSION
    export_id: str = Field(pattern=r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
    created_at: str
    dest: str = Field(min_length=1)
    members: list[ExportMember]
    count: int = Field(ge=0)
    batch_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    signature: DsseEnvelope
    query: Optional[str] = None
    query_resolved_at: Optional[str] = None
    worm: Optional[WormIntent] = None
    producer: Optional[Producer] = None
    extensions: Optional[dict[str, Any]] = None

    def to_json_dict(self) -> dict[str, Any]:
        """JSON-ready dict; drops non-nullable optionals when unset.

        ``query`` / ``query_resolved_at`` / ``worm`` are nullable in the schema
        and stay present as ``null``; ``producer`` / ``extensions`` are not
        nullable and are dropped when unset.
        """
        data = self.model_dump(mode="json")
        for key in ("producer", "extensions"):
            if data.get(key) is None:
                data.pop(key, None)
        if data["signature"].get("payload") is None:
            data["signature"].pop("payload", None)
        return data
