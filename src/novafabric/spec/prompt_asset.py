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

"""Prompt asset model and canonicalization (ADR-0112, prompt-asset v0).

A prompt version is an immutable, content-addressed record layered over the
generic registry ``AssetSpec`` (``asset_type='prompt'``).

Invariants (ADR-0112 D1/D2; spec ``design/spec/prompt-asset-v0.md``):

* Once a ``(prompt_id, version)`` is written, its ``content_hash``,
  ``template``, ``variables``, ``config``, and ``commit_message`` are frozen.
  An edit registers a new version ``max(existing) + 1`` — never a mutation.
* ``content_hash = "sha256:" + sha256(canonical_body)`` where the canonical
  body is exactly ``{"template": ..., "variables": <sorted>, "config": ...}``
  serialized with ``sort_keys=True, ensure_ascii=False,
  separators=(",", ":")`` (spec §Canonicalization, normative).
* ``variables`` are declared for documentation and validation only —
  NovaFabric never renders or interpolates a template.

Schema: ``schemas/prompt-asset.schema.json`` (Draft 2020-12, version 0.1.0).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Final, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from novafabric.spec.models import AssetStatus

PROMPT_ASSET_SCHEMA_VERSION: Final = "0.1.0"

_PROMPT_ID_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_CONTENT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
# Placeholder tokens like {user_query}; informative only — NovaFabric does not
# prescribe a templating syntax and never renders (spec §Template forms).
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class PromptMessage(BaseModel):
    """One ordered chat message in a chat-form template."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: str


# The template body: plain text, or an ordered array of chat messages.
PromptTemplate = Union[str, list[PromptMessage]]

_TEMPLATE_ADAPTER: TypeAdapter[PromptTemplate] = TypeAdapter(PromptTemplate)


def validate_template(template: Any) -> PromptTemplate:
    """Validate raw input (str or list of {role, content}) into a template.

    Raises ``pydantic.ValidationError`` on malformed messages and
    ``ValueError`` on an empty chat form.
    """
    validated = _TEMPLATE_ADAPTER.validate_python(template)
    if isinstance(validated, list) and not validated:
        raise ValueError("chat-form template must contain at least one message")
    return validated


class CompositionRef(BaseModel):
    """One direct composition reference, snapshotted at register time.

    ADR-0115 D3: a lint/convenience surface recording what each declared
    ``{{@prompt:...}}`` reference resolved to when the composed version was
    registered. The authoritative frozen artifact for replay is the capsule
    ``resolved_composition_manifest``, not this block.
    """

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1)
    resolved_version: str = Field(min_length=1)
    resolved_hash: str
    selector_kind: Literal["version", "label"] = "version"

    @field_validator("resolved_hash")
    @classmethod
    def _validate_resolved_hash(cls, v: str) -> str:
        if not _CONTENT_HASH_RE.match(v):
            raise ValueError(f"'{v}' is not a 'sha256:<64 hex>' content hash")
        return v


class PromptAsset(BaseModel):
    """An immutable, content-addressed prompt version record.

    Mirrors ``schemas/prompt-asset.schema.json`` (closed schema: unknown
    top-level keys are rejected; use ``extensions`` for vendor data).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0"] = PROMPT_ASSET_SCHEMA_VERSION
    asset_type: Literal["prompt"] = "prompt"
    prompt_id: str
    version: int = Field(ge=1)
    content_hash: str
    template: PromptTemplate
    commit_message: str
    created_at: str
    status: AssetStatus = AssetStatus.development
    variables: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    composition: list[CompositionRef] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    registry: Literal["local", "server"] = "local"
    extensions: dict[str, Any] = Field(default_factory=dict)

    @field_validator("prompt_id")
    @classmethod
    def _validate_prompt_id(cls, v: str) -> str:
        if not _PROMPT_ID_RE.match(v):
            raise ValueError(
                f"'{v}' is not a valid prompt_id slug "
                "(lowercase letters, digits, inner hyphens)"
            )
        return v

    @field_validator("content_hash")
    @classmethod
    def _validate_content_hash(cls, v: str) -> str:
        if not _CONTENT_HASH_RE.match(v):
            raise ValueError(f"'{v}' is not a 'sha256:<64 hex>' content hash")
        return v

    @field_validator("template")
    @classmethod
    def _validate_template(cls, v: PromptTemplate) -> PromptTemplate:
        if isinstance(v, list) and not v:
            raise ValueError("chat-form template must contain at least one message")
        return v

    def frozen_ref(self) -> str:
        """The canonical, replay-verifiable registry reference.

        ``prompt:<prompt_id>@<version>+sha256:<hex>`` — the exact form a Run
        Capsule records (run-capsule v1 §Asset references).
        """
        return f"prompt:{self.prompt_id}@{self.version}+{self.content_hash}"


def _template_as_plain(template: PromptTemplate) -> str | list[dict[str, str]]:
    """Project a template into plain JSON-serializable data."""
    if isinstance(template, str):
        return template
    return [
        {"role": m.role, "content": m.content}
        if isinstance(m, PromptMessage)
        else {"role": m["role"], "content": m["content"]}
        for m in template
    ]


def canonical_body(
    template: PromptTemplate,
    variables: list[str] | None = None,
    config: dict[str, Any] | None = None,
) -> str:
    """Serialize the canonical version body (spec §Canonicalization, normative).

    Exactly the keys ``template``, ``variables`` (sorted), ``config`` —
    metadata (``version``, ``commit_message``, ``created_at``, ``status``) is
    excluded. Serialization is UTF-8, no insignificant whitespace, keys
    sorted, non-ASCII preserved.
    """
    body = {
        "template": _template_as_plain(template),
        "variables": sorted(variables or []),
        "config": config or {},
    }
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def compute_content_hash(
    template: PromptTemplate,
    variables: list[str] | None = None,
    config: dict[str, Any] | None = None,
) -> str:
    """``sha256:<hex>`` over the canonical body — the capsule-verifiable hash."""
    digest = hashlib.sha256(
        canonical_body(template, variables, config).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


def variable_mismatch_warnings(
    template: PromptTemplate,
    variables: list[str] | None = None,
) -> list[str]:
    """Warn (never block) on declared/used placeholder mismatches.

    A declared variable absent from the template, or a ``{token}`` in the
    template that is not declared, is a validation *warning*, not an error —
    NovaFabric records the mismatch but does not render (spec §Edge cases).
    """
    declared = set(variables or [])
    if isinstance(template, str):
        text = template
    else:
        plain = _template_as_plain(template)
        assert isinstance(plain, list)
        text = "\n".join(m["content"] for m in plain)
    used = set(_PLACEHOLDER_RE.findall(text))
    warnings = [
        f"declared variable '{name}' does not appear in the template"
        for name in sorted(declared - used)
    ]
    warnings.extend(
        f"template placeholder '{{{name}}}' is not a declared variable"
        for name in sorted(used - declared)
    )
    return warnings
