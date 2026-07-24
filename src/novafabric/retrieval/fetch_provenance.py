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

"""Web-fetch provenance — ADR-0153 D4 / P1 (NF-218).

Records a fetch **the agent performed**: where it ended up, the redirects it
followed, the TLS leaf certificate it was served, and the digest of the body
that came back. This is what lets an auditor tell "the agent read
``https://news.example/report``" from "the agent was redirected three times and
read something else entirely".

Four invariants from ADR-0153 shape every choice in this module:

- **I-1 Record-only / never fetches.** NovaFabric records a fetch that already
  happened. It never fetches or re-fetches on its own behalf — this module
  imports no HTTP client, and verification is a pure function of the record.
- **I-2 No payloads, no credentials.** The body is a ``sha256:`` digest; the
  certificate is a leaf fingerprint, never a key. Cookies, bearer tokens, API
  keys and private keys must never reach the capsule through this path, and
  the guards below refuse them loudly rather than dropping them silently.
- **I-3 Fail-open / additive-first.** The facet lives in optional
  ``facets.fetch_provenance``; a run with no fetches is byte-identical to one
  captured before this feature existed.
- **I-4 Not adjudicated.** A recorded fetch says what was served. It does not
  say the source was authoritative, the TLS chain was trusted, or the use was
  lawful.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

FACET_NAME = "fetch_provenance"
SCHEMA_VERSION = "0.1.0"

#: Extra keys that must never appear on a fetch record. `extra="allow"` keeps
#: the model forward-compatible with later ADR-0153 phases, but that same
#: openness is what would let a caller park a session cookie in the capsule,
#: so the credential-shaped names are denied by name (I-2, ADR-0009).
_FORBIDDEN_EXTRA_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "bearer",
        "body",
        "content",
        "cookie",
        "cookies",
        "credentials",
        "password",
        "private_key",
        "secret",
        "session",
        "set_cookie",
        "token",
    }
)

#: PEM armour for private-key material of any flavour.
_PRIVATE_KEY_MARKER = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")

_SHA256_REF = re.compile(r"^sha256:[0-9a-f]{64}$")


# Deliberately NOT subclasses of ValueError. Pydantic v2 converts ValueError
# raised inside a validator into a ValidationError, which would bury the named
# error these classes exist to surface; any other exception type propagates
# unchanged. So a caller sees the same named exception whether the guard trips
# at model construction or in `record_fetch`.
class ForbiddenFetchFieldError(Exception):
    """Raised when a fetch record carries credential-shaped material (I-2)."""


class SecretMaterialError(Exception):
    """Raised when key material is offered where a fingerprint belongs (I-2)."""


class InvalidRedirectChainError(Exception):
    """Raised when a redirect chain is not a redirect chain."""


class RedirectHop(BaseModel):
    """One hop the fetch was redirected through, in the order it happened."""

    model_config = ConfigDict(extra="allow")

    url: str
    status: int


class FetchProvenance(BaseModel):
    """One agent-performed web fetch (D4)."""

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    #: Where the fetch ended up after following ``redirect_chain``.
    final_url: str
    #: Ordered hops that led to ``final_url``; empty when there was no redirect.
    redirect_chain: list[RedirectHop] = Field(default_factory=list)
    #: Status of the final response (not of the redirects).
    http_status: int
    fetched_at: str
    #: ``sha256:`` of the DER-encoded leaf certificate. Never a key (I-2).
    tls_cert_fingerprint: str | None = None
    #: ``sha256:`` of the response body. The body itself is not stored (I-2).
    body_content_hash: str | None = None
    #: RFC-3161 timestamp digest / WACZ / Memento URI the *agent* produced.
    #: NovaFabric references an archive; it does not create one.
    archival_ref: str | None = None

    @model_validator(mode="after")
    def _guard_credentials(self) -> FetchProvenance:
        for key in self.model_extra or {}:
            if key.lower() in _FORBIDDEN_EXTRA_KEYS:
                raise ForbiddenFetchFieldError(
                    f"fetch record carries credential-shaped field {key!r}; "
                    "fetch provenance is digests, URLs and fingerprints only "
                    "(ADR-0153 I-2)"
                )
        for url in (self.final_url, *(hop.url for hop in self.redirect_chain)):
            # A userinfo component (https://user:pass@host) IS a credential, and
            # unlike a query parameter it is unambiguously one. Query strings are
            # kept verbatim: the exact URL fetched is the evidence, and silently
            # rewriting it would break the very identity the record establishes.
            if urlsplit(url).username is not None:
                raise ForbiddenFetchFieldError(
                    f"URL {url!r} embeds userinfo credentials; strip them "
                    "before recording the fetch (ADR-0153 I-2)"
                )
        for hop in self.redirect_chain:
            if not 300 <= hop.status <= 399:
                raise InvalidRedirectChainError(
                    f"redirect hop {hop.url!r} has non-redirect status "
                    f"{hop.status}; a hop that did not redirect does not belong "
                    "in the chain"
                )
        if self.tls_cert_fingerprint and not _SHA256_REF.match(
            self.tls_cert_fingerprint
        ):
            raise SecretMaterialError(
                "tls_cert_fingerprint must be a sha256:<hex> digest of the leaf "
                "certificate; raw certificate or key material is never stored"
            )
        return self


def digest_body(content: str | bytes) -> str:
    """Return the ``sha256:`` digest of a fetched response body."""
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def fingerprint_leaf_certificate(der: bytes) -> str:
    """Return the ``sha256:`` fingerprint of a DER-encoded leaf certificate.

    Raises:
        SecretMaterialError: if the input contains PEM private-key armour. A
            caller passing a key here has confused a public certificate with a
            private one, and hashing it anyway would put key-derived material
            in the capsule.
    """
    if _PRIVATE_KEY_MARKER.search(der.decode("latin-1", errors="ignore")):
        raise SecretMaterialError(
            "private-key material offered where a leaf certificate is expected; "
            "fingerprint the certificate, never the key (ADR-0153 I-2)"
        )
    return f"sha256:{hashlib.sha256(der).hexdigest()}"


def record_fetch(
    *,
    final_url: str,
    http_status: int,
    fetched_at: str,
    redirect_chain: Iterable[tuple[str, int]] = (),
    tls_cert_fingerprint: str | None = None,
    body_content_hash: str | None = None,
    archival_ref: str | None = None,
) -> FetchProvenance:
    """Build a fetch record from what an agent's HTTP client observed.

    Takes the redirect chain as plain ``(url, status)`` pairs so an integration
    can hand over what its client already exposes without importing this
    module's models.
    """
    return FetchProvenance(
        final_url=final_url,
        redirect_chain=[RedirectHop(url=u, status=s) for u, s in redirect_chain],
        http_status=http_status,
        fetched_at=fetched_at,
        tls_cert_fingerprint=tls_cert_fingerprint,
        body_content_hash=body_content_hash,
        archival_ref=archival_ref,
    )


class FetchProvenanceFacet(BaseModel):
    """The optional ``facets.fetch_provenance`` block (I-3)."""

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    fetches: list[FetchProvenance] = Field(default_factory=list)


def build_facet(fetches: Iterable[FetchProvenance]) -> FetchProvenanceFacet:
    """Assemble the facet from fetch records, ordered by fetch time."""
    return FetchProvenanceFacet(fetches=sorted(fetches, key=lambda f: f.fetched_at))


def attach_facet(
    capsule: dict[str, Any], facet: FetchProvenanceFacet
) -> dict[str, Any]:
    """Attach the fetch-provenance facet to a capsule dict, additively.

    Writes nothing when there are no fetches: a run that fetched nothing must
    be byte-identical to one captured before this feature existed (I-3).
    Returns a new dict; the input is not mutated.
    """
    if not facet.fetches:
        return capsule
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    facets[FACET_NAME] = facet.model_dump(exclude_none=True)
    out["facets"] = facets
    return out


# ── Verification ──────────────────────────────────────────────────────────


def verify_body_binding(record: FetchProvenance, body: str | bytes) -> bool:
    """Re-verify a fetch record's body digest against the bytes it claims.

    Returns False for a record with no ``body_content_hash``. An unbound fetch
    is not "trivially valid" — it is the case ADR-0153 wants surfaced, and
    returning True would let a record with no binding pass a verifier that
    exists to check bindings.
    """
    if not record.body_content_hash:
        return False
    return record.body_content_hash == digest_body(body)
