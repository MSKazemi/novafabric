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

"""Embodied & cyber-physical agent evidence (ADR-0162, experimental).

Record-only: NovaFabric records which sensor streams an embodied agent
declared it consumed and which commands it declared it issued (P1), the ODD
it declared and the excursions observed outside it with a verdict that is
always null, and the perception→actuation chain of artifact digests it
declared (P2). It does not
control a robot, drive, fly, actuate, fuse sensors for control, plan a path,
gate a command, or decide whether an action was safe or in-ODD. It never sits
in a control or actuation hot path, and it stores references, digests and
counts only — never frames, point clouds, video, audio, or control
credentials.
"""

from novafabric.embodied._boundary import IN_MISSION_BOUNDARY
from novafabric.embodied._timestamps import InvalidTimestampError
from novafabric.embodied.facet import (
    FACET_NAME,
    SCHEMA_VERSION,
    ActuationRecord,
    EmbodiedFacet,
    InvalidReferenceError,
    MissingIssuerError,
    Modality,
    RawPayloadRejectedError,
    SensorStream,
    VerifiedBlock,
    attach_facet,
    build_actuation,
    build_facet,
    digest_stream,
    is_confirmed,
    reject_raw_payloads,
    verify_receipt_binding,
)
from novafabric.embodied.odd import (
    AdjudicationRefusedError,
    ExcursionOrderError,
    OddConformance,
    OddExcursion,
    build_odd,
)
from novafabric.embodied.trajectory import (
    FindingCode,
    TrajectoryFinding,
    TrajectoryHop,
    TrajectoryReport,
    TrajectoryStage,
    walk_trajectory,
)

__all__ = [
    "FACET_NAME",
    "IN_MISSION_BOUNDARY",
    "SCHEMA_VERSION",
    "ActuationRecord",
    "AdjudicationRefusedError",
    "EmbodiedFacet",
    "ExcursionOrderError",
    "FindingCode",
    "InvalidReferenceError",
    "InvalidTimestampError",
    "MissingIssuerError",
    "Modality",
    "OddConformance",
    "OddExcursion",
    "RawPayloadRejectedError",
    "SensorStream",
    "TrajectoryFinding",
    "TrajectoryHop",
    "TrajectoryReport",
    "TrajectoryStage",
    "VerifiedBlock",
    "attach_facet",
    "build_actuation",
    "build_facet",
    "build_odd",
    "digest_stream",
    "is_confirmed",
    "reject_raw_payloads",
    "verify_receipt_binding",
    "walk_trajectory",
]
