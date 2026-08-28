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

"""Prompt-composition models (ADR-0115, prompt-composition v0).

A prompt asset MAY reference other prompt assets inline:

    {{@prompt:<asset-name>@<selector>}}

where ``<selector>`` is an explicit integer version (``@3``) or a deployment
label (``@production``, ADR-0113; the reserved ``latest`` counts as a label).
A reference is a placeholder for the referenced asset's *resolved body*,
spliced in at the reference site — textual inclusion is the only mechanism.

Invariants (ADR-0115 D1–D5; spec ``the private design/spec/prompt-composition-v0.md``):

* The composition graph is a **bounded acyclic DAG**: cycles are rejected,
  and no node may sit deeper than :data:`MAX_COMPOSITION_DEPTH` (= 8, root
  at depth 0; the manifest schema's normative bound).
* At resolution time the whole transitive DAG is frozen into a
  :class:`ResolvedCompositionManifest` — every included prompt's version +
  content hash, the resolved DAG edges, and the hash of the final assembled
  prompt. Rebuilding from the manifest's pins reproduces the exact bytes.
* ``included[].depth`` is the depth at which a node was first resolved
  (DFS order); a shared child is deduplicated by ``(name, version)`` but
  keeps one edge per distinct ``(parent, reference)`` site.

Schemas: ``schemas/prompt-composition-block.schema.json`` and
``schemas/resolved-composition-manifest.schema.json`` (Draft 2020-12, 0.1.0).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Final, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from novafabric.spec.prompt_asset import PromptTemplate, _template_as_plain

PROMPT_COMPOSITION_SCHEMA_VERSION: Final = "0.1.0"

#: Deepest allowed node depth (root = 0). A node that would resolve at depth
#: 9 or beyond is rejected — the project's "bounded recursion" anti-pattern
#: (ADR-0021 §9) applied to composition, and the manifest schema's bound.
MAX_COMPOSITION_DEPTH: Final = 8

_CONTENT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: Strict reference form: ``{{@prompt:<slug>@<selector>}}``.
#: group(1) = the raw ref (braces stripped), group(2) = name, group(3) = selector.
COMPOSITION_REF_RE: Final = re.compile(
    r"\{\{(@prompt:([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)@([a-z0-9][a-z0-9._-]*))\}\}"
)

_REF_MARKER: Final = "{{@prompt:"
_LOOSE_REF_RE: Final = re.compile(r"\{\{@prompt:[^{}]*\}\}")

#: What a rebuilt/assembled template looks like (plain data, not models).
AssembledTemplate = Union[str, list[dict[str, str]]]


class CompositionSyntaxError(ValueError):
    """A ``{{@prompt:...}}`` marker is present but malformed (fail-closed)."""


def has_composition_refs(template: PromptTemplate) -> bool:
    """True when any template text carries a ``{{@prompt:`` marker."""
    return any(_REF_MARKER in text for text in template_texts(template))


def template_texts(template: PromptTemplate) -> list[str]:
    """The template's text bodies: the string itself, or each chat content."""
    plain = _template_as_plain(template)
    if isinstance(plain, str):
        return [plain]
    return [message["content"] for message in plain]


def check_composition_syntax(text: str) -> None:
    """Reject malformed composition references (named error, fail-closed).

    Every ``{{@prompt:...}}`` occurrence MUST fully match the strict
    reference form; a dangling ``{{@prompt:`` without a closing ``}}`` is
    also rejected. Text with no marker passes untouched.
    """
    loose = list(_LOOSE_REF_RE.finditer(text))
    if text.count(_REF_MARKER) != len(loose):
        raise CompositionSyntaxError(
            "unterminated composition reference: found '{{@prompt:' without "
            "a matching '}}'"
        )
    for match in loose:
        if not COMPOSITION_REF_RE.fullmatch(match.group(0)):
            raise CompositionSyntaxError(
                f"malformed composition reference {match.group(0)!r}; "
                "expected {{@prompt:<asset-name>@<selector>}} "
                "(lowercase slug name; integer version or label selector)"
            )


def parse_composition_ref(raw_ref: str) -> tuple[str, str]:
    """Split a raw ``@prompt:<name>@<selector>`` ref into its two parts."""
    match = COMPOSITION_REF_RE.fullmatch("{{" + raw_ref + "}}")
    if match is None:
        raise CompositionSyntaxError(
            f"malformed composition reference {raw_ref!r}; "
            "expected @prompt:<asset-name>@<selector>"
        )
    return match.group(2), match.group(3)


def compute_assembled_hash(assembled: AssembledTemplate) -> str:
    """``sha256:<hex>`` over the assembled prompt — the manifest's final hash.

    Text form hashes the exact spliced string (UTF-8). Chat form hashes the
    canonical JSON of the spliced messages (sorted keys, no insignificant
    whitespace, non-ASCII preserved — the ADR-0112 canonicalization style).
    """
    if isinstance(assembled, str):
        payload = assembled.encode("utf-8")
    else:
        payload = json.dumps(
            assembled, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class ManifestNode(BaseModel):
    """One transitively-included prompt version (dedup by name+version)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    hash: str
    depth: int = Field(ge=0, le=MAX_COMPOSITION_DEPTH)

    @field_validator("hash")
    @classmethod
    def _validate_hash(cls, v: str) -> str:
        if not _CONTENT_HASH_RE.match(v):
            raise ValueError(f"'{v}' is not a 'sha256:<64 hex>' content hash")
        return v


class ManifestEdge(BaseModel):
    """One resolved reference site: what a parent's ref resolved to."""

    model_config = ConfigDict(extra="forbid")

    parent_hash: str
    ref: str
    selector_kind: Literal["version", "label"] = "version"
    resolved_version: str
    resolved_hash: str

    @field_validator("parent_hash", "resolved_hash")
    @classmethod
    def _validate_hash(cls, v: str) -> str:
        if not _CONTENT_HASH_RE.match(v):
            raise ValueError(f"'{v}' is not a 'sha256:<64 hex>' content hash")
        return v


class ManifestRoot(BaseModel):
    """The top-level composed prompt the manifest describes."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    hash: str

    @field_validator("hash")
    @classmethod
    def _validate_hash(cls, v: str) -> str:
        if not _CONTENT_HASH_RE.match(v):
            raise ValueError(f"'{v}' is not a 'sha256:<64 hex>' content hash")
        return v


class ResolvedCompositionManifest(BaseModel):
    """Flattened, content-addressed evidence of a whole composition tree.

    Frozen once written (ADR-0115 D4). Replay rebuilds the assembled prompt
    from these pins — never by re-fetching selectors — and verifies the
    result against ``assembled_prompt_hash``.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0"] = PROMPT_COMPOSITION_SCHEMA_VERSION
    root: ManifestRoot
    included: list[ManifestNode]
    edges: list[ManifestEdge]
    assembled_prompt_hash: str
    max_depth: int = Field(ge=0, le=MAX_COMPOSITION_DEPTH)
    resolved_at: str

    @field_validator("assembled_prompt_hash")
    @classmethod
    def _validate_hash(cls, v: str) -> str:
        if not _CONTENT_HASH_RE.match(v):
            raise ValueError(f"'{v}' is not a 'sha256:<64 hex>' content hash")
        return v
