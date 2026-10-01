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
"""Digest-pinned legacy-object inventory for encrypted stores (ADR-0295, experimental).

An encrypted object store refuses non-envelope objects by default (ADR-0290 D4)
because a reader cannot tell "written before encryption was enabled" from
"plaintext substituted by the storage operator". A *time* watermark cannot
close that gap: the only per-object time a reader sees is inner-store
metadata, which the storage operator controls.

The inventory records the watermark as the **set of legacy objects present at
cut-over**, each pinned to the SHA-256 of its stored bytes. It is built once,
over the bare inner adapter, and kept beside the KEK — never in the inner
store. On read, a legacy object (plaintext, or a v1 envelope under strict
mode) is admitted only when its key is listed **and** its stored bytes still
hash to the pinned digest, so substitution of a pre-existing object is caught
as well as new plaintext.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from novafabric.object_capsule_store.worm.base import WormAdapter

__all__ = [
    "INVENTORY_FORMAT",
    "LegacyInventory",
    "LegacyInventoryError",
    "build_legacy_inventory",
    "load_legacy_inventory",
]

#: Format tag written into, and required from, every inventory file.
INVENTORY_FORMAT = "novafabric/legacy-object-inventory/v1"

_HEX = frozenset("0123456789abcdef")


class LegacyInventoryError(ValueError):
    """The inventory file is unreadable, malformed, or fails its digest pin."""


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_sha256_hex(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX


@dataclass(frozen=True)
class LegacyInventory:
    """Immutable ``{object key: sha256-of-stored-bytes}`` map (ADR-0295 D1)."""

    objects: Mapping[str, str]
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __len__(self) -> int:
        return len(self.objects)

    def __contains__(self, key: object) -> bool:
        return key in self.objects

    def admits(self, key: str, stored: bytes) -> bool:
        """True iff *key* is listed and *stored* hashes to its pinned digest."""
        pinned = self.objects.get(key)
        return pinned is not None and pinned == _sha256_hex(stored)

    def to_json(self) -> str:
        """Canonical JSON (sorted keys) — stable bytes for the digest pin."""
        return json.dumps(
            {
                "format": INVENTORY_FORMAT,
                "created_at": self.created_at,
                "objects": dict(sorted(self.objects.items())),
            },
            sort_keys=True,
            indent=1,
        )


def build_legacy_inventory(
    inner: WormAdapter, prefixes: Iterable[str] = ("",)
) -> LegacyInventory:
    """Inventory every legacy object under *prefixes* on the **bare** inner adapter.

    Records non-envelope objects and v1 (unbound) envelopes; skips v2 envelopes
    (already bound) and the never-encrypted chain-log namespace. Run it at the
    cut-over, from a trusted snapshot: an inventory taken after an attacker
    substituted objects would pin the substitutes.
    """
    from novafabric.object_capsule_store.encryption_wrapper import (
        CHAIN_LOG_PREFIX,
        EncryptingAdapter,
    )

    if isinstance(inner, EncryptingAdapter):
        raise LegacyInventoryError(
            "build the inventory over the bare inner adapter, not an EncryptingAdapter "
            "(the wrapper decrypts and would hash plaintext, not stored bytes)"
        )
    objects: dict[str, str] = {}
    for prefix in prefixes:
        for key in inner.iter_objects(prefix):
            if key.startswith(CHAIN_LOG_PREFIX) or key in objects:
                continue
            raw = inner.get_object(key)
            blob = EncryptingAdapter._parse_envelope(raw)
            if blob is not None and blob.is_bound:
                continue
            objects[key] = _sha256_hex(raw)
    return LegacyInventory(objects=objects)


def load_legacy_inventory(path: Path, *, expected_sha256: str | None = None) -> LegacyInventory:
    """Load and validate an inventory file; fail closed on any defect.

    *expected_sha256*, when given, must equal the SHA-256 of the file bytes
    (``NOVA_OBJECT_STORE_LEGACY_INVENTORY_SHA256``) — tampering between
    restarts then refuses startup instead of widening what is admitted.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LegacyInventoryError(
            f"cannot read legacy-object inventory {path}: {type(exc).__name__}"
        ) from exc
    if expected_sha256 is not None:
        want = expected_sha256.strip().lower()
        if not _is_sha256_hex(want):
            raise LegacyInventoryError("inventory digest pin is not a SHA-256 hex string")
        if _sha256_hex(raw) != want:
            raise LegacyInventoryError(
                f"legacy-object inventory {path} does not match its pinned SHA-256; "
                "refusing to start (ADR-0295)"
            )
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise LegacyInventoryError(f"legacy-object inventory {path} is not JSON") from exc
    if not isinstance(data, dict) or data.get("format") != INVENTORY_FORMAT:
        raise LegacyInventoryError(
            f"legacy-object inventory {path} lacks format {INVENTORY_FORMAT!r}"
        )
    objects = data.get("objects")
    if not isinstance(objects, dict):
        raise LegacyInventoryError(f"legacy-object inventory {path}: 'objects' is not a map")
    for key, digest in objects.items():
        if not isinstance(key, str) or not key or not _is_sha256_hex(digest):
            raise LegacyInventoryError(
                f"legacy-object inventory {path}: malformed entry for key {key!r}"
            )
    created_at = data.get("created_at")
    return LegacyInventory(
        objects=dict(objects),
        created_at=str(created_at) if created_at is not None else "",
    )
