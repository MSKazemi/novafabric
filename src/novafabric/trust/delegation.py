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
"""Provable delegated authority — the "acted-as" delegation chain (ADR-0106 §NF-084).

A :class:`DelegationChain` is an ordered, signed record of an authority chain
``user → agent → sub-agent``: each hop is a :class:`Grant` in which a *granter*
delegates a *scope* of capabilities to a *grantee*, until an expiry, signed by the
granter's key. It is *evidence*, not an issuer — NovaFabric verifies a chain a
third party can re-check offline; it does not mint the underlying credentials.

:func:`verify_delegation_chain` enforces the four properties that make a chain
trustworthy — a verifier that skips any of them is worse than none:

1. **Authenticity** — every hop's signature verifies under its granter's public key.
2. **Linkage** — the key/identity a hop delegates *to* is exactly the key/identity
   that signs the next hop (no substituting a different key for the same name).
3. **Attenuation** — each hop's scope is a subset of its granter's scope; authority
   only ever narrows (a hop can never grant a capability its granter did not hold —
   this is the anti-privilege-escalation core).
4. **Freshness** — no grant is expired, and a child grant never outlives its parent.

Secrets are never part of a grant (ADR-0106 I-2): only public keys, identities,
scopes, expiries, optional public grant references, and signatures.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, Field


class DelegationError(Exception):
    """A delegation chain failed verification (bad signature, linkage, scope, or expiry)."""


class Principal(BaseModel):
    """An identity in a delegation chain: a name bound to an Ed25519 public key."""

    model_config = {"frozen": True}

    id: str = Field(..., min_length=1, description="Stable identity, e.g. 'agent:planner'")
    public_key: bytes = Field(..., description="Raw 32-byte Ed25519 public key")


class Grant(BaseModel):
    """One signed hop of a delegation chain (granter delegates scope to grantee)."""

    model_config = {"frozen": True}

    granter_id: str
    granter_public_key: bytes
    grantee_id: str
    grantee_public_key: bytes
    scope: list[str] = Field(..., description="Capabilities granted at this hop")
    not_after: str = Field(..., description="ISO-8601 UTC expiry of this grant")
    grant_ref: str | None = Field(
        default=None, description="Optional public reference to the backing VC/credential"
    )
    signature: bytes = Field(..., description="Ed25519 signature by the granter over the payload")


class DelegationChain(BaseModel):
    """An ordered chain of grants, root (user) first, leaf (acting agent) last."""

    grants: list[Grant]


class DelegationResult(BaseModel):
    """The outcome of verifying a chain: the acting principal and its effective scope."""

    model_config = {"frozen": True}

    effective_principal: Principal
    effective_scope: frozenset[str]


def _signing_payload(
    *,
    granter_id: str,
    granter_public_key: bytes,
    grantee_id: str,
    grantee_public_key: bytes,
    scope: list[str],
    not_after: str,
    grant_ref: str | None,
) -> bytes:
    """Deterministic bytes a granter signs (scope is order-independent)."""
    obj = {
        "granter_id": granter_id,
        "granter_public_key": granter_public_key.hex(),
        "grantee_id": grantee_id,
        "grantee_public_key": grantee_public_key.hex(),
        "scope": sorted(set(scope)),
        "not_after": not_after,
        "grant_ref": grant_ref,
    }
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def issue_grant(
    granter_key: Ed25519PrivateKey,
    *,
    granter: Principal,
    grantee: Principal,
    scope: set[str] | frozenset[str] | list[str],
    not_after: str,
    grant_ref: str | None = None,
) -> Grant:
    """Sign and return a :class:`Grant` from *granter* to *grantee* for *scope*.

    The signing key must correspond to ``granter.public_key`` (checked), so a
    grant can never be issued in a granter's name with an unrelated key.
    """
    if granter_key.public_key().public_bytes_raw() != granter.public_key:
        raise DelegationError("granter_key does not match granter.public_key")
    scope_list = sorted(set(scope))
    payload = _signing_payload(
        granter_id=granter.id,
        granter_public_key=granter.public_key,
        grantee_id=grantee.id,
        grantee_public_key=grantee.public_key,
        scope=scope_list,
        not_after=not_after,
        grant_ref=grant_ref,
    )
    return Grant(
        granter_id=granter.id,
        granter_public_key=granter.public_key,
        grantee_id=grantee.id,
        grantee_public_key=grantee.public_key,
        scope=scope_list,
        not_after=not_after,
        grant_ref=grant_ref,
        signature=granter_key.sign(payload),
    )


def _parse_expiry(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def verify_delegation_chain(
    chain: DelegationChain,
    *,
    trusted_roots: list[Principal],
    now: datetime | None = None,
) -> DelegationResult:
    """Verify a delegation chain end to end; return the effective principal + scope.

    Raises:
        DelegationError: on an empty chain, an untrusted root, a bad signature,
            broken key/identity linkage, scope escalation, or an expired /
            parent-outliving grant.
    """
    now = now or datetime.now(timezone.utc)
    grants = chain.grants
    if not grants:
        raise DelegationError("empty delegation chain: nothing to verify")

    root = grants[0]
    trusted = {(p.id, bytes(p.public_key)) for p in trusted_roots}
    if (root.granter_id, bytes(root.granter_public_key)) not in trusted:
        raise DelegationError(
            f"chain root granter {root.granter_id!r} is not a trusted anchor"
        )

    prev_scope: frozenset[str] | None = None
    prev_expiry: datetime | None = None
    prev_grantee_id: str | None = None
    prev_grantee_key: bytes | None = None

    for i, grant in enumerate(grants):
        # 1. Authenticity — signature verifies under the granter's public key.
        payload = _signing_payload(
            granter_id=grant.granter_id,
            granter_public_key=grant.granter_public_key,
            grantee_id=grant.grantee_id,
            grantee_public_key=grant.grantee_public_key,
            scope=list(grant.scope),
            not_after=grant.not_after,
            grant_ref=grant.grant_ref,
        )
        try:
            Ed25519PublicKey.from_public_bytes(bytes(grant.granter_public_key)).verify(
                bytes(grant.signature), payload
            )
        except (InvalidSignature, ValueError) as exc:
            raise DelegationError(f"hop {i}: grant signature is invalid") from exc

        # 2. Linkage — this hop's granter is exactly the prior hop's grantee.
        if i > 0:
            if grant.granter_id != prev_grantee_id or bytes(
                grant.granter_public_key
            ) != prev_grantee_key:
                raise DelegationError(
                    f"hop {i}: broken chain linkage — granter key/identity does not match "
                    "the previous hop's grantee"
                )

        # 3. Attenuation — scope only ever narrows.
        scope = frozenset(grant.scope)
        if prev_scope is not None and not scope <= prev_scope:
            escalated = sorted(scope - prev_scope)
            raise DelegationError(
                f"hop {i}: scope escalation — grants {escalated} beyond the granter's "
                "authority (attenuation violated)"
            )

        # 4. Freshness — not expired, and not outliving the parent.
        expiry = _parse_expiry(grant.not_after)
        if expiry <= now:
            raise DelegationError(f"hop {i}: grant expired at {grant.not_after}")
        if prev_expiry is not None and expiry > prev_expiry:
            raise DelegationError(
                f"hop {i}: grant outlives its parent (expires after the granting hop)"
            )

        prev_scope = scope
        prev_expiry = expiry
        prev_grantee_id = grant.grantee_id
        prev_grantee_key = bytes(grant.grantee_public_key)

    leaf = grants[-1]
    return DelegationResult(
        effective_principal=Principal(id=leaf.grantee_id, public_key=leaf.grantee_public_key),
        effective_scope=frozenset(leaf.scope),
    )


__all__ = [
    "DelegationChain",
    "DelegationError",
    "DelegationResult",
    "Grant",
    "Principal",
    "issue_grant",
    "verify_delegation_chain",
]
