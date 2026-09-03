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

"""The content-provenance honesty line (ADR-0148 I-4).

Defined once because every ``nova provenance`` output must print the *same* words.
ADR-0148 I-4 makes it a MUST for a behavioural reason, not a legal one: this surface
renders verdict-shaped output — ``hard_binding_ok`` in green, a watermark ``present:
true`` — and a recorded claim that looks like a finding will be read as one. This line
is what separates "the manifest's claimed hash matches the bytes we hold" from
"NovaFabric certifies this content is authentic and lawfully marked".
"""

from __future__ import annotations

HONESTY_LINE = (
    "NovaFabric verifies manifests and records claims. It does not assert that content "
    "is authentic, un-manipulated, watermarked, or Art. 50-compliant."
)

__all__ = ["HONESTY_LINE"]
