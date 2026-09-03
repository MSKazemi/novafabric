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

"""Shared builders for the NF-161/162/163 provenance tests (ADR-0148 D1).

Lives outside a ``test_*`` module so two test files can share one capsule and one
manifest shape. Both shapes are taken from the real code, not invented: the manifest
store matches ``evidence/c2pa_exporter.py``, and the capsule's model-call keys match
``capture/media.py:_iter_record_media`` — a fixture with invented keys would let a
reader that finds nothing look like a reader that works.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from novafabric.trust.provenance.c2pa_bind import HARD_BINDING_LABEL, sidecar_path_for

IMAGE_BYTES = b"\x89PNG\r\n\x1a\n-fake-image-payload"
IMAGE_HEX = hashlib.sha256(IMAGE_BYTES).hexdigest()
IMAGE_HASH = f"sha256:{IMAGE_HEX}"

OTHER_BYTES = b"a completely different asset"
OTHER_HASH = f"sha256:{hashlib.sha256(OTHER_BYTES).hexdigest()}"


def a_manifest(
    claimed_hash: str | None = IMAGE_HASH,
    *,
    kind: str | None = None,
    signed: bool = False,
    active: str = "urn:manifest:1",
    resolvable: bool = True,
) -> dict[str, Any]:
    """A C2PA manifest store in the shape this repo's exporter already emits."""
    assertions: list[dict[str, Any]] = [{"label": "c2pa.ai.generated", "data": {}}]
    if claimed_hash is not None:
        assertions.append(
            {"label": HARD_BINDING_LABEL, "data": {"hash": claimed_hash, "alg": "sha256"}}
        )
    entry: dict[str, Any] = {"assertions": assertions}
    if signed:
        entry["signature_info"] = {
            "issuer": "CN=Camera Co",
            "cert_fingerprint": f"sha256:{'cc' * 32}",
            "alg": "ps256",
        }
    store: dict[str, Any] = {
        "active_manifest": active,
        "manifests": {(active if resolvable else "urn:manifest:other"): entry},
    }
    if kind is not None:
        store["manifest_kind"] = kind
    return store


def a_capsule(
    tmp_path: Path,
    *,
    media_hash: str = IMAGE_HASH,
    blob: bytes | None = None,
    sidecar: dict[str, Any] | None = None,
    on_response: bool = False,
) -> Path:
    """A capsule directory with one MediaPart, optionally with blob bytes + a sidecar."""
    capsule = tmp_path / "run_1"
    (capsule / "outputs").mkdir(parents=True)
    blob_ref: str | None = None
    if blob is not None:
        hex_digest = media_hash.split(":", 1)[-1]
        blob_ref = f"outputs/{hex_digest}.png"
        (capsule / blob_ref).write_bytes(blob)
    part = {
        "type": "image",
        "media": {
            "type": "image",
            "media_type": "image/png",
            "content_hash": media_hash,
            "byte_size": len(blob or b""),
            "redacted": False,
            "blob_ref": blob_ref,
        },
    }
    # The real capture keys, verified against capture/media.py:_iter_record_media —
    # request media lives under 'gen_ai.request.messages', response media (the NF-163
    # output side) under 'gen_ai.response.choices[].message.content'.
    record: dict[str, Any] = {"model_call_id": "call_1"}
    if on_response:
        record["gen_ai.response.choices"] = [
            {"message": {"role": "assistant", "content": [part]}}
        ]
    else:
        record["gen_ai.request.messages"] = [{"role": "user", "content": [part]}]
    (capsule / "model-calls.jsonl").write_text(json.dumps(record) + "\n")
    (capsule / "capsule.yaml").write_text("run_id: run_1\n")
    if sidecar is not None:
        sidecar_path_for(capsule, media_hash).write_text(json.dumps(sidecar))
    return capsule
