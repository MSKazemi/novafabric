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

"""Per-artifact content-provenance binding (ADR-0148 D1 / NF-161, NF-162, NF-163).

Binds a C2PA / Content-Credentials manifest to the exact ``content_hash`` of an
ADR-0125 ``MediaPart``, and records — never asserts — a watermark-presence claim.
"""
