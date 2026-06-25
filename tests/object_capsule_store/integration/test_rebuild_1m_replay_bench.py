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
"""1M-capsule rebuild bench (gated by NOVA_BENCH=1)."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("NOVA_BENCH") != "1",
    reason="1M bench requires NOVA_BENCH=1",
)


def test_rebuild_1m_capsules_bench() -> None:
    """FR-04: 1M-capsule rebuild opt-in bench (NOVA_BENCH=1)."""
    from novafabric.object_capsule_store.backend_router import InMemoryWormAdapter
    from novafabric.object_capsule_store.manifest_chain import ManifestChainWriter
    from novafabric.object_capsule_store.rebuild import rebuild_metadata_db

    adapter = InMemoryWormAdapter()
    writer = ManifestChainWriter(adapter)

    # 1,000 runs × 1,000 capsules = 1,000,000
    for i in range(1_000):
        run_id = f"run-{i:04d}"
        for j in range(1_000):
            sha = f"{(i * 1_000 + j):064x}"
            writer.append(
                tenant="bench",
                run_id=run_id,
                capsule_uri=f"capsules/bench/{sha[:4]}/{sha}/data.zst",
                capsule_sha256=sha,
            )

    report = rebuild_metadata_db(adapter, prefix="bench", target_db=":memory:")
    assert report.capsules_found == 1_000_000
    # No hard timing requirement for the bench — just report
    print(f"\n1M rebuild: {report.time_to_replay_seconds:.2f}s")
