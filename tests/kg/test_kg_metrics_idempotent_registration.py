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
"""Re-importing ``novafabric.kg.store`` must not disable its Prometheus metrics.

A ``prometheus_client`` collector name may only be registered once per process.
The module used to wrap all four registrations in a single blanket ``except``,
so a *second* import — after a reload, after ``del sys.modules[...]``, or via a
second import path — raised ``Duplicate timeseries`` and left every metric at
``None`` for the rest of the process, silently and permanently. Registration is
now idempotent: it reuses whatever is already in the registry.
"""

from __future__ import annotations

import sys

import pytest

pytest.importorskip("prometheus_client")

METRICS = (
    "_kg_node_merge_total",
    "_kg_edge_upsert_total",
    "_kg_crdt_merge_total",
    "_kg_node_count",
)


def _fresh_import() -> object:
    sys.modules.pop("novafabric.kg.store", None)
    import novafabric.kg.store as store  # noqa: PLC0415

    return store


def test_metrics_survive_repeated_reimport() -> None:
    store = _fresh_import()
    assert all(getattr(store, name) is not None for name in METRICS)

    for _ in range(3):
        store = _fresh_import()
        missing = [name for name in METRICS if getattr(store, name) is None]
        assert not missing, f"metrics lost on re-import: {missing}"


def test_reused_collectors_are_still_usable() -> None:
    _fresh_import()
    store = _fresh_import()

    # The reused collectors must be real collectors, not placeholders.
    store._kg_node_merge_total.labels(node_type="Agent").inc()
    store._kg_edge_upsert_total.labels(edge_type="CALLED").inc()
    store._kg_crdt_merge_total.labels(edge_type="CALLED").inc()
    store._kg_node_count.set(7)


def test_reimport_returns_the_same_collector_object() -> None:
    first = _fresh_import()._kg_node_merge_total
    second = _fresh_import()._kg_node_merge_total
    assert first is not None and second is not None  # `None is None` is not reuse
    assert second is first, "a re-import must reuse the registered collector"
