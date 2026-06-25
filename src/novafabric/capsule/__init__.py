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

"""NovaFabric capsule — Phase 3 parent/child capsule hierarchy (cap-001 through cap-007)."""

from novafabric.capsule.schema import (
    CapsuleRole,
    ChildCapsule,
    ChildStatus,
    DistributionRole,
    EdgeType,
    FailMode,
    LineageEdgeV2,
    OrphanPlaceholder,
    ParentCapsule,
    ParentStatus,
)

__all__ = [
    "CapsuleRole",
    "ChildCapsule",
    "ChildStatus",
    "DistributionRole",
    "EdgeType",
    "FailMode",
    "LineageEdgeV2",
    "OrphanPlaceholder",
    "ParentCapsule",
    "ParentStatus",
]
