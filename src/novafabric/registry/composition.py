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

"""Prompt-composition resolution over the registry (ADR-0115).

Three operations, all local-first and read-only against the registry:

* :func:`validate_composition` — register-time gate (ADR-0115 D5): walk the
  full transitive DAG of a not-yet-registered template; reject cycles,
  over-depth trees, unknown references, and non-text children with named
  errors; return the direct :class:`~novafabric.spec.prompt_asset.CompositionRef`
  block to store on the new version.
* :func:`resolve_composition` — resolve a registered prompt's whole DAG into
  a :class:`~novafabric.spec.prompt_composition.ResolvedCompositionManifest`
  plus the fully assembled template (ADR-0115 D4). Label selectors resolve
  through ADR-0113 at *this* instant; the manifest freezes the outcome.
* :func:`rebuild_from_manifest` — reconstruct the assembled prompt from a
  manifest's pinned versions/hashes only (never re-resolving selectors) and
  verify every content hash plus the final ``assembled_prompt_hash``. Any
  mismatch is a named :class:`CompositionDriftError` — the byte-identical
  replay guarantee.

Resolution recursion is bounded by construction: a cycle check on the DFS
path and the hard :data:`~novafabric.spec.prompt_composition.MAX_COMPOSITION_DEPTH`
bound (ADR-0021 §9 "bounded recursion").
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from novafabric.registry.labels import UnresolvableRefError, resolve_asset_ref
from novafabric.registry.prompts import PromptNotFoundError, get_prompt_version
from novafabric.spec.prompt_asset import CompositionRef, PromptTemplate
from novafabric.spec.prompt_composition import (
    COMPOSITION_REF_RE,
    MAX_COMPOSITION_DEPTH,
    AssembledTemplate,
    ManifestEdge,
    ManifestNode,
    ManifestRoot,
    ResolvedCompositionManifest,
    check_composition_syntax,
    compute_assembled_hash,
    parse_composition_ref,
    template_texts,
)


class CompositionError(Exception):
    """Base class for prompt-composition failures (fail-closed)."""


class CompositionRefError(CompositionError):
    """A composition reference does not resolve to a managed prompt version."""


class CompositionCycleError(CompositionError):
    """The composition graph contains a cycle (incl. self-reference)."""


class CompositionDepthError(CompositionError):
    """The composition tree exceeds the bounded depth (ADR-0115 D2)."""


class CompositionFormError(CompositionError):
    """A referenced child is not a text-form template (v0 splices text only)."""


class CompositionDriftError(CompositionError):
    """Rebuilding from a frozen manifest did not reproduce the pinned bytes."""


# Placeholder parent hash for a template being validated before it has a
# registered content hash of its own (register-time walk).
_UNREGISTERED_ROOT_HASH = "sha256:" + "0" * 64


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cycle_path(stack: tuple[tuple[str, int], ...], key: tuple[str, int]) -> str:
    return " -> ".join(f"{n}@{v}" for n, v in (*stack, key))


class _Walk:
    """One resolution pass: DFS splice with cycle/depth checks + evidence."""

    def __init__(self, db_path: Path | None) -> None:
        self.db_path = db_path
        # (name, version) -> depth at first resolution (DFS order).
        self.node_depths: dict[tuple[str, int], int] = {}
        self.node_hashes: dict[tuple[str, int], str] = {}
        self.edges: list[ManifestEdge] = []
        self._edge_keys: set[tuple[str, str]] = set()
        # (name, version) -> (assembled body, subtree height).
        self._memo: dict[tuple[str, int], tuple[str, int]] = {}

    def resolve_selector(self, name: str, selector: str) -> tuple[int, str, str]:
        """Resolve ``name@selector`` to ``(version, content_hash, kind)``."""
        try:
            resolved = resolve_asset_ref(
                f"prompt:{name}@{selector}", db_path=self.db_path
            )
        except UnresolvableRefError as exc:
            raise CompositionRefError(
                f"composition reference '@prompt:{name}@{selector}' does not "
                f"resolve: {exc}"
            ) from exc
        try:
            version = int(resolved["resolved_version"])
        except (TypeError, ValueError):
            raise CompositionRefError(
                f"composition reference '@prompt:{name}@{selector}' resolved "
                f"to non-managed version {resolved['resolved_version']!r}; "
                "only integer-versioned prompt assets (ADR-0112) compose."
            ) from None
        kind = "version" if resolved["resolved_via"] == "explicit-version" else "label"
        return version, resolved["resolved_content_hash"], kind

    def splice(
        self,
        text: str,
        depth: int,
        stack: tuple[tuple[str, int], ...],
        parent_hash: str,
    ) -> tuple[str, int]:
        """Expand every reference in ``text``; return (spliced, height)."""
        check_composition_syntax(text)
        parts: list[str] = []
        cursor = 0
        height = 0
        for match in COMPOSITION_REF_RE.finditer(text):
            raw_ref, name, selector = match.group(1), match.group(2), match.group(3)
            version, child_hash, kind = self.resolve_selector(name, selector)
            key = (name, version)
            if key in stack:
                raise CompositionCycleError(
                    f"composition cycle detected: {_cycle_path(stack, key)}"
                )
            child_depth = depth + 1
            if child_depth > MAX_COMPOSITION_DEPTH:
                raise CompositionDepthError(
                    f"composition exceeds the bounded depth of "
                    f"{MAX_COMPOSITION_DEPTH}: '{name}@{version}' would "
                    f"resolve at depth {child_depth}"
                )
            edge_key = (parent_hash, raw_ref)
            if edge_key not in self._edge_keys:
                self._edge_keys.add(edge_key)
                self.edges.append(
                    ManifestEdge(
                        parent_hash=parent_hash,
                        ref=raw_ref,
                        selector_kind=kind,  # type: ignore[arg-type]
                        resolved_version=str(version),
                        resolved_hash=child_hash,
                    )
                )
            if key in self._memo:
                body, child_height = self._memo[key]
            else:
                record = get_prompt_version(name, version, db_path=self.db_path)
                child_template = record["template"]
                if not isinstance(child_template, str):
                    raise CompositionFormError(
                        f"referenced prompt '{name}@{version}' is a chat-form "
                        "template; only text-form prompts can be included "
                        "(prompt-composition v0)"
                    )
                body, child_height = self.splice(
                    child_template, child_depth, (*stack, key), child_hash
                )
                self._memo[key] = (body, child_height)
            if child_depth + child_height > MAX_COMPOSITION_DEPTH:
                raise CompositionDepthError(
                    f"composition exceeds the bounded depth of "
                    f"{MAX_COMPOSITION_DEPTH}: including '{name}@{version}' "
                    f"at depth {child_depth} places its deepest child at "
                    f"depth {child_depth + child_height}"
                )
            if key not in self.node_depths:
                self.node_depths[key] = child_depth
                self.node_hashes[key] = child_hash
            height = max(height, child_height + 1)
            parts.append(text[cursor : match.start()])
            parts.append(body)
            cursor = match.end()
        parts.append(text[cursor:])
        return "".join(parts), height

    def assemble(
        self,
        template: PromptTemplate | Any,
        stack: tuple[tuple[str, int], ...],
        root_hash: str,
    ) -> AssembledTemplate:
        """Splice a root template (text or chat form) at depth 0."""
        if isinstance(template, str):
            return self.splice(template, 0, stack, root_hash)[0]
        texts = template_texts(template)
        roles = [m["role"] for m in _plain_messages(template)]
        return [
            {"role": role, "content": self.splice(text, 0, stack, root_hash)[0]}
            for role, text in zip(roles, texts)
        ]


def _plain_messages(template: PromptTemplate | Any) -> list[dict[str, str]]:
    from novafabric.spec.prompt_asset import _template_as_plain

    plain = _template_as_plain(template)
    assert isinstance(plain, list)
    return plain


def validate_composition(
    template: PromptTemplate,
    *,
    db_path: Path | None = None,
) -> list[CompositionRef]:
    """Register-time DAG gate (ADR-0115 D5) for a not-yet-registered template.

    Walks the entire transitive composition (the new version is a virtual
    root at depth 0). Raises a named :class:`CompositionError` on an unknown
    reference, a cycle, an over-depth tree, or a chat-form child — the
    malformed composition never enters the registry. Returns the direct
    ``composition`` block (one entry per distinct declared reference, in
    order of first appearance) to freeze on the new version.
    """
    walk = _Walk(db_path)
    walk.assemble(template, stack=(), root_hash=_UNREGISTERED_ROOT_HASH)
    return [
        CompositionRef(
            ref=edge.ref,
            resolved_version=edge.resolved_version,
            resolved_hash=edge.resolved_hash,
            selector_kind=edge.selector_kind,
        )
        for edge in walk.edges
        if edge.parent_hash == _UNREGISTERED_ROOT_HASH
    ]


def resolve_composition(
    prompt_id: str,
    selector: str | None = None,
    *,
    db_path: Path | None = None,
) -> tuple[dict[str, Any], AssembledTemplate]:
    """Resolve a registered prompt's full composition DAG (ADR-0115 D4).

    ``selector`` is an integer version, a label, or ``None`` for the latest
    version. Returns ``(manifest, assembled)`` where ``manifest`` is the
    capsule-ready ``resolved_composition_manifest`` dict and ``assembled``
    is the fully spliced template. Read-only; label references resolve at
    this instant and the manifest records the outcome.
    """
    walk = _Walk(db_path)
    if selector is None:
        record = get_prompt_version(prompt_id, db_path=db_path)
    elif selector.isdigit():
        record = get_prompt_version(prompt_id, int(selector), db_path=db_path)
    else:
        version, _, _ = walk.resolve_selector(prompt_id, selector)
        record = get_prompt_version(prompt_id, version, db_path=db_path)

    root_key = (record["prompt_id"], record["version"])
    root_hash = record["content_hash"]
    walk.node_depths[root_key] = 0
    walk.node_hashes[root_key] = root_hash

    assembled = walk.assemble(record["template"], stack=(root_key,), root_hash=root_hash)

    included = [
        ManifestNode(
            name=name,
            version=str(version),
            hash=walk.node_hashes[(name, version)],
            depth=depth,
        )
        for (name, version), depth in walk.node_depths.items()
    ]
    manifest = ResolvedCompositionManifest(
        root=ManifestRoot(
            name=record["prompt_id"],
            version=str(record["version"]),
            hash=root_hash,
        ),
        included=included,
        edges=walk.edges,
        assembled_prompt_hash=compute_assembled_hash(assembled),
        max_depth=max(node.depth for node in included),
        resolved_at=_now(),
    )
    return manifest.model_dump(mode="json"), assembled


def rebuild_from_manifest(
    manifest: dict[str, Any],
    *,
    db_path: Path | None = None,
) -> AssembledTemplate:
    """Rebuild the assembled prompt from a frozen manifest's pins only.

    Never re-resolves selectors: every reference site is looked up in the
    manifest's ``edges`` by ``(parent_hash, ref)``, each fetched body is
    verified against its pinned content hash, and the final result is
    verified against ``assembled_prompt_hash``. Any divergence — an edited
    registry, a missing version, a hash mismatch — raises a named
    :class:`CompositionDriftError`. Later label moves have no effect.
    """
    parsed = ResolvedCompositionManifest.model_validate(manifest)
    edge_map = {(edge.parent_hash, edge.ref): edge for edge in parsed.edges}

    def fetch(name: str, version_str: str, pinned_hash: str) -> dict[str, Any]:
        try:
            record = get_prompt_version(name, int(version_str), db_path=db_path)
        except (PromptNotFoundError, ValueError) as exc:
            raise CompositionDriftError(
                f"manifest pins '{name}@{version_str}' but the registry "
                f"cannot supply it: {exc}"
            ) from exc
        if record["content_hash"] != pinned_hash:
            raise CompositionDriftError(
                f"content drift: '{name}@{version_str}' has hash "
                f"{record['content_hash']} but the manifest pinned {pinned_hash}"
            )
        return record

    def build(text: str, parent_hash: str, depth: int) -> str:
        if depth > MAX_COMPOSITION_DEPTH:
            raise CompositionDriftError(
                f"manifest rebuild exceeded the bounded depth of "
                f"{MAX_COMPOSITION_DEPTH}"
            )
        parts: list[str] = []
        cursor = 0
        for match in COMPOSITION_REF_RE.finditer(text):
            raw_ref = match.group(1)
            edge = edge_map.get((parent_hash, raw_ref))
            if edge is None:
                raise CompositionDriftError(
                    f"manifest has no edge for reference {raw_ref!r} under "
                    f"parent {parent_hash}"
                )
            name, _ = parse_composition_ref(raw_ref)
            child = fetch(name, edge.resolved_version, edge.resolved_hash)
            child_template = child["template"]
            if not isinstance(child_template, str):
                raise CompositionDriftError(
                    f"manifest pins '{name}@{edge.resolved_version}' but the "
                    "registry version is not a text-form template"
                )
            parts.append(text[cursor : match.start()])
            parts.append(build(child_template, edge.resolved_hash, depth + 1))
            cursor = match.end()
        parts.append(text[cursor:])
        return "".join(parts)

    root = fetch(parsed.root.name, parsed.root.version, parsed.root.hash)
    template = root["template"]
    assembled: AssembledTemplate
    if isinstance(template, str):
        assembled = build(template, parsed.root.hash, 0)
    else:
        assembled = [
            {
                "role": message["role"],
                "content": build(message["content"], parsed.root.hash, 0),
            }
            for message in _plain_messages(template)
        ]
    rebuilt_hash = compute_assembled_hash(assembled)
    if rebuilt_hash != parsed.assembled_prompt_hash:
        raise CompositionDriftError(
            f"assembled-prompt hash mismatch: rebuilt {rebuilt_hash} but the "
            f"manifest recorded {parsed.assembled_prompt_hash}"
        )
    return assembled
