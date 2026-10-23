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
"""Local WORM locks for deletion gates, read fail-closed (ADR-0031 §4, PR #158).

Every path that deletes capsule data reads WORM locks through this module:
``nova capsule delete``, ``DELETE /v0/capsules/{id}``, ``POST
/v0/capsules/bulk-delete``, serve's ``DELETE /api/runs/{id}``, metadata-index
deletion (all via :mod:`novafabric.server.capsule_delete`), and ``nova retention
plan|apply|status|explain`` including the apply-time re-check.

**Configured.** Local WORM state is configured when a ``worm.db`` entry exists in
any registry location the hold store uses (:func:`novafabric.retention.holds.hold_roots`:
``$NOVAFABRIC_HOME/registries/<r>/``, the legacy ``./.novafabric/registries/<r>/`` and
the capsule base's ``<capsule_dir>/../registries/<r>/``), or when the operator names
one with ``nova retention --worm-db``. No such entry anywhere means no local WORM is
configured, and deletion is not blocked by WORM.

**Fail closed.** A configured database that cannot be read -- missing (when named
explicitly), not a regular file, empty, not SQLite, without its table, with a row
that does not parse, permission denied, or locked -- raises
:class:`WormStateUnavailableError`. "We could not read it" never reads as "there
is no lock". There is no override: the operator repairs or restores the database.

The local adapter is dev/test-only (ADR-0031): this check protects against a
damaged or misplaced file, not against an administrator who edits it.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from novafabric.retention.holds import (
    WORM_DB_FILENAME,
    RegistryStateUnreadableError,
    entry_present,
    hold_roots,
    registry_file_entries,
)
from novafabric.storage._local_worm import WormDatabaseUnreadableError, read_worm_entries

logger = logging.getLogger(__name__)

#: Refusal code every deletion surface reports (HTTP 409 on the server routes).
WORM_STATE_UNAVAILABLE = "worm_state_unavailable"


class WormStateUnavailableError(Exception):
    """Configured local WORM state cannot be read, so no deletion may proceed."""

    code = WORM_STATE_UNAVAILABLE

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(
            f"{WORM_STATE_UNAVAILABLE}: WORM database {path} is configured but cannot "
            f"be read ({reason}); nothing was deleted. Repair or restore it, then retry."
        )
        self.path = path
        self.reason = reason


def worm_db_paths(
    registry: str | None = None, *, extra_roots: Iterable[Path] = ()
) -> list[Path]:
    """Every configured ``worm.db`` entry, canonical root first.

    *registry* ``None`` means every registry directory under every root (the
    delete core: holds and WORM are consulted across registries).

    Raises:
        WormStateUnavailableError: a root or registry directory exists but cannot
            be listed or searched, so a ``worm.db`` in it cannot be ruled out.
    """
    try:
        if registry is not None:
            return registry_file_entries(registry, WORM_DB_FILENAME, extra_roots=extra_roots)
        found: list[Path] = []
        for root in hold_roots(extra_roots):
            try:
                names = sorted(os.listdir(root))
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError as exc:
                raise RegistryStateUnreadableError(root, f"cannot be listed: {exc}") from exc
            # A non-directory entry yields NotADirectoryError, i.e. "absent".
            found.extend(
                candidate
                for name in names
                if entry_present(candidate := root / name / WORM_DB_FILENAME)
            )
        return found
    except RegistryStateUnreadableError as exc:
        raise WormStateUnavailableError(exc.path, exc.reason) from exc


def read_worm_locks(
    paths: Iterable[Path], *, required: Iterable[Path] = ()
) -> dict[str, datetime]:
    """Every lock in *paths* and *required*; the latest ``locked_until`` wins per capsule.

    *paths* are discovered entries (:func:`worm_db_paths`); one that has vanished
    since discovery is skipped. *required* are databases the operator named
    explicitly, which must exist.

    Raises:
        WormStateUnavailableError: a database cannot be read (see module docstring).
    """
    locks: dict[str, datetime] = {}
    must_exist = list(dict.fromkeys(required))
    for path in dict.fromkeys([*must_exist, *paths]):
        try:
            if path not in must_exist and not entry_present(path):
                continue
            entries = read_worm_entries(path)
        except (RegistryStateUnreadableError, WormDatabaseUnreadableError) as exc:
            logger.error(
                "WORM database %s unreadable (%s): refusing deletion (fail closed)",
                path,
                exc.reason,
            )
            raise WormStateUnavailableError(path, exc.reason) from exc
        for entry in entries:
            current = locks.get(entry.capsule_id)
            if current is None or entry.locked_until > current:
                locks[entry.capsule_id] = entry.locked_until
    return locks
