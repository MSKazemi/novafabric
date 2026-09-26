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

"""Acted-on-behalf-of binding — ADR-0150 D4/P3 (NF-186).

An :class:`ActedOnBehalfRecord` joins a conversation turn to the NF-084
delegation hop under which the agent acted at that turn: ``turn_ref``,
``delegation_hop_ref`` and ``principal_ref``. It lets a reviewer walk from
"the agent said X in turn 7" to "under Alice's ``spend:usd<=100`` grant".

**Reference, never re-derivation (D4, Alternative 3).** This module does not
import, call, or re-implement NF-084 delegation-chain verification
(:mod:`novafabric.trust.delegation` owns it). :func:`resolve_hop` only *looks
up* the referenced hop in an NF-084 ``facets.delegation`` document
(``schemas/features/delegation-chain-v0.schema.json``) and *surfaces* the
verification state that NF-084's verifier recorded in that document's
``verified`` block — including ``broken_hop``. It never asserts authority
NF-084 did not establish:

- ``absent`` — no NF-084 document, or no hop matches ``delegation_hop_ref``;
- ``ambiguous`` — more than one hop matches;
- ``malformed`` — the NF-084 document could not be read as a v0 facet;
- ``unverified`` — the hop exists but NF-084 recorded no verification;
- ``broken`` — NF-084 recorded a ``broken_hop`` (surfaced verbatim) or a
  failed walk;
- ``principal_mismatch`` — the hop exists but the binding's ``principal_ref``
  is neither that hop's ``granter`` nor the chain root's ``granter`` (see
  :func:`principal_matches`); NF-084's recorded walk state is still surfaced
  in ``walk_ok`` / ``broken_hop``, but the binding is a defect;
- ``established`` — NF-084 recorded ``walk_ok: true`` and ``broken_hop: null``
  and the principal matches.

**What "established" means — and does not.** It is the verdict *as recorded
in the NF-084 document's own* ``verified`` *block*, which this module does not
authenticate: anyone who can write the document can write ``walk_ok: true``.
Every resolution therefore reports ``recorded_by: "nf084_document"`` and
``reverified: false``. Re-verification is not offered here: the
delegation-chain-v0 facet carries no granter/grantee public keys and no trust
anchors, which the NF-084 chain verifier in :mod:`novafabric.trust.delegation`
needs, so it could not be re-run from the document without inventing inputs.

``delegation_hop_ref`` is matched against the hop's NF-084 ``grant_ref`` — the
digest of the signed grant credential, NF-084's own identifier for the hop.
Storage: a list at ``facets.conversation.acted_on_behalf`` (the ADR-0150
storage deviation); several bindings may share a turn.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from novafabric.hitl._records import (
    LoadedRecords,
    RecordOutcome,
    check_digest,
    check_extras,
    check_turn_ref,
    load_records,
    record_fail_open,
)
from novafabric.hitl.conversation import IdentityRefError, _validate_identity_ref
from novafabric.hitl.handoff import party_key, same_party

RECORD_KEY = "acted_on_behalf"

#: The NF-084 facet name the hop reference points into.
DELEGATION_FACET = "delegation"

#: Bounds on the NF-084 document this module reads. A real acted-as chain is a
#: handful of hops; anything past these is refused as ``malformed`` rather than
#: walked.
MAX_HOPS = 256
MAX_SCOPE_TOKENS = 256
MAX_TOKEN_LENGTH = 256

HopState = Literal[
    "absent",
    "ambiguous",
    "malformed",
    "unverified",
    "broken",
    "principal_mismatch",
    "established",
]
PrincipalMatch = Literal["hop_granter", "chain_root", "none"]

#: Kind prefixes a delegation-chain identity may carry, mapped to the
#: ``principal_ref`` kind they denote. NF-084 examples use ``user:`` for people.
_DELEGATION_KINDS = {"user": "human", "human": "human", "agent": "agent"}

_ACTED_FIELDS = frozenset({"turn_ref", "delegation_hop_ref", "principal_ref"})


class ActedOnBehalfRecord(BaseModel):
    """NF-186: at ``turn_ref`` the agent acted under ``delegation_hop_ref``."""

    model_config = ConfigDict(extra="allow")

    turn_ref: str
    delegation_hop_ref: str
    principal_ref: str

    @model_validator(mode="before")
    @classmethod
    def _check_extras(cls, data: Any) -> Any:
        return check_extras(data, known=_ACTED_FIELDS)

    @field_validator("turn_ref", mode="before")
    @classmethod
    def _check_turn_ref(cls, v: object) -> str:
        return check_turn_ref(v)

    @field_validator("delegation_hop_ref", mode="before")
    @classmethod
    def _check_hop_ref(cls, v: object) -> str:
        return check_digest(v, field_name="delegation_hop_ref")

    @field_validator("principal_ref", mode="before")
    @classmethod
    def _check_principal(cls, v: object) -> str:
        ref = _validate_identity_ref(v, field="principal_ref")
        if not ref.startswith(("human:", "agent:")):
            raise IdentityRefError(
                "principal_ref must be a 'human:' or 'agent:' ref — the party on "
                "whose behalf the agent acted"
            )
        return ref


def record_acted_on_behalf(
    capsule: dict[str, Any], record: ActedOnBehalfRecord | Mapping[str, Any]
) -> RecordOutcome:
    """Attach a binding to ``facets.conversation.acted_on_behalf``; never raises.

    Recording does not consult NF-084 at all: the binding states which hop the
    capture site says was exercised; whether that hop exists and verified is
    read back, from NF-084, by :func:`resolve_hop`.
    """
    return record_fail_open(capsule, RECORD_KEY, ActedOnBehalfRecord, record, unique_per_turn=False)


def load_acted_on_behalf(capsule: Mapping[str, Any]) -> LoadedRecords[ActedOnBehalfRecord]:
    """Read every stored binding (malformed entries become defects)."""
    return load_records(capsule, RECORD_KEY, ActedOnBehalfRecord)


# ── NF-084 lookup (read-only, by reference) ───────────────────────────────


@dataclass(frozen=True)
class HopResolution:
    """What NF-084 says about the hop a binding references — surfaced, not judged."""

    state: HopState
    hop_index: int | None = None
    granter: str | None = None
    grantee: str | None = None
    scope: list[str] = field(default_factory=list)
    broken_hop: int | None = None
    walk_ok: bool | None = None
    detail: str | None = None
    principal_match: PrincipalMatch | None = None

    @property
    def established(self) -> bool:
        """True only when the NF-084 document records a clean walk and the principal matches.

        The walk verdict is the document's own, not re-verified here.
        """
        return self.state == "established"

    def to_dict(self) -> dict[str, Any]:
        """Deterministic JSON-ready form (the spec's ``nf084_hop_state``)."""
        return {
            "state": self.state,
            "present": self.state not in ("absent", "malformed"),
            "hop_index": self.hop_index,
            "granter": self.granter,
            "grantee": self.grantee,
            "scope": list(self.scope),
            "broken_hop": self.broken_hop,
            "walk_ok": self.walk_ok,
            "principal_match": self.principal_match,
            "recorded_by": "nf084_document",
            "reverified": False,
            "detail": self.detail,
        }


def delegation_from_capsule(capsule: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return ``facets.delegation`` when a capsule carries one, else None.

    The shipped ``run-capsule`` facet registry does not yet list
    ``delegation`` (NF-084 is future design), so today the document usually
    arrives separately — see ``nova hitl acted-as --delegation``.
    """
    facets = capsule.get("facets")
    if not isinstance(facets, Mapping):
        return None
    doc = facets.get(DELEGATION_FACET)
    return doc if isinstance(doc, Mapping) else None


def _bounded_str(value: object) -> str | None:
    if isinstance(value, str) and len(value) <= MAX_TOKEN_LENGTH:
        return value
    return None


def _read_verified(doc: Mapping[str, Any]) -> tuple[bool, bool | None, int | None, str | None]:
    """Return (present, walk_ok, broken_hop, error) from NF-084's ``verified``."""
    verified = doc.get("verified")
    if verified is None:
        return False, None, None, None
    if not isinstance(verified, Mapping):
        return True, None, None, "verified is not an object"
    walk_ok = verified.get("walk_ok")
    broken = verified.get("broken_hop")
    if walk_ok is not None and not isinstance(walk_ok, bool):
        return True, None, None, "verified.walk_ok is not a boolean"
    if broken is not None and (isinstance(broken, bool) or not isinstance(broken, int)):
        return True, None, None, "verified.broken_hop is not an integer or null"
    return True, walk_ok, broken, None


def principal_matches(principal_ref: str, identity: str | None) -> bool:
    """Return True when ``principal_ref`` names the NF-084 ``identity``.

    Both sides are compared in :func:`~novafabric.hitl.handoff.party_key` form
    (NFKC + case-fold). ``principal_ref`` is ``<kind>:<rest>`` with kind
    ``human`` or ``agent``. The NF-084 identity may carry a kind prefix
    (``user:`` / ``human:`` → human, ``agent:`` → agent): then the kinds must
    agree and the rests must be the same party (fingerprint refs compare by
    prefix, as in :func:`~novafabric.hitl.handoff.same_party`). An identity
    with no kind prefix (a bare DID or SPIFFE URI) matches when it equals
    ``<rest>``. Anything else — including a missing identity — does not match.
    """
    if identity is None:
        return False
    p_kind, _, p_rest = party_key(principal_ref).partition(":")
    ident = party_key(identity)
    head, sep, tail = ident.partition(":")
    kind = _DELEGATION_KINDS.get(head) if sep else None
    if kind is None:
        return ident == p_rest
    return kind == p_kind and same_party(f"{p_kind}:{p_rest}", f"{kind}:{tail}")


def _match_principal(
    principal_ref: str | None, chain: list[Any], hop: Mapping[str, Any]
) -> PrincipalMatch | None:
    if principal_ref is None:
        return None
    if principal_matches(principal_ref, _bounded_str(hop.get("granter"))):
        return "hop_granter"
    root = chain[0]
    root_granter = _bounded_str(root.get("granter")) if isinstance(root, Mapping) else None
    if principal_matches(principal_ref, root_granter):
        return "chain_root"
    return "none"


def resolve_hop(
    delegation: Mapping[str, Any] | None,
    delegation_hop_ref: str,
    *,
    principal_ref: str | None = None,
) -> HopResolution:
    """Look up ``delegation_hop_ref`` in an NF-084 document; never raises.

    Matches the reference against each hop's ``grant_ref`` and surfaces the
    ``verified`` state recorded in the NF-084 document (not re-verified). It
    performs no signature, linkage, attenuation or expiry check of its own —
    those are NF-084's, and doing them here would be the re-derivation
    ADR-0150 D4 rules out. When ``principal_ref`` is given it must match the
    hop's ``granter`` or the chain root's ``granter``
    (:func:`principal_matches`); otherwise the state is ``principal_mismatch``.
    """
    if delegation is None:
        return HopResolution("absent", detail="no NF-084 delegation document")
    chain = delegation.get("chain")
    if not isinstance(chain, list) or not chain:
        return HopResolution("malformed", detail="delegation.chain is not a non-empty list")
    if len(chain) > MAX_HOPS:
        return HopResolution("malformed", detail=f"delegation.chain exceeds {MAX_HOPS} hops")
    matches = [
        (pos, hop)
        for pos, hop in enumerate(chain)
        if isinstance(hop, Mapping) and hop.get("grant_ref") == delegation_hop_ref
    ]
    if not matches:
        return HopResolution("absent", detail="no NF-084 hop has this grant_ref")
    if len(matches) > 1:
        return HopResolution("ambiguous", detail=f"{len(matches)} NF-084 hops share this grant_ref")
    pos, hop = matches[0]
    raw_scope = hop.get("scope")
    if not isinstance(raw_scope, list) or len(raw_scope) > MAX_SCOPE_TOKENS:
        return HopResolution("malformed", hop_index=pos, detail="hop scope is not a bounded list")
    scope = [s for s in (_bounded_str(item) for item in raw_scope) if s is not None]
    if len(scope) != len(raw_scope):
        return HopResolution("malformed", hop_index=pos, detail="hop scope has a non-string token")
    index = hop.get("hop", pos)
    if isinstance(index, bool) or not isinstance(index, int):
        return HopResolution("malformed", hop_index=pos, detail="hop index is not an integer")
    base: dict[str, Any] = {
        "hop_index": index,
        "granter": _bounded_str(hop.get("granter")),
        "grantee": _bounded_str(hop.get("grantee")),
        "scope": scope,
    }
    present, walk_ok, broken, error = _read_verified(delegation)
    if error is not None:
        return HopResolution("malformed", detail=error, **base)
    match = _match_principal(principal_ref, chain, hop)
    base["principal_match"] = match
    if not present:
        state: HopState = "unverified"
        detail = "NF-084 recorded no verification"
    elif broken is not None or walk_ok is not True:
        state, detail = "broken", "NF-084 did not establish this chain"
    else:
        state, detail = "established", ""
    if match == "none":
        note = "principal_ref matches neither this hop's granter nor the chain root's granter"
        detail = f"{note}; NF-084 walk: {state}"
        state = "principal_mismatch"
    return HopResolution(state, broken_hop=broken, walk_ok=walk_ok, detail=detail or None, **base)


@dataclass(frozen=True)
class ActedAsView:
    """One stored binding with its NF-084 resolution, for a reader."""

    index: int
    record: ActedOnBehalfRecord
    resolution: HopResolution

    def to_dict(self) -> dict[str, Any]:
        """Deterministic JSON-ready form."""
        return {
            "index": self.index,
            **self.record.model_dump(exclude_none=True),
            "nf084_hop_state": self.resolution.to_dict(),
        }


def bindings_for_turn(
    capsule: Mapping[str, Any],
    turn_ref: str,
    delegation: Mapping[str, Any] | None,
) -> tuple[list[ActedAsView], LoadedRecords[ActedOnBehalfRecord]]:
    """Return the turn's bindings, each resolved against ``delegation``."""
    loaded = load_acted_on_behalf(capsule)
    views = [
        ActedAsView(
            i,
            rec,
            resolve_hop(delegation, rec.delegation_hop_ref, principal_ref=rec.principal_ref),
        )
        for i, rec in loaded.records
        if rec.turn_ref == turn_ref
    ]
    return views, loaded
