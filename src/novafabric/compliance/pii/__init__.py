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
"""PII detection gate and redaction manifest (cap-001).

OQ-01 (GDPR Art.17) is UNRESOLVED.
This module operates in LEGAL-HOLD DRAFT MODE until OQ-01 is resolved.
Redacted capsules are written to legal_hold/ staging, NOT sealed.
"""

from .detector import (
    CustomDetector,
    PIIDetector,
    PIISpan,
    PresidioDetector,
    RegexDetector,
    make_detector,
)
from .gate import PIIDetectionGate, PIIScanError, RedactedPayload
from .index import RedactionSubjectIndex
from .manifest import RedactionManifest, RedactionManifestEntry

__all__ = [
    "CustomDetector",
    "PIIDetectionGate",
    "PIIDetector",
    "PIIScanError",
    "PIISpan",
    "PresidioDetector",
    "RedactedPayload",
    "RedactionManifest",
    "RedactionManifestEntry",
    "RedactionSubjectIndex",
    "RegexDetector",
    "make_detector",
]
