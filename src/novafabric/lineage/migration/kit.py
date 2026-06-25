"""E-3: Migration engine — reads Parquet or ObjectCapsuleStore, writes to target store.

Two migration sources are supported:

  1. ``migrate()`` — reads from a Parquet file (deprecated; kept for backward
     compat with existing tooling).  Emits ``DeprecationWarning`` on call.
  2. ``migrate_from_ocs()`` — reads lineage edges from every capsule archived in
     an ``ObjectCapsuleStore`` by unpacking the ``lineage.jsonl`` file inside
     each capsule ZIP archive.  This is the ADR-0022-compliant path: lineage
     data is derived from the ObjectCapsuleStore manifest chain, not from an
     external Parquet dump.

Both functions share the same validate-queries divergence check and
``--commit`` / dry-run semantics.
"""

from __future__ import annotations

import io
import json
import logging
import warnings
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyarrow.parquet as pq

from novafabric.lineage._types import LineageEdge
from novafabric.lineage.migration.validate import validate_stores
from novafabric.lineage.store import AbstractLineageStore

if TYPE_CHECKING:
    from novafabric.object_capsule_store.client import ObjectCapsuleStore

log = logging.getLogger(__name__)


class MigrationDivergenceError(Exception):
    """Raised when validate=True and divergence is detected after loading."""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _parquet_row_to_edge(row: dict[str, Any]) -> LineageEdge:
    """Reconstruct a LineageEdge from a Parquet row dict."""
    source_run_id = str(row.get("source_run_id", ""))
    target_run_id = str(row.get("target_run_id", ""))
    return LineageEdge(
        edge_id=str(row.get("edge_id", "")),
        edge_type=str(row.get("edge_type", "")),
        source={"kind": "run", "run_id": source_run_id},
        target={"kind": "run", "run_id": target_run_id},
        confidence=str(row.get("confidence", "medium")),
        capsule_run_id=str(row.get("capsule_run_id", "")),
        schema_version=str(row.get("schema_version", "0.1.0")),
        created_at=str(row.get("created_at", "")),
    )


def _edges_from_lineage_jsonl(data: str | bytes, capsule_run_id: str) -> list[LineageEdge]:
    """Parse lineage.jsonl content and return a list of ``LineageEdge`` objects.

    Each non-empty line is expected to be a JSON object conforming to the
    lineage-edge schema.  Malformed lines are skipped with a warning.

    Args:
        data:           Raw text (str) or UTF-8 bytes of a ``lineage.jsonl`` file.
        capsule_run_id: The run_id of the capsule that owns this lineage file.
                        Used as a fallback when the edge record does not carry
                        its own ``capsule_run_id``.

    Returns:
        List of ``LineageEdge`` objects (may be empty).
    """
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    edges: list[LineageEdge] = []
    for lineno, line in enumerate(data.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            record: dict[str, Any] = json.loads(line)
            edge = LineageEdge(
                edge_id=str(record.get("edge_id", "")),
                edge_type=str(record.get("edge_type", "")),
                source=record.get("source", {}),
                target=record.get("target", {}),
                confidence=str(record.get("confidence", "medium")),
                capsule_run_id=str(record.get("capsule_run_id", capsule_run_id)),
                schema_version=str(record.get("schema_version", "0.1.0")),
                created_at=str(record.get("created_at", "")),
            )
            edges.append(edge)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            log.warning(
                "lineage.jsonl line %d in capsule %r skipped: %s",
                lineno, capsule_run_id, exc,
            )
    return edges


def _edges_from_capsule_bytes(capsule_bytes: bytes, capsule_run_id: str) -> list[LineageEdge]:
    """Extract lineage edges from a capsule ZIP archive.

    Capsule archives store lineage as a ``lineage.jsonl`` member file inside
    the ZIP.  If no such file exists, an empty list is returned (capsules may
    legitimately have no lineage edges).

    Args:
        capsule_bytes:  Raw bytes of the capsule ZIP archive.
        capsule_run_id: The run_id associated with this capsule.

    Returns:
        List of ``LineageEdge`` objects extracted from ``lineage.jsonl``.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(capsule_bytes)) as zf:
            names = zf.namelist()
            # Accept both bare "lineage.jsonl" and prefixed "<run_id>/lineage.jsonl"
            matches = [n for n in names if n == "lineage.jsonl" or n.endswith("/lineage.jsonl")]
            if not matches:
                return []
            # Use the first match (there should only be one)
            raw = zf.read(matches[0])
            return _edges_from_lineage_jsonl(raw, capsule_run_id)
    except zipfile.BadZipFile:
        log.warning(
            "Capsule %r is not a valid ZIP archive — skipping lineage extraction",
            capsule_run_id,
        )
        return []
    except Exception as exc:
        log.warning(
            "Failed to extract lineage from capsule %r: %s — skipping",
            capsule_run_id, exc,
        )
        return []


def _run_divergence_check(
    edges: list[LineageEdge],
    target: AbstractLineageStore,
    run_ids_sample: list[str] | None,
    run_id_set: set[str],
) -> int:
    """Run the validate-queries divergence check.

    Builds a temporary SQLite store from *edges* and compares provenance /
    blast_radius / replay_chain query results against *target*.

    Returns the number of diverged query results.  Raises
    ``MigrationDivergenceError`` if diverged > 0.
    """
    import tempfile

    from novafabric.lineage.backends.sqlite import SqliteLineageStore

    sample = run_ids_sample if run_ids_sample is not None else list(run_id_set)
    tmp_db = Path(tempfile.mkdtemp()) / "migration_src.db"
    source_store = SqliteLineageStore(db_path=tmp_db)
    for edge in edges:
        source_store.insert(edge)

    reports = validate_stores(source_store, target, sample, max_depth=5)
    diverged = len(reports)
    if diverged > 0:
        raise MigrationDivergenceError(
            f"Migration divergence detected: {diverged} query result(s) differ "
            f"between source and target. Reports: {reports}"
        )
    return diverged


# ---------------------------------------------------------------------------
# Primary migration path: ObjectCapsuleStore (ADR-0022)
# ---------------------------------------------------------------------------


def migrate_from_ocs(
    ocs: "ObjectCapsuleStore",
    tenant: str,
    dest_store: AbstractLineageStore,
    *,
    run_ids: list[str] | None = None,
    validate: bool = True,
    commit: bool = False,
    run_ids_sample: list[str] | None = None,
) -> dict[str, Any]:
    """Migrate lineage edges from an ObjectCapsuleStore to a target lineage store.

    Derives lineage data from the OCS manifest chain per ADR-0022: for each
    capsule committed in the store, unpacks the capsule ZIP archive and reads
    the ``lineage.jsonl`` member file.  This is the canonical, recommended
    migration path.

    Algorithm:
      1. Call ``ocs.list_capsules(tenant, run_id)`` for each run in *run_ids*
         (or enumerate all run_ids visible in the OCS when *run_ids* is None).
      2. For each capsule URI, call ``ocs.get_capsule(tenant, run_id, uri)`` to
         fetch the capsule bytes.
      3. Unpack the ZIP archive and parse ``lineage.jsonl``.
      4. Accumulate all ``LineageEdge`` objects.
      5. Optionally commit them to *dest_store* and run a divergence check.

    Args:
        ocs:            ``ObjectCapsuleStore`` instance to read from.
        tenant:         Tenant identifier used for all OCS look-ups.
        dest_store:     Target ``AbstractLineageStore`` to write edges into.
        run_ids:        Explicit list of run_ids to migrate.  When ``None``,
                        the caller must provide them; there is no global
                        enumeration API on OCS (by design — FR-05).
        validate:       When ``True`` (default), run the divergence check after
                        writing.  Raises ``MigrationDivergenceError`` on
                        divergence.
        commit:         When ``False`` (default), perform a dry run: count edges
                        but do not write to *dest_store*.
        run_ids_sample: Optional subset of run_ids to use for the divergence
                        check.  Defaults to all run_ids encountered.

    Returns:
        ``{"loaded": int, "diverged": int, "committed": bool, "capsules_scanned": int}``.

    Raises:
        MigrationDivergenceError: if *validate* is ``True`` and divergence is
                                  detected between the reconstructed source and
                                  *dest_store*.
    """
    if run_ids is None:
        run_ids = []
    if not run_ids:
        log.warning(
            "migrate_from_ocs: run_ids is empty — no capsules will be scanned. "
            "Pass an explicit list of run_ids to migrate."
        )
        return {"loaded": 0, "diverged": 0, "committed": False, "capsules_scanned": 0}

    all_edges: list[LineageEdge] = []
    run_id_set: set[str] = set()
    capsules_scanned = 0

    for run_id in run_ids:
        uris = ocs.list_capsules(tenant=tenant, run_id=run_id)
        for uri in uris:
            capsules_scanned += 1
            capsule_bytes = ocs.get_capsule(tenant=tenant, run_id=run_id, capsule_uri=uri)
            edges = _edges_from_capsule_bytes(capsule_bytes, capsule_run_id=run_id)
            for edge in edges:
                all_edges.append(edge)
                if isinstance(edge.source, dict):
                    src_run_id = edge.source.get("run_id", "")
                    if src_run_id:
                        run_id_set.add(src_run_id)
                if isinstance(edge.target, dict):
                    tgt_run_id = edge.target.get("run_id", "")
                    if tgt_run_id:
                        run_id_set.add(tgt_run_id)

    loaded = len(all_edges)

    if not commit:
        return {
            "loaded": loaded,
            "diverged": 0,
            "committed": False,
            "capsules_scanned": capsules_scanned,
        }

    for edge in all_edges:
        dest_store.insert(edge)

    diverged = 0
    if validate and all_edges:
        diverged = _run_divergence_check(all_edges, dest_store, run_ids_sample, run_id_set)

    return {
        "loaded": loaded,
        "diverged": diverged,
        "committed": True,
        "capsules_scanned": capsules_scanned,
    }


# ---------------------------------------------------------------------------
# Deprecated fallback: Parquet-backed migration
# ---------------------------------------------------------------------------


def migrate(
    source_parquet: Path,
    target: AbstractLineageStore,
    validate: bool = True,
    commit: bool = False,
    run_ids_sample: list[str] | None = None,
) -> dict[str, Any]:
    """Migrate edges from a Parquet file to a target lineage store.

    .. deprecated::
        Use :func:`migrate_from_ocs` instead.  Parquet-backed migration is a
        legacy path that does not derive lineage from the authoritative
        ObjectCapsuleStore manifest chain (ADR-0022).  This function will be
        removed in a future release.

    Returns ``{"loaded": int, "diverged": int, "committed": bool}``.
    """
    warnings.warn(
        "migrate() reads lineage from a Parquet file, which is not the "
        "authoritative source per ADR-0022.  Use migrate_from_ocs() to derive "
        "lineage edges directly from the ObjectCapsuleStore manifest chain. "
        "This fallback will be removed in a future release.",
        DeprecationWarning,
        stacklevel=2,
    )

    table = pq.read_table(str(source_parquet))  # type: ignore[no-untyped-call]
    edges: list[LineageEdge] = []
    run_id_set: set[str] = set()

    for batch in table.to_batches():
        for i in range(batch.num_rows):
            row = {col: batch.column(col)[i].as_py() for col in batch.schema.names}
            edge = _parquet_row_to_edge(row)
            edges.append(edge)
            run_id_set.add(str(row.get("source_run_id", "")))
            run_id_set.add(str(row.get("target_run_id", "")))

    loaded = len(edges)

    if not commit:
        return {"loaded": loaded, "diverged": 0, "committed": False}

    # Write all edges to the target store.
    for edge in edges:
        target.insert(edge)

    diverged = 0
    if validate:
        diverged = _run_divergence_check(edges, target, run_ids_sample, run_id_set)

    return {"loaded": loaded, "diverged": diverged, "committed": True}
