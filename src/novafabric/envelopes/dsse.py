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

"""DSSE-wrap an Evidence Bundle for stock ``cosign`` verification (NF-029, ADR-0096).

The canonical bundle bytes become the DSSE ``payload`` verbatim (wrap, don't replace) so
``cosign verify-blob-attestation`` can verify the envelope with no NovaFabric dependency.
Reuses the single DSSE writer in :mod:`novafabric.evidence.intoto` — there is no second
DSSE code path (requirement 2).
"""

from __future__ import annotations

import base64
from typing import Any

from novafabric.evidence.intoto import dsse_sign_payload, dsse_verify

#: Media type of the DSSE payload for an Evidence Bundle (requirement 1).
BUNDLE_PAYLOAD_TYPE = "application/vnd.novafabric.bundle+json"


def wrap_bundle(
    bundle_bytes: bytes, signer: Any, payload_type: str = BUNDLE_PAYLOAD_TYPE
) -> dict[str, Any]:
    """Return a signed DSSE envelope whose payload is *bundle_bytes* verbatim.

    ``signer`` must expose ``sign(bytes) -> bytes`` and ``keyid: str`` (the NovaSeal
    signer). The inner bundle bytes are never rewritten — they are base64-encoded as the
    DSSE payload.
    """
    return dsse_sign_payload(bundle_bytes, payload_type, signer)


def unwrap_bundle(envelope: dict[str, Any]) -> bytes:
    """Return the raw bundle bytes from a DSSE envelope (no signature check)."""
    return base64.b64decode(envelope["payload"])


def verify_bundle_envelope(envelope: dict[str, Any], verify_fn: Any) -> bytes:
    """Verify a bundle DSSE envelope and return the raw bundle bytes.

    ``verify_fn(pae_bytes, signature_bytes) -> bool`` performs the cryptographic check
    (e.g. an Ed25519 public-key verify). Raises ``ValueError`` on signature failure.
    Reuses :func:`novafabric.evidence.intoto.dsse_verify` for the PAE + signature check.
    """
    dsse_verify(envelope, verify_fn)
    return unwrap_bundle(envelope)
