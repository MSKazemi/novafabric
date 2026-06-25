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
"""Tests for WORM adapter family (base interface + GCS stub)."""

from __future__ import annotations

import pytest

from novafabric.object_capsule_store.worm.base import WormAdapter
from novafabric.object_capsule_store.worm.gcs import GcsWormAdapter


def test_gcs_is_worm_adapter_subclass():
    """GcsWormAdapter is a subclass of WormAdapter (interface compliance)."""
    assert issubclass(GcsWormAdapter, WormAdapter)


# ---------------------------------------------------------------------------
# Abstract WormAdapter cannot be instantiated
# ---------------------------------------------------------------------------

def test_worm_adapter_cannot_instantiate():
    with pytest.raises(TypeError):
        WormAdapter()  # type: ignore[abstract]
