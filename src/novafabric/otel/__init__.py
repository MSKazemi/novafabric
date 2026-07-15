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

"""OTel GenAI canonical-span emission (NF-032/033, ADR-0098).

`genai_emitter.emit_spans` maps an already-captured capsule outward to OTel GenAI
`gen_ai.*` spans; `content_bridge` is the opt-in, ADR-0009-redacted message bridge.
The OTLP/OpenInference *ingestion* endpoint (NF-034) is intentionally not implemented
here — it requires a protobuf decode dependency and is tracked separately.
"""

from novafabric.otel.genai_emitter import MAPPING_VERSION, emit_spans

__all__ = ["MAPPING_VERSION", "emit_spans"]
