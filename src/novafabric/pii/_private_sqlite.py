"""Open a SQLite database of key material so it, and its sidecars, are 0600 from the start.

``dek.db`` holds raw per-subject AES keys and the erasure queue names data
subjects. ``sqlite3.connect`` creates a missing database at the umask mode
(commonly 0644), and SQLite gives the ``-wal``/``-shm``/``-journal`` sidecars the
database's mode. Creating the file at 0600 first (and narrowing an existing one
and any sidecar left from before) means no byte of it is ever readable by other
local users (line-by-line review compliance-privacy-01).
"""

from __future__ import annotations

from pathlib import Path

from novafabric._safe_path import ensure_private_file

_SIDECARS = ("-wal", "-shm", "-journal")


def prepare_private_database(db_path: Path) -> None:
    """Create or narrow *db_path* (and any existing sidecar) to 0600 before SQLite opens it.

    Raises:
        PathEscapeError: the database or a sidecar is a link or not a regular file.
    """
    ensure_private_file(db_path)
    for suffix in _SIDECARS:
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            ensure_private_file(sidecar)
