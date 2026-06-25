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
"""NovaFabric compliance evidence module (BQ-005).

Provides:
- tool_permission: ToolPermissionEvent recording and indexing (cap-004)
- pii: PII detection gate, redaction manifests, and subject index (cap-001)
- export: EU AI Act Annex IV and NIS2 incident exports (cap-002, cap-005)

OQ-01 (GDPR Art.17) status: UNRESOLVED.
cap-001 PII redaction operates in LEGAL-HOLD DRAFT MODE only until OQ-01 is resolved.
"""

__all__ = [
    "tool_permission",
    "pii",
    "export",
]
