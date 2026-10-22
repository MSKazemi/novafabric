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
"""Per-subject PII envelope: AES-256-GCM under the subject's DEK (ADR-0314, experimental).

This is the write-time encryption step ADR-0069 §2 designed and never had: a PII span
retained in a capsule is replaced by an inline, versioned **envelope token** whose
ciphertext only the data subject's DEK (``$NOVAFABRIC_HOME/dek.db``) can open. Once
``nova pii erase <subject>`` destroys that DEK, the token can never be opened again and
readers render it as ``[erased]``.

Token grammar (``schemas/pii-envelope.schema.json``)::

    [NFPII:v1:<entity>:sha256:<subject-hmac>:<nonce>:<ciphertext>]

* ``entity`` — detection rule(s), ``[A-Z0-9_+]+`` (e.g. ``EMAIL``, ``EMAIL+PERSON``);
* ``sha256:<subject-hmac>`` — HMAC-SHA256(``NOVA_PII_PEPPER``, subject id), the same
  pseudonymous reference the redaction manifest and ``nova pii status`` use. The raw
  subject id is never written to a capsule;
* ``nonce`` — 12 random bytes, 24 lowercase hex characters, fresh per token;
* ``ciphertext`` — AES-256-GCM ciphertext with its 16-byte tag, lowercase hex in
  ``.``-separated groups of 16. The grouping is deliberate: the ADR-0009 secret
  scanner redacts bare 32/40-character alphanumeric runs and bare 64-character hex
  runs as API keys, and a residual pass rewriting part of a ciphertext would destroy
  it. A 16-character group can match none of those rules.

Associated data binds each token to where it was written — ``run_id``, ``field_path``
(the capsule-relative locator, e.g. ``model-calls.jsonl#L3 request.messages[0].content``),
the token's position among the envelopes in that value, the entity, and the raw subject
id — so a token copied to another field, run, position or subject fails to open
(:class:`EnvelopeIntegrityError`), exactly like a flipped ciphertext bit.

Never logs plaintext, keys or raw subject ids.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict, Field

from novafabric.pii.dek.store import DataSubjectDEK, DEKStore

ENVELOPE_VERSION = 1
ENVELOPE_ALG = "AES-256-GCM"
_AAD_DOMAIN = b"novafabric/pii-envelope/v1\n"
_NONCE_BYTES = 12
_GROUP = 16

#: Placeholders a reader renders instead of plaintext. Never raises on any of them.
ERASED = "[erased]"
TAMPERED = "[tampered]"
LOCKED = "[encrypted]"

TOKEN_RE = re.compile(
    r"\[NFPII:v1:(?P<entity>[A-Z0-9_+]{1,64}):(?P<subject_ref>sha256:[0-9a-f]{64}):"
    r"(?P<nonce>[0-9a-f]{24}):(?P<ct>[0-9a-f]{1,16}(?:\.[0-9a-f]{1,16})*)\]"
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PIIEnvelopeError(Exception):
    """Base error for PII envelope operations."""


class EnvelopeFormatError(PIIEnvelopeError):
    """The text is not a well-formed v1 envelope token."""


class EnvelopeIntegrityError(PIIEnvelopeError):
    """AES-GCM authentication failed: ciphertext, tag, nonce or associated data changed."""


class SubjectKeyErasedError(PIIEnvelopeError):
    """No DEK exists for the envelope's subject: it was erased (``nova pii erase``)."""


class SubjectKeyUnavailableError(PIIEnvelopeError):
    """The reader cannot resolve subjects: no DEK store or no ``NOVA_PII_PEPPER``."""


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


def subject_ref(pepper: bytes, subject_id: str) -> str:
    """``sha256:<hex>`` HMAC of *subject_id* — same form as the redaction manifest."""
    mac = hmac.new(pepper, subject_id.encode("utf-8"), hashlib.sha256)
    return "sha256:" + mac.hexdigest()


def subject_hash_prefix(subject_id: str) -> str:
    """First 12 hex chars of SHA-256(subject_id): the only subject form allowed in logs."""
    return hashlib.sha256(subject_id.encode("utf-8")).hexdigest()[:12]


class PIIEnvelope(BaseModel):
    """A typed, versioned per-subject ciphertext envelope (parsed form of a token)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = Field(default=1, description="Envelope format version")
    alg: Literal["AES-256-GCM"] = Field(default="AES-256-GCM", description="AEAD algorithm")
    entity_type: str = Field(..., pattern=r"^[A-Z0-9_+]{1,64}$")
    subject_ref: str = Field(..., pattern=r"^sha256:[0-9a-f]{64}$")
    nonce: str = Field(..., pattern=r"^[0-9a-f]{24}$")
    ciphertext: str = Field(
        ..., pattern=r"^[0-9a-f]+$", description="Ciphertext || 16-byte tag, lowercase hex"
    )

    def to_token(self) -> str:
        """Serialise to the inline ``[NFPII:v1:...]`` token."""
        groups = [self.ciphertext[i : i + _GROUP] for i in range(0, len(self.ciphertext), _GROUP)]
        return (
            f"[NFPII:v1:{self.entity_type}:{self.subject_ref}:{self.nonce}:"
            f"{'.'.join(groups)}]"
        )

    @classmethod
    def from_token(cls, token: str) -> PIIEnvelope:
        """Parse one token; raise :class:`EnvelopeFormatError` if it is not one."""
        m = TOKEN_RE.fullmatch(token)
        if m is None:
            raise EnvelopeFormatError("not a v1 PII envelope token")
        return cls(
            entity_type=m["entity"],
            subject_ref=m["subject_ref"],
            nonce=m["nonce"],
            ciphertext=m["ct"].replace(".", ""),
        )


def build_aad(
    *, run_id: str, field_path: str, span_index: int, entity_type: str, subject_id: str
) -> bytes:
    """Canonical associated data binding an envelope to its place and subject."""
    body = json.dumps(
        {
            "entity_type": entity_type,
            "field_path": field_path,
            "run_id": run_id,
            "span_index": span_index,
            "subject_id": subject_id,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return _AAD_DOMAIN + body.encode("utf-8")


def _key(dek: DataSubjectDEK) -> bytes:
    key = bytes.fromhex(dek.dek_hex)
    if len(key) != 32:
        raise PIIEnvelopeError("DEK is not 32 bytes")
    return key


def seal_value(
    plaintext: str,
    *,
    dek: DataSubjectDEK,
    subject_ref_value: str,
    run_id: str,
    field_path: str,
    span_index: int,
    entity_type: str,
) -> PIIEnvelope:
    """Encrypt *plaintext* under the subject's DEK with a fresh random nonce."""
    nonce = os.urandom(_NONCE_BYTES)
    aad = build_aad(
        run_id=run_id,
        field_path=field_path,
        span_index=span_index,
        entity_type=entity_type,
        subject_id=dek.subject_id,
    )
    ct = AESGCM(_key(dek)).encrypt(nonce, plaintext.encode("utf-8"), aad)
    return PIIEnvelope(
        entity_type=entity_type,
        subject_ref=subject_ref_value,
        nonce=nonce.hex(),
        ciphertext=ct.hex(),
    )


def open_value(
    envelope: PIIEnvelope,
    *,
    dek: DataSubjectDEK,
    run_id: str,
    field_path: str,
    span_index: int,
) -> str:
    """Decrypt *envelope*; raise :class:`EnvelopeIntegrityError` on any tampering."""
    aad = build_aad(
        run_id=run_id,
        field_path=field_path,
        span_index=span_index,
        entity_type=envelope.entity_type,
        subject_id=dek.subject_id,
    )
    try:
        pt = AESGCM(_key(dek)).decrypt(
            bytes.fromhex(envelope.nonce), bytes.fromhex(envelope.ciphertext), aad
        )
    except (InvalidTag, ValueError) as exc:
        raise EnvelopeIntegrityError("envelope failed authentication") from exc
    return pt.decode("utf-8")


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


@dataclass
class EnvelopeKeyring:
    """Resolves an envelope's ``subject_ref`` to the subject's live DEK.

    An authorised reader holds both the DEK store and ``NOVA_PII_PEPPER``. Without
    either, every envelope stays sealed (:class:`SubjectKeyUnavailableError`). A
    subject absent from a present store has been erased (:class:`SubjectKeyErasedError`).
    """

    dek_store: DEKStore | None
    pepper: bytes | None
    _by_ref: dict[str, DataSubjectDEK] | None = field(default=None, repr=False)

    @classmethod
    def from_home(cls, home: Path, pepper: bytes | None = None) -> EnvelopeKeyring:
        """Keyring over ``<home>/dek.db``; never creates the store.

        *pepper* defaults to ``NOVA_PII_PEPPER``.
        """
        if pepper is None:
            raw = os.environ.get("NOVA_PII_PEPPER", "")
            pepper = raw.encode("utf-8") if raw else None
        db = home / "dek.db"
        store = DEKStore(db) if db.is_file() else None
        return cls(dek_store=store, pepper=pepper)

    def dek_for(self, ref: str) -> DataSubjectDEK:
        if self.pepper is None:
            raise SubjectKeyUnavailableError("NOVA_PII_PEPPER is not available")
        if self.dek_store is None:
            # ADR-0069: a lost dek.db leaves the same guarantee as an erased DEK.
            raise SubjectKeyErasedError("no DEK store: subject keys are gone")
        if self._by_ref is None:
            by_ref: dict[str, DataSubjectDEK] = {}
            for record in self.dek_store.list_subjects():
                dek = self.dek_store.get_dek(record.subject_id)
                if dek is not None:
                    by_ref[subject_ref(self.pepper, record.subject_id)] = dek
            self._by_ref = by_ref
        dek = self._by_ref.get(ref)
        if dek is None:
            raise SubjectKeyErasedError("no DEK for this subject: erased")
        return dek

    def forget(self) -> None:
        """Drop cached keys (e.g. after an erasure in the same process)."""
        self._by_ref = None

    def close(self) -> None:
        self.forget()
        if self.dek_store is not None:
            self.dek_store.close()


def render_value(
    text: str, *, keyring: EnvelopeKeyring | None, run_id: str, field_path: str
) -> str:
    """Replace every envelope token in *text* with its plaintext or a placeholder.

    ``[erased]`` (subject's DEK gone), ``[tampered]`` (authentication failed) or
    ``[encrypted]`` (this reader holds no key access). Never raises.
    """
    if "[NFPII:" not in text:
        return text
    index = 0

    def _one(m: re.Match[str]) -> str:
        nonlocal index
        span_index = index
        index += 1
        if keyring is None:
            return LOCKED
        try:
            env = PIIEnvelope.from_token(m.group(0))
            dek = keyring.dek_for(env.subject_ref)
            return open_value(
                env, dek=dek, run_id=run_id, field_path=field_path, span_index=span_index
            )
        except SubjectKeyErasedError:
            return ERASED
        except SubjectKeyUnavailableError:
            return LOCKED
        except (PIIEnvelopeError, ValueError):
            return TAMPERED

    return TOKEN_RE.sub(_one, text)


def _render_obj(obj: Any, path: str, ref: str, keyring: EnvelopeKeyring | None,
                run_id: str) -> Any:
    if isinstance(obj, str):
        locator = ref + (f" {path}" if path else "")
        return render_value(obj, keyring=keyring, run_id=run_id, field_path=locator)
    if isinstance(obj, dict):
        return {
            k: _render_obj(v, f"{path}.{k}" if path else str(k), ref, keyring, run_id)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_render_obj(v, f"{path}[{i}]", ref, keyring, run_id) for i, v in enumerate(obj)]
    return obj


def read_capsule_file(
    capsule_dir: Path, rel: str, *, keyring: EnvelopeKeyring | None, run_id: str
) -> Any:
    """Return capsule file *rel* with every envelope rendered (see :func:`render_value`).

    JSONL → list of records, YAML → the parsed document, anything else → text. Uses
    the same walk and locators as the capture stage, so field paths line up.
    """
    import yaml

    from novafabric.masking._pipeline import _walk_mode

    path = capsule_dir / rel
    mode = _walk_mode(rel)
    if mode == "jsonl":
        records: list[Any] = []
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                records.append(
                    render_value(line, keyring=keyring, run_id=run_id,
                                 field_path=f"{rel}#L{lineno}")
                )
                continue
            records.append(_render_obj(obj, "", f"{rel}#L{lineno}", keyring, run_id))
        return records
    if mode == "yaml":
        return _render_obj(
            yaml.safe_load(path.read_text(encoding="utf-8")), "", rel, keyring, run_id
        )
    out: list[str] = []
    for lineno, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(keepends=True), start=1
    ):
        body = line.rstrip("\r\n")
        out.append(
            render_value(body, keyring=keyring, run_id=run_id, field_path=f"{rel}#L{lineno}")
            + line[len(body):]
        )
    return "".join(out)
