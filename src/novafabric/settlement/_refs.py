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

"""Reference validators shared by the ADR-0163 P3 blocks (NF-315/316/319).

Two reference kinds appear in P3 and they are deliberately different:

- a **digest** (``sha256:<64 hex>``) binds bytes held elsewhere — the
  settlement confirmation of a hop, the agreement artifact, the invoice;
- an **identity reference** names a party — an agent, a merchant, an invoice
  issuer — e.g. ``agent:acme-buyer``, ``did:web:shop.example`` or
  ``spiffe://acme.example/agent/buyer``. It is an identifier, never a key or
  a credential.

Every validator here is a ``fullmatch`` against a bounded pattern: an anchored
``^…$`` would accept a trailing newline, and an unbounded one would let a
producer stuff an artifact into a field meant for its name.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from novafabric.settlement.facet import InvalidReferenceError, SettlementFacet

#: A ``sha256:`` digest — the only form that binds bytes offline.
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")

#: Longest identity reference accepted. Far above any DID / SPIFFE id, and
#: small enough that an inlined artifact cannot pass for a name.
MAX_IDENTITY_REF_LEN = 256

#: An identity reference: printable, no whitespace, no quotes or brackets.
_IDENTITY_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@#+~%=-]{0,255}")

#: A URI whose authority carries ``user[:password]@`` embeds a credential.
_URI_USERINFO_RE = re.compile(r"[a-z][a-z0-9+.\-]*://[^/?#]*@", re.IGNORECASE)

#: Longest RFC-3339 instant accepted (``2026-07-13T10:02:11.123456+05:30`` is 32).
_MAX_INSTANT_LEN = 64


class InvalidIdentityRefError(Exception):
    """Raised when a party reference is not a bounded identifier.

    Not a ``ValueError``, for the reason given on
    :class:`~novafabric.settlement.facet.PaymentSecretRejectedError`: pydantic
    folds a ``ValueError`` into a generic validation error, and a reference
    carrying a credential must be seen as exactly that.
    """


def validate_digest(value: object, *, field: str) -> str:
    """Return ``value`` if it is a ``sha256:<64 hex>`` digest.

    Raises:
        InvalidReferenceError: for anything else, including a URI — a URI
            names a place, not bytes, and binds nothing offline.
    """
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        # The value is never echoed: the likeliest wrong value here is a raw
        # token or an inlined artifact, and this message travels into logs.
        raise InvalidReferenceError(
            f"{field} is not a 'sha256:<64 hex>' digest; artifact "
            "bytes, raw tokens and URIs are never stored here (ADR-0163 D1/D3)"
        )
    return value


def validate_identity_ref(value: object, *, field: str) -> str:
    """Return ``value`` if it is a bounded, credential-free identity reference.

    Raises:
        InvalidIdentityRefError: on a non-string, an over-long or oddly-shaped
            value, or a URI carrying ``user[:password]@`` userinfo.
    """
    if not isinstance(value, str):
        raise InvalidIdentityRefError(f"{field} must be a string identity reference")
    if len(value) > MAX_IDENTITY_REF_LEN or not _IDENTITY_REF_RE.fullmatch(value):
        raise InvalidIdentityRefError(
            f"{field} is not an identity reference (1-{MAX_IDENTITY_REF_LEN} chars "
            "of [A-Za-z0-9._:/@#+~%=-], starting alphanumeric); a party is named, "
            "never inlined (ADR-0163 D3)"
        )
    if _URI_USERINFO_RE.match(value):
        raise InvalidIdentityRefError(
            f"{field} is a URI carrying user[:password]@ userinfo; a reference "
            "must never embed a credential (ADR-0163 I-2, ADR-0009)"
        )
    return value


def validate_instant(value: object, *, field: str) -> str:
    """Return ``value`` if it is a timezone-aware RFC-3339 instant.

    An instant without an offset cannot be ordered against the network's
    confirmation times, so it is refused rather than guessed at.

    Raises:
        ValueError: on anything else (folded into a pydantic validation error —
            a malformed timestamp is a shape fault, not a leak).
    """
    if not isinstance(value, str) or len(value) > _MAX_INSTANT_LEN:
        raise ValueError(f"{field} must be an RFC-3339 string of <= {_MAX_INSTANT_LEN} chars")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} is not an RFC-3339 instant") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} has no UTC offset; RFC-3339 requires one")
    return value


def with_block(facet: SettlementFacet, key: str, value: Any) -> SettlementFacet:
    """Return a new facet with ``value`` stored under ``key`` (a P3 block).

    Re-validated through :class:`SettlementFacet`, so the facet-wide secret
    scan (ADR-0163 I-2) covers the new block. The input is not mutated.
    """
    data = facet.model_dump(exclude_none=True)
    data[key] = value
    return SettlementFacet.model_validate(data)
