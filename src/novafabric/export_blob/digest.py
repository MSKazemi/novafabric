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

"""Normative batch digest + canonical signing payload (spec batch-blob-export-v0).

The batch digest algorithm is normative so any implementation reproduces it:
sorted member triples, joined with newlines, SHA-256. Sorting by
``content_hash`` (ties by ``capsule_id``) makes the digest order-independent —
the same set of capsules always yields the same fingerprint regardless of
export order or resume interruptions. The empty batch digests the empty string.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from novafabric.export_blob.models import ExportMember


def sort_members(members: Sequence[ExportMember]) -> list[ExportMember]:
    """Members in canonical order: by ``content_hash``, ties by ``capsule_id``."""
    return sorted(members, key=lambda m: (m.content_hash, m.capsule_id))


def compute_batch_digest(members: Sequence[ExportMember]) -> str:
    """``sha256:<hex>`` over the canonical, sorted member triple list."""
    triples = [
        f"{m.capsule_id}\n{m.content_hash}\n{m.size}" for m in sort_members(members)
    ]
    joined = "\n".join(triples).encode("utf-8")
    return "sha256:" + hashlib.sha256(joined).hexdigest()


def canonical_signing_payload(
    *,
    schema_version: str,
    export_id: str,
    dest: str,
    members: Sequence[ExportMember],
    count: int,
    batch_digest: str,
) -> bytes:
    """Canonical-JSON bytes of the completeness-relevant fields (the DSSE payload).

    Covers ``{schema_version, export_id, dest, members, count, batch_digest}``
    exactly as stored in the manifest, so verifying the signature plus
    recomputing ``batch_digest`` proves the member list is authentic and
    internally consistent.
    """
    obj = {
        "schema_version": schema_version,
        "export_id": export_id,
        "dest": dest,
        "members": [m.model_dump(mode="json") for m in members],
        "count": count,
        "batch_digest": batch_digest,
    }
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
