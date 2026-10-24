"""The ``schema_version`` every capsule writer stamps into ``capsule.yaml``.

One constant for every writer — ``nova capture``, ``@agent``, OTLP ingest, the shared
adapter core and each framework adapter — so a capsule's label says which generation of
the format it is, not which code path wrote it (review eval-adapters-query-18).

The Run Capsule format is a pre-freeze draft at ``0.1.0`` (ADR-0034 §1: the packaged
``novafabric/schemas/run-capsule.schema.json``). ``1.0.0`` is produced only by
``nova migrate`` (ADR-0034 §6), which also records the ``migration_source`` lineage
edge; the published ``schemas/run-capsule.schema.json`` is that v1 target. A writer
never stamps ``1.0.0`` itself.
"""
from __future__ import annotations

from typing import Final

CAPSULE_SCHEMA_VERSION: Final = "0.1.0"
