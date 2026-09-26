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

"""Fleet-shared-memory access-governance ledger — ADR-0171 D2/P2 (NF-392).

Records, per shared-store access, *which agent read or wrote which entry, and
whether that access fell inside the scope the agent declared*::

    {agent, store_id, namespace, entry_id, access, allowed_scope, contained}

``contained: false`` is **evidence of an out-of-scope access, never
enforcement.** Nothing in this module gates, blocks, delays, or rejects an
access because it is out of scope: an out-of-scope access is *recorded*, with
the flag set, exactly as faithfully as an in-scope one (ADR-0171 I-4,
[F10-003][F10-004]). The only inputs this module ever rejects are malformed
evidence — a missing identifier, a smuggled payload — never an access.

``contained`` is **computed**, not accepted: :func:`record_access` derives it
from ``allowed_scope`` and the entry's namespace/id, and
:func:`verify_scope_flags` recomputes it for every stored row, so a row forged
to read ``contained: true`` for an out-of-scope access is caught offline.

Scope grammar (deliberately small and exact)
--------------------------------------------
``allowed_scope`` is one or more comma-separated patterns:

- ``*`` — every entry of the store.
- ``<namespace>`` — every entry of exactly that namespace.
- ``<namespace>/<entry-glob>`` — entries of exactly that namespace whose id
  matches the case-sensitive ``fnmatch`` glob (``playbooks/*``,
  ``playbooks/pb-44*``). The pattern is split on its **last** ``/``, so a
  namespace may itself contain ``/``.

The namespace part is **never** a glob. A cross-namespace access is the
defining NF-392 finding, and a namespace glob (``play*``) would silently widen
a scope across namespaces the declarer may never have meant to include.

Storage
-------
The block lives *inside* the existing ``facets.memstore_mutation`` facet as its
``access`` key (the facet is ``extra="allow"``), so no capsule-schema change is
needed. Like the NF-391 ledger, the access ledger is an ordered **hash chain**
(``prev_record_hash``) over canonical JSON, so a dropped, reordered, or edited
row is detectable offline at a reported index.

Composition, not re-capture: :func:`access_kwargs_from_memory_event` lifts a
shipped C2 ``MemoryOperationEvent`` (ADR-0143) into access-record arguments
using its key, agent, run and claimed origin only — never its opt-in ``value``.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from novafabric.memstore.ledger import (
    FACET_NAME,
    MAX_ID_LENGTH,
    MAX_TIMESTAMP_LENGTH,
    ContentCaptureError,
    LedgerVerification,
    MemstoreError,
    _validate_digest,
    _validate_id,
)

if TYPE_CHECKING:
    from novafabric.capture.events import MemoryOperationEvent

ACCESS_BLOCK = "access"
ACCESS_SCHEMA_VERSION = "0.1.0"

#: Printed by every CLI output of this cluster (spec §2, I-4).
IN_MISSION_BOUNDARY = (
    "NovaFabric records evidence about the shared store; it never hosts, serves, "
    "manages, or gates it. contained:false is evidence of an out-of-scope access, "
    "not enforcement."
)

#: Bounds. An access ledger is per-run; ten thousand accesses from one run to
#: one store is already an unusual workload, and an unbounded list is an
#: unbounded allocation when an untrusted capsule is loaded.
MAX_ACCESS_RECORDS = 10_000
MAX_SCOPE_LENGTH = 512
MAX_SCOPE_PATTERNS = 32
#: Depth to which extra (``extra="allow"``) keys are inspected for payloads.
_MAX_EXTRA_DEPTH = 4
_MAX_EXTRA_LIST = 64
#: Extra keys allowed in any one mapping. Declared fields do not count. Without
#: a key cap, a key-name blocklist is no defence at all: two thousand
#: innocuously-named keys (``notes_0001`` …) each carrying a sub-limit string
#: reassemble an entry's text in full.
MAX_EXTRA_KEYS = 32
#: Cap on the canonical-JSON size of *all* extras of one record, in bytes. The
#: per-key and per-string caps bound each piece; this bounds their sum.
MAX_EXTRA_BYTES = 8 * 1024
#: A value under an ``…id`` / ``…ref`` key must look like one identifier.
MAX_REFERENCE_LENGTH = 128

AccessKind = Literal["read", "write"]

# ── Errors ────────────────────────────────────────────────────────────────


class ScopeError(MemstoreError):
    """Raised when an ``allowed_scope`` declaration is malformed.

    A malformed *declaration* is a caller mistake. An access that falls outside
    a well-formed declaration is never an error — it is ``contained: false``.
    """


class StoreMismatchError(MemstoreError):
    """Raised when an access block would be attached to another store's facet."""


# ── Payload guard (I-2) ───────────────────────────────────────────────────

#: Substring markers of content-bearing keys, matched against the *normalised*
#: key (lower-cased, non-alphanumerics stripped), so ``Entry-Text``,
#: ``entry_text`` and ``ENTRYTEXT`` are one key.
_CONTENT_MARKERS: tuple[str, ...] = (
    "content",
    "text",
    "body",
    "value",
    "embedding",
    "vector",
    "prompt",
    "response",
    "completion",
    "payload",
    "raw",
    "document",
    "chunk",
    "snippet",
    "excerpt",
    "message",
    "secret",
    "password",
    "token",
    "pii",
)
#: Markers of *credential*-bearing keys. Unlike the content markers above, a
#: reference-shaped suffix never exempts these: ``api_secret_ref`` is far more
#: often a secret under a reference's name than a pointer to one, and the access
#: ledger has no need to point at credentials at all (ADR-0009).
_CREDENTIAL_MARKERS: tuple[str, ...] = (
    "secret",
    "password",
    "passwd",
    "credential",
    "apikey",
    "privatekey",
)
#: A key ending in one of these names a reference, and is allowed provided its
#: *value* is shape-valid for that suffix — see :func:`_guard_reference`.
_DIGEST_SUFFIXES: tuple[str, ...] = ("digest", "hash")
_IDENT_SUFFIXES: tuple[str, ...] = ("id", "ref")
_COUNT_SUFFIXES: tuple[str, ...] = ("count",)
_REFERENCE_SUFFIXES: tuple[str, ...] = _IDENT_SUFFIXES + _DIGEST_SUFFIXES + _COUNT_SUFFIXES
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
#: One identifier: a ULID, a slug, a pod name, a path-ish key. No whitespace, so
#: a sentence cannot pass as an id however short it is. Applied with fullmatch.
_IDENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@#+=-]{0,127}")
_EXTRA_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
#: Credential prefixes the ADR-0009 pack does not cover because they contain a
#: separator its ``sk-<alnum>`` rule stops at (Stripe-style ``sk_live_…``,
#: ``sk-live-…``). A supplement to the pack, never a replacement for it.
_EXTRA_CREDENTIAL_RE = re.compile(r"(?i)\b(?:sk|rk|pk)[-_](?:live|test|prod)[-_][A-Za-z0-9]{8,}")


def normalise_key(key: str) -> str:
    """Return *key* lower-cased with every non-alphanumeric character removed."""
    return _NON_ALNUM.sub("", key.lower())


def _is_reference_key(norm: str) -> bool:
    return any(norm.endswith(suffix) for suffix in _REFERENCE_SUFFIXES)


def _scan_secret(value: str, *, path: str) -> None:
    """Raise if *value* matches the ADR-0009 secret pack (or its supplement).

    Only rule ids ever reach the message — never the matched text.
    """
    from novafabric.capture.secrets import scan_text_rule_ids

    rules = scan_text_rule_ids(value)
    if not rules and _EXTRA_CREDENTIAL_RE.search(value):
        rules = ["credential-prefix"]
    if rules:
        raise ContentCaptureError(
            f"{path} matches secret rule(s) {', '.join(rules)}; a credential never "
            "enters the access ledger (ADR-0009, ADR-0171 I-2)"
        )


def _guard_scalar(value: object, *, path: str) -> None:
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise ContentCaptureError(f"{path} carries raw bytes; digests/ids only (I-2)")
    if isinstance(value, str):
        if len(value) > MAX_ID_LENGTH:
            raise ContentCaptureError(
                f"{path} is {len(value)} chars, over the {MAX_ID_LENGTH}-char limit; "
                "this looks like inlined entry content (ADR-0171 I-2)"
            )
        _scan_secret(value, path=path)


def _guard_reference(value: object, *, norm: str, path: str) -> None:
    """Admit *value* under a reference-suffixed key only if it has that shape.

    The suffix is a claim; the value must honour it. ``…digest`` / ``…hash``
    takes exactly ``sha256:<64 hex>``; ``…id`` / ``…ref`` one short
    identifier (or an int); ``…count`` a non-negative int. ``None`` is always
    allowed — an absent reference is not a payload.
    """
    if value is None:
        return
    if isinstance(value, (Mapping, list, tuple)):
        # A reference names one thing; a structure under a reference key is
        # a payload wearing an id's name.
        raise ContentCaptureError(f"{path!r} must be a single scalar reference")
    _guard_scalar(value, path=path)
    is_int = isinstance(value, int) and not isinstance(value, bool)
    if norm.endswith(_DIGEST_SUFFIXES):
        ok = isinstance(value, str) and _EXTRA_DIGEST_RE.fullmatch(value) is not None
        shape = "'sha256:<64 hex>'"
    elif norm.endswith(_COUNT_SUFFIXES):
        ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
        shape = "a non-negative integer"
    else:
        ok = is_int or (isinstance(value, str) and _IDENT_RE.fullmatch(value) is not None)
        shape = f"one identifier (no whitespace, <= {MAX_REFERENCE_LENGTH} chars)"
    if not ok:
        raise ContentCaptureError(
            f"{path!r} is named as a reference but its value is not {shape}; a "
            "reference-shaped key does not exempt a payload (ADR-0171 I-2)"
        )


def _check_key(key: object, *, path: str) -> str:
    if not isinstance(key, str):
        raise ContentCaptureError(f"key at {path or 'record'} is not a string (I-2)")
    if len(key) > MAX_ID_LENGTH:
        raise ContentCaptureError(f"key at {path or 'record'} is over-long (I-2)")
    if not key.isascii():
        # Normalisation strips non-ASCII, so a homoglyph key (Cyrillic 'е' in
        # ``tеxt``) would otherwise slip past every marker. Extras are machine
        # keys; there is no legitimate reason for one to leave ASCII.
        raise ContentCaptureError(
            f"key {key!r} at {path or 'record'} is not ASCII; extra keys must be "
            "plain ASCII so the content markers can see them (ADR-0171 I-2)"
        )
    return key


def guard_payload(
    payload: Mapping[str, Any], *, known: frozenset[str], path: str = "", depth: int = 0
) -> None:
    """Raise :class:`ContentCaptureError` if *payload* smuggles store content.

    Declared model fields (*known*) are validated by their own validators; every
    other key is checked here:

    - at most :data:`MAX_EXTRA_KEYS` extras per mapping, and (at the top level)
      at most :data:`MAX_EXTRA_BYTES` of canonical JSON across all extras;
    - keys must be ASCII and must not contain a credential marker;
    - a content-marker key is refused unless reference-shaped (``…id`` /
      ``…ref`` / ``…digest`` / ``…count``), and then only if its *value* has
      the shape the suffix promises;
    - every string is length-capped and run through the ADR-0009 secret pack;
    - numeric sequences (embedding vectors) are refused and nesting is bounded.
    """
    if depth > _MAX_EXTRA_DEPTH:
        raise ContentCaptureError(f"{path or 'record'} nests deeper than {_MAX_EXTRA_DEPTH}")
    extras = [k for k in payload if not (depth == 0 and k in known)]
    if len(extras) > MAX_EXTRA_KEYS:
        raise ContentCaptureError(
            f"{path or 'record'} carries {len(extras)} extra keys, over the "
            f"{MAX_EXTRA_KEYS}-key limit; many small fields reassemble content (I-2)"
        )
    for raw_key in extras:
        value = payload[raw_key]
        key = _check_key(raw_key, path=path)
        where = f"{path}.{key}" if path else key
        norm = normalise_key(key)
        if any(marker in norm for marker in _CREDENTIAL_MARKERS):
            raise ContentCaptureError(
                f"field {where!r} names a credential; the access ledger never "
                "records one, even by reference (ADR-0009, ADR-0171 I-2)"
            )
        if _is_reference_key(norm):
            _guard_reference(value, norm=norm, path=where)
            continue
        if any(marker in norm for marker in _CONTENT_MARKERS):
            raise ContentCaptureError(
                f"field {where!r} looks content-bearing; the access ledger records "
                "references, digests, keys and counts only — never entry content, "
                "embeddings, or PII (ADR-0171 I-2)"
            )
        _guard_value(value, where=where, depth=depth)
    if depth == 0 and extras:
        _check_extras_size({k: payload[k] for k in extras})


def _check_extras_size(extras: Mapping[str, Any]) -> None:
    # Runs after the structural walk, so every value is already bounded and
    # JSON-shaped; ``default=str`` only covers exotic scalars (datetimes).
    size = len(json.dumps(extras, sort_keys=True, separators=(",", ":"), default=str).encode())
    if size > MAX_EXTRA_BYTES:
        raise ContentCaptureError(
            f"extra fields total {size} bytes, over the {MAX_EXTRA_BYTES}-byte limit; "
            "the access ledger is references, not a side channel for content (I-2)"
        )


def _guard_value(value: object, *, where: str, depth: int) -> None:
    if isinstance(value, Mapping):
        guard_payload(value, known=frozenset(), path=where, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        if len(value) > _MAX_EXTRA_LIST:
            raise ContentCaptureError(f"{where} lists over {_MAX_EXTRA_LIST} items")
        if value and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
            raise ContentCaptureError(
                f"{where} is a numeric sequence; an embedding vector never enters "
                "the ledger (ADR-0171 I-2)"
            )
        for index, item in enumerate(value):
            _guard_value(item, where=f"{where}[{index}]", depth=depth + 1)
    else:
        _guard_scalar(value, path=where)


# ── Scope ─────────────────────────────────────────────────────────────────


def parse_scope(allowed_scope: str) -> tuple[str, ...]:
    """Split and validate an ``allowed_scope`` declaration.

    Raises:
        ScopeError: on an empty, over-long, or over-wide declaration, or on an
            empty pattern part (``playbooks/``, ``/pb-1``).
    """
    if not isinstance(allowed_scope, str):
        raise ScopeError("allowed_scope must be a string")
    if len(allowed_scope) > MAX_SCOPE_LENGTH:
        raise ScopeError(f"allowed_scope is over {MAX_SCOPE_LENGTH} chars")
    patterns = tuple(p.strip() for p in allowed_scope.split(","))
    if not allowed_scope.strip() or any(not p for p in patterns):
        raise ScopeError(
            "allowed_scope must be non-empty patterns ('*', '<namespace>', or "
            "'<namespace>/<entry-glob>'); an undeclared scope cannot be evidence"
        )
    if len(patterns) > MAX_SCOPE_PATTERNS:
        raise ScopeError(f"allowed_scope has over {MAX_SCOPE_PATTERNS} patterns")
    for pattern in patterns:
        if pattern == "*":
            continue
        if "/" in pattern:
            namespace, _, entry_glob = pattern.rpartition("/")
            if not namespace or not entry_glob:
                raise ScopeError(f"scope pattern {pattern!r} has an empty part")
    return patterns


def _pattern_contains(pattern: str, namespace: str, entry_id: str) -> bool:
    if pattern == "*":
        return True
    if "/" not in pattern:
        return pattern == namespace
    scope_ns, _, entry_glob = pattern.rpartition("/")
    return scope_ns == namespace and fnmatch.fnmatchcase(entry_id, entry_glob)


def scope_contains(allowed_scope: str, namespace: str, entry_id: str) -> bool:
    """Return whether ``namespace``/``entry_id`` lies inside *allowed_scope*.

    Pure and deterministic; see the module docstring for the grammar.
    """
    return any(_pattern_contains(p, namespace, entry_id) for p in parse_scope(allowed_scope))


# ── Objects ───────────────────────────────────────────────────────────────


class AccessRecord(BaseModel):
    """One access to a shared-store entry — NF-392.

    ``value_digest`` is the digest of the value the access observed (read) or
    produced (write); it is what lets NF-395 bind a read to the write that
    seeded it. ``mutation_ref`` is the NF-391 record digest of a write.
    ``claimed_origin_mutation_ref`` / ``claimed_origin_run`` are what the
    *reader* believed it read — claims, verified by the derivation walk, never
    trusted as facts.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = ACCESS_SCHEMA_VERSION
    agent: str
    store_id: str
    namespace: str
    entry_id: str
    access: AccessKind
    allowed_scope: str
    contained: bool
    run: str | None = None
    at: str | None = None
    value_digest: str | None = None
    mutation_ref: str | None = None
    claimed_origin_mutation_ref: str | None = None
    claimed_origin_run: str | None = None
    prev_record_hash: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _guard(cls, data: Any) -> Any:
        if isinstance(data, Mapping):
            guard_payload(data, known=frozenset(cls.model_fields))
        return data

    @field_validator("agent", "store_id", "namespace", "entry_id", mode="before")
    @classmethod
    def _check_ids(cls, v: object) -> str:
        return _validate_id(v, field="identifier")

    @field_validator("run", "claimed_origin_run", mode="before")
    @classmethod
    def _check_runs(cls, v: object) -> str | None:
        return None if v is None else _validate_id(v, field="run")

    @field_validator("allowed_scope", mode="before")
    @classmethod
    def _check_scope(cls, v: object) -> str:
        parse_scope(v)  # type: ignore[arg-type]  # parse_scope rejects non-str
        return v  # type: ignore[return-value]

    @field_validator("at", mode="before")
    @classmethod
    def _check_at(cls, v: object) -> str | None:
        if v is None:
            return None
        if not isinstance(v, str) or not v.strip() or len(v) > MAX_TIMESTAMP_LENGTH:
            raise ValueError(f"at must be a non-empty timestamp ≤{MAX_TIMESTAMP_LENGTH} chars")
        return v

    @field_validator(
        "value_digest",
        "mutation_ref",
        "claimed_origin_mutation_ref",
        "prev_record_hash",
        mode="before",
    )
    @classmethod
    def _check_digests(cls, v: object) -> str | None:
        return None if v is None else _validate_digest(v, field="digest")


class ScopeFlagFinding(BaseModel):
    """A stored ``contained`` flag that disagrees with its recomputation."""

    model_config = ConfigDict(extra="forbid")

    index: int
    recorded: bool
    recomputed: bool


class AccessVerified(BaseModel):
    """Offline re-verification of an access block (spec §4.2 ``verified``)."""

    model_config = ConfigDict(extra="forbid")

    chain_ok: bool
    scope_flags_ok: bool
    broken_at: int | None = None
    reason: str | None = None


class AccessLedgerBlock(BaseModel):
    """``facets.memstore_mutation.access`` — this run's access rows (NF-392)."""

    model_config = ConfigDict(extra="allow")

    schema_version: str = ACCESS_SCHEMA_VERSION
    store_id: str
    ledger_ref: str | None = None
    accesses: list[AccessRecord] = Field(default_factory=list, max_length=MAX_ACCESS_RECORDS)
    verified: AccessVerified | None = None

    @model_validator(mode="before")
    @classmethod
    def _guard(cls, data: Any) -> Any:
        if isinstance(data, Mapping):
            guard_payload(data, known=frozenset(cls.model_fields))
        return data

    @field_validator("store_id", mode="before")
    @classmethod
    def _check_store(cls, v: object) -> str:
        return _validate_id(v, field="store_id")

    @field_validator("ledger_ref", mode="before")
    @classmethod
    def _check_ref(cls, v: object) -> str | None:
        return None if v is None else _validate_digest(v, field="ledger_ref")


# ── The chain ─────────────────────────────────────────────────────────────


def access_preimage(record: AccessRecord) -> str:
    """Return the canonical JSON :func:`access_digest` hashes.

    Same construction as the NF-391 ledger's ``record_preimage``: sorted keys,
    compact separators, absent optionals omitted rather than nulled.
    """
    payload: dict[str, Any] = json.loads(record.model_dump_json(exclude_none=True))
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def access_digest(record: AccessRecord) -> str:
    """Return the ``sha256:`` digest of one access row — its chain link."""
    return f"sha256:{hashlib.sha256(access_preimage(record).encode()).hexdigest()}"


def access_head(records: Sequence[AccessRecord]) -> str | None:
    """Return the head digest of an access ledger, or None when empty."""
    return access_digest(records[-1]) if records else None


def record_access(
    records: Sequence[AccessRecord],
    *,
    agent: str,
    store_id: str,
    namespace: str,
    entry_id: str,
    access: AccessKind,
    allowed_scope: str,
    run: str | None = None,
    at: str | None = None,
    value_digest: str | None = None,
    mutation_ref: str | None = None,
    claimed_origin_mutation_ref: str | None = None,
    claimed_origin_run: str | None = None,
) -> list[AccessRecord]:
    """Append one access row, returning a **new** list; *records* is untouched.

    ``contained`` is computed here from *allowed_scope* — never supplied — and
    an out-of-scope access is appended exactly like an in-scope one. This
    function never refuses an access; it refuses only malformed evidence.

    Raises:
        ScopeError: if *allowed_scope* is malformed.
        MemstoreError: if the ledger is already at :data:`MAX_ACCESS_RECORDS`.
    """
    if len(records) >= MAX_ACCESS_RECORDS:
        raise MemstoreError(
            f"access ledger is at its {MAX_ACCESS_RECORDS}-row bound; start a new "
            "block rather than growing one without limit"
        )
    record = AccessRecord(
        agent=agent,
        store_id=store_id,
        namespace=namespace,
        entry_id=entry_id,
        access=access,
        allowed_scope=allowed_scope,
        contained=scope_contains(allowed_scope, namespace, entry_id),
        run=run,
        at=at,
        value_digest=value_digest,
        mutation_ref=mutation_ref,
        claimed_origin_mutation_ref=claimed_origin_mutation_ref,
        claimed_origin_run=claimed_origin_run,
        prev_record_hash=access_head(records),
    )
    return [*records, record]


def verify_access_chain(records: Sequence[AccessRecord]) -> LedgerVerification:
    """Walk the access chain offline; report the first broken index.

    Returns, never raises (a finding, not a crash). Advances on *recomputed*
    digests so an edited row breaks at its successor.
    """
    expected: str | None = None
    for index, record in enumerate(records):
        if record.prev_record_hash != expected:
            return LedgerVerification(
                ok=False,
                broken_at=index,
                reason=(
                    f"prev_record_hash mismatch at access row {index} "
                    f"(stored={record.prev_record_hash!r}, expected={expected!r}); "
                    "a row was dropped, reordered, or edited"
                ),
            )
        expected = access_digest(record)
    return LedgerVerification(ok=True)


def verify_scope_flags(records: Sequence[AccessRecord]) -> list[ScopeFlagFinding]:
    """Recompute every ``contained`` flag; return the rows that disagree.

    A row reading ``contained: true`` for an out-of-scope access is a forged
    flag, and so is the reverse. Empty list ⇒ every flag is honest.
    """
    findings: list[ScopeFlagFinding] = []
    for index, record in enumerate(records):
        recomputed = scope_contains(record.allowed_scope, record.namespace, record.entry_id)
        if recomputed != record.contained:
            findings.append(
                ScopeFlagFinding(index=index, recorded=record.contained, recomputed=recomputed)
            )
    return findings


def verify_access_block(block: AccessLedgerBlock) -> AccessVerified:
    """Re-verify a block: chain, head binding, every scope flag, store match."""
    chain = verify_access_chain(block.accesses)
    flags = verify_scope_flags(block.accesses)
    reason = chain.reason
    broken_at = chain.broken_at
    chain_ok = chain.ok
    if chain_ok and block.ledger_ref != access_head(block.accesses):
        chain_ok = False
        reason = "ledger_ref does not match the access chain head (rows truncated or unbound)"
    foreign = [i for i, r in enumerate(block.accesses) if r.store_id != block.store_id]
    if chain_ok and foreign:
        chain_ok = False
        broken_at = foreign[0]
        reason = f"access row {foreign[0]} names another store than the block"
    return AccessVerified(
        chain_ok=chain_ok,
        scope_flags_ok=not flags,
        broken_at=broken_at,
        reason=reason,
    )


def build_access_block(store_id: str, accesses: Sequence[AccessRecord]) -> AccessLedgerBlock | None:
    """Build the block for *accesses*, or None when there are none (I-3)."""
    if not accesses:
        return None
    block = AccessLedgerBlock(
        store_id=store_id, ledger_ref=access_head(accesses), accesses=list(accesses)
    )
    block.verified = verify_access_block(block)
    return block


def uncontained(records: Sequence[AccessRecord]) -> list[AccessRecord]:
    """Return the out-of-scope rows — the NF-392 evidence set."""
    return [r for r in records if not r.contained]


# ── Facet attachment ──────────────────────────────────────────────────────


def attach_access(capsule: dict[str, Any], block: AccessLedgerBlock | None) -> dict[str, Any]:
    """Attach *block* under ``facets.memstore_mutation.access``, additively.

    Returns *capsule* itself when *block* is None (I-3, byte-identical). If the
    capsule has no mutation facet yet, a minimal one (``schema_version`` +
    ``store_id``) is created, since a run may read a store without writing it.
    Attach the NF-391 facet first: ``attach_facet`` replaces the facet block.

    Raises:
        StoreMismatchError: if the capsule's facet names a different store.
    """
    if block is None:
        return capsule
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    existing = facets.get(FACET_NAME)
    facet = dict(existing) if isinstance(existing, dict) else {}
    if facet.get("store_id") not in (None, block.store_id):
        raise StoreMismatchError(
            f"capsule's {FACET_NAME} facet is for store {facet.get('store_id')!r}, "
            f"not {block.store_id!r}; one facet describes one store"
        )
    facet.setdefault("schema_version", ACCESS_SCHEMA_VERSION)
    facet.setdefault("store_id", block.store_id)
    facet[ACCESS_BLOCK] = block.model_dump(exclude_none=True)
    facets[FACET_NAME] = facet
    out["facets"] = facets
    return out


def access_block_from_capsule(capsule: Mapping[str, Any]) -> AccessLedgerBlock | None:
    """Read the access block back; None when absent (the common case, I-3).

    Raises:
        pydantic.ValidationError / MemstoreError: when present but malformed —
            a defective evidence block is reported, never silently skipped.
    """
    facets = capsule.get("facets")
    facet = facets.get(FACET_NAME) if isinstance(facets, Mapping) else None
    block = facet.get(ACCESS_BLOCK) if isinstance(facet, Mapping) else None
    if block is None:
        return None
    return AccessLedgerBlock.model_validate(block)


# ── C2 composition ────────────────────────────────────────────────────────


def access_kwargs_from_memory_event(event: MemoryOperationEvent) -> dict[str, Any]:
    """Map a shipped C2 ``MemoryOperationEvent`` onto :func:`record_access` kwargs.

    Composes ADR-0143's per-run event rather than re-capturing it: only the key,
    agent, run, timestamp and the reader's *claimed* origin run are used. The
    event's opt-in ``value`` is never read (I-2). ``delete``/``update``/``write``
    all map to ``write``. The caller supplies ``store_id``, ``namespace`` and
    ``allowed_scope`` (the event does not carry them) plus, optionally, a
    ``value_digest`` it computed itself.
    """
    kwargs: dict[str, Any] = {
        "entry_id": event.memory_key,
        "access": "read" if event.operation == "read" else "write",
        "run": event.run_id,
        "at": event.timestamp_utc,
    }
    # No placeholder agent is invented: an event without one leaves ``agent``
    # for the caller to supply, and record_access fails loudly if nobody does.
    if event.agent_id is not None:
        kwargs["agent"] = event.agent_id
    if event.operation == "read" and event.origin_run_id is not None:
        kwargs["claimed_origin_run"] = event.origin_run_id
    return kwargs
