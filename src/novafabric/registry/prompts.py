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

"""Prompt-version registry operations (ADR-0112).

Extends the existing local SQLite registry (ADR-0001/0002) — **no schema
change**. A prompt version is stored as a generic row in the existing
``assets`` table (``name = prompt_id``, ``asset_type = 'prompt'``,
``version = str(int)``) whose ``spec_json`` holds the full
:class:`~novafabric.spec.prompt_asset.PromptAsset` record. The existing
``UNIQUE(name, version)`` constraint enforces the immutability key.

Invariants:

* **Append-only versions** — registering changed content writes
  ``version = max(existing) + 1``; existing rows are never mutated.
* **Idempotent register** — a canonical body whose ``content_hash`` already
  exists for the ``prompt_id`` returns the existing version, no new row.
* **Lifecycle status is row truth** — ``status`` may advance via the existing
  eval-gated ``nova promote`` (ADR-0003); the content fields stay frozen.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from novafabric.registry.store import (
    get_connection,
    init_schema,
    record_status_change,
    spec_digest,
)
from novafabric.spec.prompt_asset import (
    CompositionRef,
    PromptAsset,
    PromptTemplate,
    compute_content_hash,
    validate_template,
)
from novafabric.spec.prompt_composition import has_composition_refs

# Bounded retry budget for the (rare) concurrent-register race on
# UNIQUE(name, version).
_REGISTER_RETRIES = 5


class PromptNotFoundError(Exception):
    """No prompt version matches the requested ``prompt_id[@version]``."""


class PromptRegistrationError(Exception):
    """A new prompt version could not be written (retry budget exhausted)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_from_row(row: sqlite3.Row) -> dict[str, Any]:
    """Project a registry row into a PromptAsset record dict.

    Content fields come from the frozen ``spec_json``; the mutable lifecycle
    ``status`` comes from the row (promotion updates the row, never the
    frozen record).
    """
    record: dict[str, Any] = json.loads(row["spec_json"])
    record["status"] = row["status"]
    return record


def _prompt_version_rows(
    conn: sqlite3.Connection, prompt_id: str
) -> list[tuple[int, sqlite3.Row]]:
    """All managed prompt-version rows for ``prompt_id``, version-ascending.

    Rows whose ``version`` is not a plain integer (e.g. a legacy SemVer
    ``prompt`` asset registered via ``nova register``) are not managed
    versions and are ignored.
    """
    rows = conn.execute(
        "SELECT * FROM assets WHERE name = ? AND asset_type = 'prompt'",
        (prompt_id,),
    ).fetchall()
    versioned: list[tuple[int, sqlite3.Row]] = []
    for row in rows:
        try:
            versioned.append((int(row["version"]), row))
        except (TypeError, ValueError):
            continue
    versioned.sort(key=lambda pair: pair[0])
    return versioned


def _next_prompt_version(
    conn: sqlite3.Connection,
    prompt_id: str,
    existing: list[tuple[int, sqlite3.Row]],
) -> int:
    """One past the highest version the registry has *ever* issued for ``prompt_id``.

    Counts the live rows **and** every ``asset_status_history`` row, so a version
    that was unregistered (its row deleted) is never handed to new content. Before
    review r6 this counted live rows only, and a retired production version number
    was reissued with different content.
    """
    issued = [version for version, _ in existing]
    for (raw,) in conn.execute(
        "SELECT DISTINCT version FROM asset_status_history WHERE asset_name = ?",
        (prompt_id,),
    ).fetchall():
        try:
            issued.append(int(raw))
        except (TypeError, ValueError):
            continue  # a legacy SemVer prompt asset is not a managed version
    return max(issued, default=0) + 1


def register_prompt_version(
    prompt_id: str,
    template: PromptTemplate,
    variables: list[str] | None = None,
    config: dict[str, Any] | None = None,
    commit_message: str = "",
    db_path: Path | None = None,
    *,
    actor: str = "",
) -> tuple[dict[str, Any], bool]:
    """Register a new immutable prompt version (or return the existing one).

    Returns ``(record, created)`` where ``created`` is ``False`` when the
    canonical body already exists for this ``prompt_id`` (idempotent
    register — same content, same version; spec §Edge cases).

    A template carrying ``{{@prompt:...}}`` composition references
    (ADR-0115) is validated fail-closed **before** any row is written:
    unknown references, cycles, and over-depth trees raise a named
    ``CompositionError`` and the version never enters the registry. Each
    direct reference is snapshotted (version + content hash it resolved to
    now) into the frozen ``composition`` block.

    Version numbers are immutable (ADR-0312 Amendment 3, applied to prompts in
    review r6): the new number skips every version the status history ever
    recorded, a version that was in production is refused with
    :class:`~novafabric.registry.service.VersionReuseError`, and the insert writes
    a ``register`` history row bound to the new row id and spec digest, with
    *actor* as ``changed_by``, in the same transaction.
    """
    from novafabric.registry.service import (  # noqa: PLC0415 — service is heavy
        REGISTRATION_STATUS,
        _require_unused_production_version,
    )

    template = validate_template(template)
    content_hash = compute_content_hash(template, variables, config)
    conn = get_connection(db_path)
    init_schema(conn)
    try:
        existing = _prompt_version_rows(conn, prompt_id)
        for _, row in existing:
            record = _record_from_row(row)
            if record.get("content_hash") == content_hash:
                return record, False

        composition: list[CompositionRef] = []
        if has_composition_refs(template):
            # Lazy import: registry.composition builds on this module.
            from novafabric.registry.composition import validate_composition

            composition = validate_composition(template, db_path=db_path)

        next_version = _next_prompt_version(conn, prompt_id, existing)
        for _ in range(_REGISTER_RETRIES):
            _require_unused_production_version(conn, prompt_id, str(next_version))
            asset = PromptAsset(
                prompt_id=prompt_id,
                version=next_version,
                content_hash=content_hash,
                template=template,
                variables=sorted(variables or []),
                config=config or {},
                composition=composition,
                commit_message=commit_message,
                created_at=_now(),
                status=REGISTRATION_STATUS,
            )
            # A prompt with no references omits the block entirely (spec
            # §Data model) — the stored record is unchanged from ADR-0112.
            dump_exclude = None if composition else {"composition"}
            asset_id = str(uuid.uuid4())
            spec_json = asset.model_dump_json(exclude=dump_exclude)
            try:
                conn.execute(
                    """
                    INSERT INTO assets
                        (id, name, asset_type, version, status, spec_json,
                         git_commit_sha, created_at, forced_promotion)
                    VALUES (?, ?, 'prompt', ?, ?, ?, NULL, ?, 0)
                    """,
                    (
                        asset_id,
                        prompt_id,
                        str(asset.version),
                        asset.status.value,
                        spec_json,
                        asset.created_at,
                    ),
                )
            except sqlite3.IntegrityError:
                # Concurrent writer claimed this version — retry with the next.
                conn.rollback()
                next_version += 1
                continue
            record_status_change(
                conn,
                asset_name=prompt_id,
                version=str(asset.version),
                from_status=None,
                to_status=asset.status.value,
                changed_at=asset.created_at,
                changed_by=actor,
                via="register",
                asset_id=asset_id,
                spec_sha256=spec_digest(spec_json),
            )
            conn.commit()
            return asset.model_dump(mode="json", exclude=dump_exclude), True
        raise PromptRegistrationError(
            f"Could not register prompt '{prompt_id}' after "
            f"{_REGISTER_RETRIES} attempts (concurrent writers)."
        )
    finally:
        conn.close()


def get_prompt_version(
    prompt_id: str,
    version: int | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Fetch one prompt version record (latest when ``version`` is None)."""
    conn = get_connection(db_path)
    init_schema(conn)
    try:
        rows = _prompt_version_rows(conn, prompt_id)
        if not rows:
            raise PromptNotFoundError(f"Prompt '{prompt_id}' not found.")
        if version is None:
            return _record_from_row(rows[-1][1])
        for v, row in rows:
            if v == version:
                return _record_from_row(row)
        raise PromptNotFoundError(f"Prompt '{prompt_id}@{version}' not found.")
    finally:
        conn.close()


def list_prompt_versions(
    prompt_id: str,
    status: str | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """All versions of one prompt, ascending; optional lifecycle filter."""
    conn = get_connection(db_path)
    init_schema(conn)
    try:
        rows = _prompt_version_rows(conn, prompt_id)
        if not rows:
            raise PromptNotFoundError(f"Prompt '{prompt_id}' not found.")
        records = [_record_from_row(row) for _, row in rows]
        if status:
            records = [r for r in records if r["status"] == status]
        return records
    finally:
        conn.close()


def list_prompts(
    status: str | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """One summary per managed prompt: latest version, status, version count."""
    conn = get_connection(db_path)
    init_schema(conn)
    try:
        names = [
            row["name"]
            for row in conn.execute(
                "SELECT DISTINCT name FROM assets "
                "WHERE asset_type = 'prompt' ORDER BY name"
            ).fetchall()
        ]
        summaries: list[dict[str, Any]] = []
        for name in names:
            rows = _prompt_version_rows(conn, name)
            if not rows:  # only legacy non-integer-version prompt rows
                continue
            latest = _record_from_row(rows[-1][1])
            if status and latest["status"] != status:
                continue
            summaries.append(
                {
                    "prompt_id": name,
                    "latest_version": latest["version"],
                    "status": latest["status"],
                    "content_hash": latest["content_hash"],
                    "versions": len(rows),
                    "created_at": latest["created_at"],
                }
            )
        return summaries
    finally:
        conn.close()


def frozen_ref(record: dict[str, Any]) -> str:
    """The replay-verifiable reference for a record dict (ADR-0112 D2)."""
    return (
        f"prompt:{record['prompt_id']}@{record['version']}"
        f"+{record['content_hash']}"
    )
