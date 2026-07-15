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

"""Standard *outer* envelopes (NF-029/030/031/035, ADR-0096).

Additive emitters that wrap NovaFabric's inner artifacts (Run Capsule, Evidence Bundle,
Event Envelope v1) in industry-standard envelopes — DSSE, in-toto, SLSA v1,
CloudEvents — so stock tooling (``cosign``, in-toto/SLSA verifiers, CloudEvents brokers)
can verify and route them with no NovaFabric dependency. Posture: **wrap, don't replace**
— the inner bytes are the envelope payload and are never rewritten.
"""
