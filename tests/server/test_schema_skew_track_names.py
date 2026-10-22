"""The schema-skew guard's operator text, and the container migration path.

L2 — E-SKEW-EMPTY told the operator to run ``nova db upgrade --backend
postgres`` WITHOUT ``--track registry``. ``nova db upgrade`` defaults to
``--track metadata`` (the MetadataStore tier, ``NOVAFABRIC_METADATA_DSN``), so
following the fix text migrated a different schema and left the guarded
registry database exactly as empty as before. Every remediation the guard
prints must name the track the guard checks.

Container migration path (audit "unconfirmed" item, confirmed by reading;
pinned below as a strict xfail): the Docker entrypoint and the Helm init
container run ``nova db upgrade --backend postgres`` — the MetadataStore track —
against the SAME Postgres database the server's guard reads (both
``NOVAFABRIC_METADATA_DSN`` and ``NOVAFABRIC_POSTGRES_DSN`` point at it), and
both alembic tracks use the default ``alembic_version`` table. So the database
is stamped with the MetadataStore head, a revision the registry track does not
know, and the guard classifies it ``ahead_or_foreign``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from novafabric.server import schema_skew
from novafabric.server.schema_skew import (
    STATUS_AHEAD,
    STATUS_BEHIND,
    SkewReport,
    compare_revisions,
    empty_backend_message,
)


def test_empty_backend_fix_names_the_registry_track() -> None:
    msg = empty_backend_message("postgres")
    assert "nova db upgrade --track registry --backend postgres" in msg
    # The bare command is the MetadataStore track — it must not be the advice.
    assert "`nova db upgrade --backend postgres`" not in msg


@pytest.mark.parametrize("status", [STATUS_BEHIND])
def test_every_upgrade_remediation_names_the_registry_track(status: str) -> None:
    report = SkewReport(
        status=status,  # type: ignore[arg-type]
        backend="postgres",
        track="registry",
        db_revision="p0001",
        head_revision="p0003",
        detail="",
    )
    assert "--track registry" in report.remediation
    assert "--track registry" in schema_skew.behind_message(report)


def _metadata_track_head() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    import novafabric.metadata_store as ms

    cfg = Config()
    cfg.set_main_option(
        "script_location", str(Path(ms.__file__).parent / "migrations" / "postgres")
    )
    head = ScriptDirectory.from_config(cfg).get_current_head()
    assert head is not None
    return head


@pytest.mark.xfail(
    strict=True,
    reason=(
        "known defect, needs an ADR: the MetadataStore and registry alembic tracks "
        "share one `alembic_version` table in the same Postgres database, so the "
        "container/Helm migration step stamps a revision the server's guard "
        "rejects (E-SKEW-AHEAD). Fix = a distinct version_table per track plus a "
        "migration for databases already stamped."
    ),
)
def test_a_database_migrated_by_the_container_entrypoint_passes_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stamped_by_entrypoint = _metadata_track_head()
    monkeypatch.setattr(
        schema_skew,
        "_read_stamp_postgres",
        lambda _dsn: (stamped_by_entrypoint, schema_skew._STAMPED),
    )
    report = compare_revisions("postgres", "postgresql://unused")
    assert report.status != STATUS_AHEAD, (
        f"stamp {stamped_by_entrypoint!r} (MetadataStore head) is foreign to the "
        f"registry track (head {report.head_revision!r})"
    )
