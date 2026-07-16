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

"""OTel GenAI canonical-span emission and ingest (NF-032/033/034, ADR-0098).

`genai_emitter.emit_spans` maps an already-captured capsule outward to OTel GenAI
`gen_ai.*` spans; `content_bridge` is the opt-in, ADR-0009-redacted message bridge;
`genai_ingest` is the inbound half — OTLP/HTTP **JSON** traces carrying GenAI spans
become run capsules (`capture_level: ingested-otlp`). OTLP/**protobuf** ingest is
also supported (ADR-0177) and reuses the JSON path after decoding, so both wire
encodings converge on identical events; protobuf decoding needs the `otlp` extra
(`pip install 'novafabric[otlp]'`, opentelemetry-proto, Apache-2.0).
"""

from novafabric.otel.genai_emitter import MAPPING_VERSION, emit_spans
from novafabric.otel.genai_ingest import (
    OTLPIngestError,
    ingest_otlp_json,
    ingest_otlp_protobuf,
    parse_otlp_json,
    parse_otlp_protobuf,
    write_ingest_capsule,
)

__all__ = [
    "MAPPING_VERSION",
    "OTLPIngestError",
    "emit_spans",
    "ingest_otlp_json",
    "ingest_otlp_protobuf",
    "parse_otlp_json",
    "parse_otlp_protobuf",
    "write_ingest_capsule",
]
