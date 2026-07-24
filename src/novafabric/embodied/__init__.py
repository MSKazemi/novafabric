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
declared it consumed and which commands it declared it issued. It does not
control a robot, drive, fly, actuate, fuse sensors for control, plan a path,
gate a command, or decide whether an action was safe or in-ODD. It never sits
in a control or actuation hot path, and it stores references, digests and
counts only — never frames, point clouds, video, audio, or control
credentials.
"""

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

__all__ = [
    "FACET_NAME",
    "SCHEMA_VERSION",
    "ActuationRecord",
    "EmbodiedFacet",
    "InvalidReferenceError",
    "MissingIssuerError",
    "Modality",
    "RawPayloadRejectedError",
    "SensorStream",
    "VerifiedBlock",
    "attach_facet",
    "build_actuation",
    "build_facet",
    "digest_stream",
    "is_confirmed",
    "reject_raw_payloads",
    "verify_receipt_binding",
]
