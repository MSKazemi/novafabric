"""Evidence Fabric — self-contained and scale-tier accumulator stack.

Implements the Evidence Fabric v1.0 primitives (cap-001..cap-006/009,
ADR-0066).

Dependency-free backends (stdlib + already-core deps only):
  EventQueueConsumer — asyncio.Queue consumer with NATS-compatible interface

Extra-gated backends (each needs one optional dependency):
  DuckDBAccumulator      — in-process DuckDB lineage-edge + capsule-event store
                           (pip install novafabric[scale])
  LocalPIITable          — Parquet-backed PII event log with Iceberg-compat sidecar
                           (pip install novafabric[scale])
  PIIEvent               — Pydantic model for a PII detection record.  The model
                           itself needs nothing optional, but it is *defined in*
                           ``pii_table``, which imports pyarrow at module level,
                           so importing it from here needs the same extra
                           (pip install novafabric[scale])
  NATSJetStreamConsumer  — NATS JetStream pull consumer (pip install novafabric[nats])
  ClickHouseAccumulator  — ClickHouse sink for lineage + cost (pip install novafabric[clickhouse])
  AvroSerializer         — Avro binary serialization for evidence events
                           (pip install novafabric[avro])

Import contract (ADR-0222)
--------------------------
Attribute access on this package is **lazy** (PEP 562).  Importing
``novafabric.evidence_fabric`` itself pulls in nothing optional, and resolving
one name never drags in another backend's dependency — so
``from novafabric.evidence_fabric import EventQueueConsumer`` works on a plain
``pip install novafabric``.

``DuckDBAccumulator``, ``LocalPIITable`` and ``PIIEvent`` come from modules that
import their dependency at module level (``duckdb_accumulator`` → duckdb,
``pii_table`` → pyarrow), so accessing those three names raises ``ImportError``
with a ``pip install`` hint when the ``scale`` extra is absent.
``NATSJetStreamConsumer``, ``ClickHouseAccumulator`` and ``AvroSerializer``
guard their imports internally: the name always resolves and the error is
raised on instantiation instead.  Either way the failure names the exact extra
to install — never a silent wrong answer.

``EventQueueConsumer`` is the only export that needs nothing optional.

.. note::
   Before ADR-0222 this module imported all six names eagerly, which made
   *every* symbol here — including the dependency-free ones — require duckdb
   and pyarrow at import time.  The docstring also claimed
   ``DuckDBAccumulator``/``LocalPIITable`` were "self-contained (no optional
   deps required)", which was never true: duckdb and pyarrow only looked
   non-optional because they were unconditional core dependencies.
   A first pass at this rewrite then mis-filed ``PIIEvent`` as dependency-free
   — it is a plain Pydantic model, but it lives in ``pii_table`` next to the
   pyarrow import, so importing it from this package needs ``[scale]`` too.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing-only, never executed at runtime
    from novafabric.evidence_fabric.avro_serializer import AvroSerializer
    from novafabric.evidence_fabric.clickhouse_accumulator import ClickHouseAccumulator
    from novafabric.evidence_fabric.duckdb_accumulator import DuckDBAccumulator
    from novafabric.evidence_fabric.nats_consumer import NATSJetStreamConsumer
    from novafabric.evidence_fabric.pii_table import LocalPIITable, PIIEvent
    from novafabric.evidence_fabric.queue_consumer import EventQueueConsumer

__all__ = [
    "AvroSerializer",
    "ClickHouseAccumulator",
    "DuckDBAccumulator",
    "EventQueueConsumer",
    "LocalPIITable",
    "NATSJetStreamConsumer",
    "PIIEvent",
]

# name -> (submodule, extra that provides its dependency or None if unconditional)
_LAZY_EXPORTS: dict[str, tuple[str, str | None]] = {
    "AvroSerializer": ("avro_serializer", "avro"),
    "ClickHouseAccumulator": ("clickhouse_accumulator", "clickhouse"),
    "DuckDBAccumulator": ("duckdb_accumulator", "scale"),
    "EventQueueConsumer": ("queue_consumer", None),
    "LocalPIITable": ("pii_table", "scale"),
    "NATSJetStreamConsumer": ("nats_consumer", "nats"),
    "PIIEvent": ("pii_table", "scale"),
}


def __getattr__(name: str) -> Any:
    """Resolve an Evidence Fabric export on first access (PEP 562).

    Keeping this lazy is what lets a plain install import the package and its
    dependency-free members.  A missing optional dependency is re-raised with
    the exact extra to install, matching the ``_require_clickhouse()``-style
    hints used inside the scale-tier modules themselves.
    """
    try:
        module_name, extra = _LAZY_EXPORTS[name]
    except KeyError:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        ) from None

    import importlib

    try:
        module = importlib.import_module(f"{__name__}.{module_name}")
    except ImportError as exc:
        if extra is None:
            raise
        raise ImportError(
            f"{name} requires an optional dependency that is not installed "
            f"({exc}). Install it with: pip install novafabric[{extra}]"
        ) from exc
    value = getattr(module, name)
    globals()[name] = value  # cache: subsequent lookups skip __getattr__
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
