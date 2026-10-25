"""Merkle-log locations: libpq keyword DSNs and ``@`` in passwords (review trust-evidence-16).

``merkle_db: "host=db user=nova password=hunter2 dbname=x"`` (a form psycopg
accepts) became ``Path(...)``, so sealing created a SQLite file in the current
directory named after the password. ``redact_location`` stopped the password at
its first ``@``: ``postgresql://nova:s3cr@t@db/x`` printed ``…:***@t@db/x``.
"""
from __future__ import annotations

import pytest

from novafabric.trust.novaseal.log_location import (
    MerkleLocationError,
    normalize_location,
    redact_location,
)


@pytest.mark.parametrize(
    "dsn",
    [
        "host=db user=nova password=hunter2 dbname=x",
        "dbname=nova",
        "  user=nova host=db",
        "host=db port=5432 password='a b'",
    ],
)
def test_a_libpq_keyword_dsn_is_refused_never_a_file(dsn: str) -> None:
    with pytest.raises(MerkleLocationError) as exc:
        normalize_location(dsn)
    assert "hunter2" not in str(exc.value) and "a b" not in str(exc.value)
    assert "postgresql://" in str(exc.value)


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        ("postgresql://nova:s3cr@t@db/x", "postgresql://nova:***@db/x"),
        ("postgres://nova:p@ss@w0rd@db:5432/x?sslmode=require", "postgres://nova:***@db:5432/x?sslmode=require"),
        ("postgresql://nova:plain@db/x", "postgresql://nova:***@db/x"),
        ("postgresql://nova@db/x", "postgresql://nova@db/x"),
    ],
)
def test_the_whole_password_is_redacted(url: str, shown: str) -> None:
    assert redact_location(url) == shown


@pytest.mark.parametrize("path", ["/var/lib/nova/merkle.db", "logs/merkle=v2.db", "~/merkle.db"])
def test_ordinary_paths_still_resolve_to_files(path: str) -> None:
    assert not isinstance(normalize_location(path), str)
