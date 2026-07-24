"""OpenInference → OTel GenAI attribute mapping (ADR-0098, experimental).

Why this exists
---------------
``genai_ingest`` classifies spans purely on ``gen_ai.*`` attributes, so traces
emitted by the OpenInference-instrumented ecosystem — LangChain, LlamaIndex,
CrewAI, DSPy, and Arize Phoenix — arrived as ``unclassified`` and were
dropped. Those libraries carry the same facts under a different vocabulary
(``llm.model_name`` rather than ``gen_ai.request.model``, and so on).

Design
------
This module **translates, it does not classify**. It rewrites a span's
attribute dict into the ``gen_ai.*`` vocabulary and hands it back; every
downstream step in ``genai_ingest`` — classification, passthrough
allow-lists, event construction, unmapped-key accounting — then works
unchanged. One vocabulary, one code path, so the two ingest routes cannot
drift in how they classify or what they preserve.

Honesty rules (ADR-0098, ADR-0021 §4)
-------------------------------------
- **Nothing is fabricated.** A ``gen_ai.*`` key is written only when the
  corresponding OpenInference attribute is actually present.
- **Existing ``gen_ai.*`` wins.** A span carrying both vocabularies (some
  instrumentations dual-emit) keeps its native values; the translation never
  overwrites them.
- **Content stays content.** ``input.value``/``output.value`` and the
  ``llm.*_messages`` families map onto the ``gen_ai`` *content* keys, so they
  follow the same ADR-0021 policy path as natively-emitted content rather
  than sneaking in through a second, unpoliced route.
- **Nothing is silently discarded.** Untranslated ``openinference.*``/``llm.*``
  keys keep their original names and are reported by the existing
  unmapped/dropped-key accounting.

OpenInference is an Apache-2.0 specification (Tier A per ADR-0024); this is a
hand-written mapping table, so no dependency is added.
"""

from __future__ import annotations

import json
from typing import Any, Final

#: Marker attribute every OpenInference instrumentation sets. Its presence is
#: what tells us a span speaks this vocabulary at all.
SPAN_KIND_KEY: Final[str] = "openinference.span.kind"

#: OpenInference span kinds → the ``gen_ai.operation.name`` that
#: ``genai_ingest._classify`` already understands.
#:
#: RETRIEVER/EMBEDDING/RERANKER deliberately map to ``chat``-adjacent model
#: operations rather than being invented as new event kinds: the capsule
#: schema has no retrieval primitive today, and inventing one here would put
#: a schema decision in a translation table. They surface as model calls with
#: their original attributes preserved, which is honest and lossless.
SPAN_KIND_TO_OPERATION: Final[dict[str, str]] = {
    "LLM": "chat",
    "CHAIN": "invoke_agent",
    "AGENT": "invoke_agent",
    "TOOL": "execute_tool",
    "RETRIEVER": "embeddings",
    "EMBEDDING": "embeddings",
    "RERANKER": "embeddings",
    "GUARDRAIL": "chat",
    "EVALUATOR": "chat",
}

#: Direct 1:1 attribute renames. Structural and metric facts only — content
#: is handled separately below so it stays on the ADR-0021 path.
ATTRIBUTE_MAP: Final[dict[str, str]] = {
    "llm.model_name": "gen_ai.request.model",
    "llm.provider": "gen_ai.system",
    "llm.system": "gen_ai.system",
    "llm.token_count.prompt": "gen_ai.usage.input_tokens",
    "llm.token_count.completion": "gen_ai.usage.output_tokens",
    "tool.name": "gen_ai.tool.name",
    "tool.description": "gen_ai.tool.description",
    "tool_call.id": "gen_ai.tool.call.id",
    "graph.node.id": "gen_ai.agent.id",
    "graph.node.name": "gen_ai.agent.name",
}

#: Content attributes → ``gen_ai`` content keys (``_CONTENT_KEYS`` in
#: ``genai_ingest``). Kept separate from :data:`ATTRIBUTE_MAP` so it is
#: obvious at a glance which half of this table carries model text.
CONTENT_MAP: Final[dict[str, str]] = {
    "input.value": "gen_ai.input.messages",
    "output.value": "gen_ai.output.messages",
    "llm.input_messages": "gen_ai.input.messages",
    "llm.output_messages": "gen_ai.output.messages",
    "llm.prompts": "gen_ai.request.messages",
}

#: ``llm.invocation_parameters`` is a JSON *string* holding the sampling
#: parameters that GenAI models as first-class attributes. Only these keys are
#: lifted out; anything else in the blob stays untouched in the original
#: attribute so nothing is lost.
_INVOCATION_PARAM_MAP: Final[dict[str, str]] = {
    "temperature": "gen_ai.request.temperature",
    "top_p": "gen_ai.request.top_p",
    "top_k": "gen_ai.request.top_k",
    "max_tokens": "gen_ai.request.max_tokens",
    "frequency_penalty": "gen_ai.request.frequency_penalty",
    "presence_penalty": "gen_ai.request.presence_penalty",
    "seed": "gen_ai.request.seed",
    "stop": "gen_ai.request.stop_sequences",
}

_INVOCATION_PARAMS_KEY: Final[str] = "llm.invocation_parameters"


def is_openinference_span(attrs: dict[str, Any]) -> bool:
    """True if *attrs* looks like an OpenInference span.

    Requires the explicit span-kind marker, or an ``llm.``/``tool.`` attribute
    we know how to translate. Deliberately strict: a span carrying some
    unrelated ``llm.``-prefixed vendor attribute should not be dragged through
    a translation that would not produce anything meaningful.
    """
    if SPAN_KIND_KEY in attrs:
        return True
    return any(key in attrs for key in ATTRIBUTE_MAP) or any(
        key in attrs for key in CONTENT_MAP
    )


def _lift_invocation_parameters(
    attrs: dict[str, Any], out: dict[str, Any]
) -> None:
    """Lift sampling parameters out of the JSON-string blob, if parseable."""
    raw = attrs.get(_INVOCATION_PARAMS_KEY)
    if not isinstance(raw, str) or not raw.strip():
        return
    try:
        params = json.loads(raw)
    except (ValueError, TypeError):
        return  # malformed → leave the original attribute alone, lose nothing
    if not isinstance(params, dict):
        return
    for source, target in _INVOCATION_PARAM_MAP.items():
        if source in params and target not in out:
            out[target] = params[source]


def translate_attributes(attrs: dict[str, Any]) -> dict[str, Any]:
    """Return *attrs* with OpenInference keys expressed as ``gen_ai.*``.

    The original keys are **kept**, so nothing is lost and the existing
    unmapped/dropped accounting in ``genai_ingest`` still sees them. A
    ``gen_ai.*`` key already present on the span is never overwritten — a
    dual-emitting instrumentation's native value is authoritative.
    """
    if not is_openinference_span(attrs):
        return attrs

    out = dict(attrs)

    kind = attrs.get(SPAN_KIND_KEY)
    if isinstance(kind, str):
        operation = SPAN_KIND_TO_OPERATION.get(kind.strip().upper())
        if operation and "gen_ai.operation.name" not in out:
            out["gen_ai.operation.name"] = operation

    for source, target in ATTRIBUTE_MAP.items():
        if source in attrs and target not in out:
            out[target] = attrs[source]

    for source, target in CONTENT_MAP.items():
        if source in attrs and target not in out:
            out[target] = attrs[source]

    _lift_invocation_parameters(attrs, out)

    # A TOOL span whose name only lives in the span name still needs a
    # gen_ai.tool.name for _classify to route it — but we do not invent one
    # here; genai_ingest already falls back to the span name for that.
    return out
