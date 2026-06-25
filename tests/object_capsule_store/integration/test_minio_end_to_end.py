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
"""MinIO end-to-end integration test.

Requires a running MinIO instance and MINIO_ENDPOINT env var.
Skipped unless ``NOVA_INTEGRATION=1`` is set.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("NOVA_INTEGRATION") != "1",
    reason="Integration tests require NOVA_INTEGRATION=1 and a running MinIO instance",
)


def test_minio_end_to_end_placeholder() -> None:
    """Placeholder: full MinIO end-to-end test (requires NOVA_INTEGRATION=1)."""
    # TODO: instantiate MinioWormAdapter, ObjectCapsuleStore, run put/verify cycle
    pytest.skip("Not yet implemented — requires MinIO testbed")
