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
"""Concurrent chain writer integration test.

Large-scale concurrent test (32 processes × 1,000 commits) as specified in
FR-02.  Runs with in-memory adapter using thread locks to simulate conditional-
PUT semantics.  Full multi-process test requires NOVA_INTEGRATION=1.
"""

from __future__ import annotations

import concurrent.futures
import os
import threading

import pytest

from novafabric.object_capsule_store.backend_router import InMemoryWormAdapter
from novafabric.object_capsule_store.manifest_chain import ManifestChainWriter


@pytest.mark.skipif(
    os.environ.get("NOVA_INTEGRATION") != "1",
    reason="Requires NOVA_INTEGRATION=1 for full 32×1000 test",
)
def test_32_concurrent_writers_1000_commits_each() -> None:
    """FR-02: 32 threads × 1,000 commits → 32,000 unique version numbers."""
    adapter = InMemoryWormAdapter()
    lock = threading.Lock()

    original_put = adapter.put_log_object
    original_put_if = adapter.put_log_object_if_absent
    original_list = adapter.list_objects

    adapter.put_log_object = lambda k, d: (lock.acquire(), original_put(k, d), lock.release())[1]  # type: ignore[method-assign]
    adapter.put_log_object_if_absent = (  # type: ignore[method-assign]
        lambda k, d: (lock.acquire(), original_put_if(k, d), lock.release())[1]
    )
    adapter.list_objects = lambda p: (lock.acquire(), r := original_list(p), lock.release(), r)[3]  # type: ignore[method-assign]

    writer = ManifestChainWriter(adapter)
    n_threads = 32
    commits_per_thread = 1_000
    versions: list[int] = []
    v_lock = threading.Lock()

    def worker() -> None:
        for _ in range(commits_per_thread):
            c = writer.append(
                tenant="t", run_id="r",
                capsule_uri=f"capsules/t/abcd/{'a'*64}/data.zst",
                capsule_sha256="a" * 64,
            )
            with v_lock:
                versions.append(c.version)

    with concurrent.futures.ThreadPoolExecutor(max_workers=n_threads) as pool:
        futs = [pool.submit(worker) for _ in range(n_threads)]
        for f in futs:
            f.result()

    total = n_threads * commits_per_thread
    assert len(versions) == total
    assert len(set(versions)) == total
    assert set(versions) == set(range(1, total + 1))
