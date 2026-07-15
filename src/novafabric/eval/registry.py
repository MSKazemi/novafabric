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

"""Content-addressed registry for signed eval cards (NF-002, ADR-0099).

An eval card is registered by its content digest so any
:class:`~novafabric.eval.scores.Score` can be resolved back to the exact evaluator
that produced it (requirement 2: a score whose ``eval_card_digest`` does not resolve
here is invalid).

**Storage decision (deliberate, see spec §4.4 + design note).** ADR-0099 authorises
eval cards as Asset-Registry assets. We honour "*no new storage backend*" by reusing
the registry's own SQLite database (`novafabric.registry.store.get_connection`), but
we store cards in a **dedicated additive ``eval_cards`` table** rather than mutating
the shared ``AssetType`` enum / ``BaseAssetSpec`` promotion lifecycle
(``src/novafabric/spec/models.py``) — that shared schema feeds the Rego promote gate,
the KG normaliser and the suggestion engine, so extending it is a larger, human-review
change tracked as a follow-up. This table is purely additive: it cannot affect existing
``assets`` queries or data.

Registration is *gated* (spec "eval-gated registration"): a card MUST carry a signature
block, and a ``judge`` card MUST carry calibration, or registration is refused.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from novafabric.eval.card import EVAL_CARD_ASSET_TYPE, EvalCard, card_digest
from novafabric.registry.store import get_connection


class EvalCardError(Exception):
    """Base class for eval-card registry errors."""


class DuplicateEvalCardError(EvalCardError):
    """Raised when a ``card_id@version`` is already registered."""


class EvalCardNotFoundError(EvalCardError):
    """Raised when a card cannot be resolved by ref or digest."""


class UnsignedCardError(EvalCardError):
    """Raised when registration is attempted on a card that fails the gate."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def asset_ref(card: EvalCard) -> str:
    """Return the ``eval-card:<card_id>@<version>+sha256:<hex>`` asset ref (spec §4.2)."""
    return f"{EVAL_CARD_ASSET_TYPE}:{card.card_id}@{card.version}+{card_digest(card)}"


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS eval_cards (
            digest      TEXT PRIMARY KEY,
            card_id     TEXT NOT NULL,
            version     TEXT NOT NULL,
            source      TEXT NOT NULL,
            asset_ref   TEXT NOT NULL,
            card_json   TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            UNIQUE(card_id, version)
        );
        CREATE INDEX IF NOT EXISTS idx_eval_cards_card_id ON eval_cards(card_id);
        CREATE INDEX IF NOT EXISTS idx_eval_cards_source ON eval_cards(source);
        """
    )
    conn.commit()


def _gate(card: EvalCard) -> None:
    """Enforce the registration gate; raise :class:`UnsignedCardError` on failure.

    Judge-card calibration is already guaranteed by :class:`EvalCard`'s own validator,
    so the only gate this layer adds is that the card is *signed* (the model permits an
    unsigned ``signature=None`` card for the sign-then-register flow).
    """
    if card.signature is None:
        raise UnsignedCardError(
            f"eval card {card.card_id}@{card.version} must be signed before registration"
        )


def register_card(card: EvalCard, db_path: Path | None = None) -> str:
    """Register *card* content-addressed; return its ``sha256:<hex>`` digest.

    Refuses an unsigned card, a judge card without calibration, or a duplicate
    ``card_id@version``.
    """
    _gate(card)
    digest = card_digest(card)
    conn = get_connection(db_path)
    try:
        _ensure_table(conn)
        dup = conn.execute(
            "SELECT digest FROM eval_cards WHERE card_id = ? AND version = ?",
            (card.card_id, card.version),
        ).fetchone()
        if dup is not None:
            raise DuplicateEvalCardError(
                f"eval card {card.card_id}@{card.version} is already registered"
            )
        conn.execute(
            """
            INSERT INTO eval_cards
                (digest, card_id, version, source, asset_ref, card_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                digest,
                card.card_id,
                card.version,
                card.source.value,
                asset_ref(card),
                card.model_dump_json(exclude_none=True),
                _now(),
            ),
        )
        conn.commit()
        return digest
    finally:
        conn.close()


def card_exists(digest: str, db_path: Path | None = None) -> bool:
    """Return True iff a card with *digest* is registered (Score-validation hook, req 2)."""
    conn = get_connection(db_path)
    try:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT 1 FROM eval_cards WHERE digest = ?", (digest,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_card_by_digest(digest: str, db_path: Path | None = None) -> EvalCard:
    """Load a registered card by its content digest."""
    conn = get_connection(db_path)
    try:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT card_json FROM eval_cards WHERE digest = ?", (digest,)
        ).fetchone()
        if row is None:
            raise EvalCardNotFoundError(f"no eval card registered for digest {digest}")
        return EvalCard.model_validate_json(row["card_json"])
    finally:
        conn.close()


def get_card(card_id: str, version: str, db_path: Path | None = None) -> EvalCard:
    """Load a registered card by ``card_id`` and ``version``."""
    conn = get_connection(db_path)
    try:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT card_json FROM eval_cards WHERE card_id = ? AND version = ?",
            (card_id, version),
        ).fetchone()
        if row is None:
            raise EvalCardNotFoundError(f"no eval card registered for {card_id}@{version}")
        return EvalCard.model_validate_json(row["card_json"])
    finally:
        conn.close()


def list_cards(source: str | None = None, db_path: Path | None = None) -> list[EvalCard]:
    """List registered cards, newest first, optionally filtered by ``source``."""
    conn = get_connection(db_path)
    try:
        _ensure_table(conn)
        if source is None:
            rows = conn.execute(
                "SELECT card_json FROM eval_cards ORDER BY created_at DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT card_json FROM eval_cards WHERE source = ? ORDER BY created_at DESC",
                (source,),
            ).fetchall()
        return [EvalCard.model_validate_json(r["card_json"]) for r in rows]
    finally:
        conn.close()
