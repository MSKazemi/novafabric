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

"""Signed dataset provenance cards (NF-058, ADR-0105).

A dataset provenance card records a dataset's ``source``, ``version``, content ``hash``,
and **transform history** as a first-class, signed artifact that feeds both the AI-BOM
(NF-056) and the SLSA-for-ML attestation (NF-057). Each ``transformHistory`` entry
references a content-addressed operation digest (the ADR-0090/0109 signed transform
facet that produced the derivation) — **never** raw values, prompts, or cell contents
(I-4). The card is Ed25519-signed reusing the existing NovaSeal/Evidence-Bundle keyring
path (`novafabric.trust.keyring`) — no new crypto dependency; the signature is taken over
the canonical-JSON card body *excluding* the signature block, so an unsigned card is not
evidence.

Validates against ``schemas/features/dataset-provenance-card-v0.schema.json``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field

from novafabric.trust.keyring import sign_payload, verify_sig

CARD_SCHEMA_VERSION = "dataset-provenance-card/1"

# Lineage edge types that represent a dataset derivation (contribute a transform entry).
_DERIVATION_EDGE_TYPES = {"produced_by", "transformed", "derived", "consumed"}


class _Camel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class Source(_Camel):
    uri: str
    retrieved_at: str | None = Field(default=None, alias="retrievedAt")


class DigestMap(_Camel):
    sha256: str


class Transform(_Camel):
    op: str
    signed_op_digest: str = Field(alias="signedOpDigest")
    op_type: str | None = Field(default=None, alias="opType")
    produced_edge: str | None = Field(default=None, alias="producedEdge")


class BoundTo(_Camel):
    registry_digest: str = Field(alias="registryDigest")
    capsule_digest: str | None = Field(default=None, alias="capsuleDigest")


class Signature(_Camel):
    keyid: str
    alg: str = "ed25519"
    sig: str


class DatasetProvenanceCard(_Camel):
    """A dataset provenance card (NF-058). ``signature`` is ``None`` until signed."""

    schema_version: str = Field(default=CARD_SCHEMA_VERSION, alias="schemaVersion")
    asset: str
    source: Source
    version: str
    hash: DigestMap
    license: str | None = None
    tlp: str | None = None
    transform_history: list[Transform] = Field(default_factory=list, alias="transformHistory")
    bound_to: BoundTo | None = Field(default=None, alias="boundTo")
    signature: Signature | None = None

    def to_json_dict(self) -> dict[str, Any]:
        """Schema-shaped (camelCase) dict, dropping unset optionals."""
        return self.model_dump(by_alias=True, exclude_none=True)


def _content_hash(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_card_bytes(card: DatasetProvenanceCard) -> bytes:
    """Canonical-JSON bytes of *card* excluding its signature block (signing input)."""
    body = card.to_json_dict()
    body.pop("signature", None)
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def transforms_from_capsule(capsule_dir: str | Path) -> list[Transform]:
    """Derive a transform history from a capsule's ``lineage.jsonl`` derivation edges.

    Each derivation edge becomes a :class:`Transform` whose ``signedOpDigest`` is the
    content hash of the (canonicalized) edge record — a content-addressed reference to
    the recorded operation. Only metadata (op type, edge id) is copied; never values.
    """
    capsule_dir = Path(capsule_dir)
    lineage = capsule_dir / "lineage.jsonl"
    transforms: list[Transform] = []
    if not lineage.exists():
        return transforms
    for line in lineage.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            edge: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            continue
        edge_type = str(edge.get("edge_type", ""))
        if edge_type not in _DERIVATION_EDGE_TYPES:
            continue
        digest = _content_hash(
            json.dumps(edge, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        transforms.append(
            Transform(
                op=edge_type,
                signedOpDigest=digest,
                opType=str(edge.get("op_type")) if edge.get("op_type") else None,
                producedEdge=str(edge.get("edge_id")) if edge.get("edge_id") else None,
            )
        )
    return transforms


def build_card(
    *,
    asset: str,
    source_uri: str,
    version: str,
    dataset_hash: str,
    retrieved_at: str | None = None,
    license: str | None = None,
    tlp: str | None = None,
    transforms: list[Transform] | None = None,
    registry_digest: str | None = None,
    capsule_digest: str | None = None,
) -> DatasetProvenanceCard:
    """Build an **unsigned** dataset provenance card (NF-058)."""
    bound: BoundTo | None = None
    if registry_digest is not None:
        bound = BoundTo(
            registryDigest="sha256:" + registry_digest.removeprefix("sha256:"),
            capsuleDigest=(
                "sha256:" + capsule_digest.removeprefix("sha256:")
                if capsule_digest
                else None
            ),
        )
    return DatasetProvenanceCard(
        asset=asset,
        source=Source(uri=source_uri, retrievedAt=retrieved_at),
        version=version,
        hash=DigestMap(sha256=dataset_hash.removeprefix("sha256:")),
        license=license,
        tlp=tlp,
        transformHistory=transforms or [],
        boundTo=bound,
    )


def sign_card(
    card: DatasetProvenanceCard, private_key: Ed25519PrivateKey, keyid: str
) -> DatasetProvenanceCard:
    """Return a copy of *card* with an Ed25519 signature over its canonical body."""
    sig = sign_payload(private_key, canonical_card_bytes(card))
    signed = card.model_copy(deep=True)
    signed.signature = Signature(keyid=keyid, alg="ed25519", sig=sig)
    return signed


def verify_card(card: DatasetProvenanceCard, public_key: Ed25519PublicKey) -> bool:
    """True iff the card carries a signature that verifies over its canonical body."""
    if card.signature is None:
        return False
    return verify_sig(public_key, card.signature.sig, canonical_card_bytes(card))
