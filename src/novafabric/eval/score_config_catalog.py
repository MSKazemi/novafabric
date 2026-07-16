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

"""Local score-configuration catalog (ADR-0117 D3) — SQLite-backed, local-first.

Stores :class:`~novafabric.eval.score_config.ScoreConfig` records in the registry's
own SQLite database (``registry.store.get_connection``), in a dedicated **additive**
``score_configs`` table — mirroring the eval-card registry's storage decision
(:mod:`novafabric.eval.registry`): no new storage backend, no change to the shared
``assets`` schema, no server, no internet.

The catalog is append-only in spirit (I1/C4): versions accumulate, none are mutated.
:func:`register_config` auto-bumps ``version`` per ``name`` and treats an identical
body as a no-op (C5). :func:`append_score_validated` is the opt-in D2 hook: it
resolves a config by the score's ``name`` and refuses the append on violation —
**no matching config means the score is appended unchanged** (free score, exactly
today's behavior).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from novafabric.eval.score_config import (
    ScoreCategory,
    ScoreConfig,
    ScoreConfigError,
    ScoreRange,
    validate_score_against_config,
)
from novafabric.eval.scores import Score, ScoreValueType, append_score
from novafabric.registry.store import get_connection

__all__ = [
    "ScoreConfigImmutabilityError",
    "ScoreConfigNotFoundError",
    "append_score_validated",
    "find_config_for_score",
    "get_config",
    "get_config_by_digest",
    "list_configs",
    "register_config",
    "register_config_record",
    "resolve_config_ref",
]


class ScoreConfigNotFoundError(ScoreConfigError):
    """Raised when a config cannot be resolved by name, ``name@version``, or digest."""


class ScoreConfigImmutabilityError(ScoreConfigError):
    """Raised when a write would redefine an existing ``(name, version)`` (I1/C4)."""


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS score_configs (
            config_id       TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            version         INTEGER NOT NULL,
            value_type      TEXT NOT NULL,
            content_digest  TEXT NOT NULL UNIQUE,
            config_json     TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            UNIQUE(name, version)
        );
        CREATE INDEX IF NOT EXISTS idx_score_configs_name ON score_configs(name);
        """
    )
    conn.commit()


def _row_to_config(row: sqlite3.Row) -> ScoreConfig:
    return ScoreConfig.model_validate_json(row["config_json"])


def _latest(conn: sqlite3.Connection, name: str) -> ScoreConfig | None:
    row = conn.execute(
        "SELECT config_json FROM score_configs WHERE name = ? "
        "ORDER BY version DESC LIMIT 1",
        (name,),
    ).fetchone()
    return None if row is None else _row_to_config(row)


def _insert(conn: sqlite3.Connection, config: ScoreConfig) -> None:
    conn.execute(
        """
        INSERT INTO score_configs
            (config_id, name, version, value_type, content_digest, config_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            config.config_id,
            config.name,
            config.version,
            config.value_type.value,
            config.content_digest,
            config.model_dump_json(exclude_none=True),
            config.created_at,
        ),
    )
    conn.commit()


def register_config(
    name: str,
    value_type: ScoreValueType,
    description: str,
    categories: list[ScoreCategory] | None = None,
    range_: ScoreRange | None = None,
    labels: list[str] | None = None,
    db_path: Path | None = None,
) -> ScoreConfig:
    """Register a score definition under *name*; return the stored record.

    Versioning per I1/C5: a first registration is ``version=1``; a changed definition
    body bumps to ``latest+1`` (never edits in place); an **identical** body is a
    no-op returning the already-stored record.
    """
    def _candidate(version: int) -> ScoreConfig:
        return ScoreConfig(
            name=name,
            value_type=value_type,
            description=description,
            categories=categories,
            range=range_,
            labels=labels,
            version=version,
        )

    conn = get_connection(db_path)
    try:
        _ensure_table(conn)
        latest = _latest(conn, name)
        if latest is None:
            candidate = _candidate(version=1)
        else:
            if _candidate(version=latest.version).content_digest == latest.content_digest:
                return latest  # C5: identical body ⇒ no-op
            candidate = _candidate(version=latest.version + 1)
        _insert(conn, candidate)
        return candidate
    finally:
        conn.close()


def register_config_record(config: ScoreConfig, db_path: Path | None = None) -> ScoreConfig:
    """Register a fully-formed record at its own ``(name, version)`` (import path).

    Enforces I1: an existing ``(name, version)`` with a **different** body is refused
    (:class:`ScoreConfigImmutabilityError`); an identical body is a no-op returning
    the stored record.
    """
    conn = get_connection(db_path)
    try:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT config_json FROM score_configs WHERE name = ? AND version = ?",
            (config.name, config.version),
        ).fetchone()
        if row is not None:
            existing = _row_to_config(row)
            if existing.content_digest == config.content_digest:
                return existing  # identical body ⇒ no-op
            raise ScoreConfigImmutabilityError(
                f"score config {config.name}@{config.version} already exists with a "
                f"different body ({existing.content_digest}); a redefinition must bump "
                f"the version (I1)"
            )
        _insert(conn, config)
        return config
    finally:
        conn.close()


def get_config(
    name: str, version: int | None = None, db_path: Path | None = None
) -> ScoreConfig:
    """Load a config by *name*; latest version when *version* is ``None``."""
    conn = get_connection(db_path)
    try:
        _ensure_table(conn)
        if version is None:
            latest = _latest(conn, name)
            if latest is None:
                raise ScoreConfigNotFoundError(f"no score config registered for {name!r}")
            return latest
        row = conn.execute(
            "SELECT config_json FROM score_configs WHERE name = ? AND version = ?",
            (name, version),
        ).fetchone()
        if row is None:
            raise ScoreConfigNotFoundError(
                f"no score config registered for {name}@{version}"
            )
        return _row_to_config(row)
    finally:
        conn.close()


def get_config_by_digest(digest: str, db_path: Path | None = None) -> ScoreConfig:
    """Load a config by its ``sha256:`` content digest (reproducibility-stable ref)."""
    conn = get_connection(db_path)
    try:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT config_json FROM score_configs WHERE content_digest = ?", (digest,)
        ).fetchone()
        if row is None:
            raise ScoreConfigNotFoundError(f"no score config registered for digest {digest}")
        return _row_to_config(row)
    finally:
        conn.close()


def resolve_config_ref(ref: str, db_path: Path | None = None) -> ScoreConfig:
    """Resolve ``name``, ``name@version``, or ``sha256:<hex>`` to one config.

    A bare ``name`` resolves to the latest version (spec §Reference / resolution).
    """
    if ref.startswith("sha256:"):
        return get_config_by_digest(ref, db_path=db_path)
    if "@" in ref:
        name, _, suffix = ref.rpartition("@")
        if not suffix.isdigit():
            raise ValueError(
                f"invalid config ref {ref!r}: version must be an integer (name@version)"
            )
        return get_config(name, version=int(suffix), db_path=db_path)
    return get_config(ref, db_path=db_path)


def list_configs(
    all_versions: bool = False, db_path: Path | None = None
) -> list[ScoreConfig]:
    """List configs — latest version per ``name`` by default; every version with
    ``all_versions=True``. Ordered by ``name`` then descending ``version``."""
    conn = get_connection(db_path)
    try:
        _ensure_table(conn)
        if all_versions:
            rows = conn.execute(
                "SELECT config_json FROM score_configs ORDER BY name, version DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT config_json FROM score_configs AS sc
                WHERE version = (
                    SELECT MAX(version) FROM score_configs WHERE name = sc.name
                )
                ORDER BY name
                """
            ).fetchall()
        return [_row_to_config(r) for r in rows]
    finally:
        conn.close()


def find_config_for_score(name: str, db_path: Path | None = None) -> ScoreConfig | None:
    """Latest config governing metric *name*, or ``None`` (free score — D2 lookup)."""
    conn = get_connection(db_path)
    try:
        _ensure_table(conn)
        return _latest(conn, name)
    finally:
        conn.close()


def append_score_validated(
    path: str | Path, score: Score, db_path: Path | None = None
) -> ScoreConfig | None:
    """Append *score* after validating it against its governing config, if any (D2).

    Returns the config the score was validated against, so callers can pin its
    ``content_digest`` (D4) — or ``None`` when no config governs ``score.name``
    (the score is then appended unchanged, exactly today's behavior). On a
    :class:`~novafabric.eval.score_config.ScoreConfigViolation` nothing is appended.
    """
    config = find_config_for_score(score.name, db_path=db_path)
    if config is not None:
        validate_score_against_config(score, config)
    append_score(path, score)
    return config
