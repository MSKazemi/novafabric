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

"""Tool-schema change classification (ADR-0148 D2 / NF-164).

Answers *"what kind of change is this?"* between two versions of a tool's JSON Schema, as the
counterpart to NF-165 (:mod:`novafabric.supplychain.toolschema.impact`), which answers *"which
past runs would it break?"*. Classify first, then measure the blast radius.

The **additive-safe rule** (ADR-0148 D2) decides the class: adding an *optional* property is
``additive``; removing one, changing its type, tightening optional→required, or adding a **new
required** property is ``breaking``. That last case is the one most easily got wrong — it is an
*addition*, and it breaks every payload ever recorded.

Three properties this has to get right, each of which fails quietly:

- **``unknown`` never defaults to safe.** A schema built from ``allOf``/``anyOf``/``oneOf``/
  ``not``/``$ref`` is not traversed here, and is reported as ``unknown`` with a reason. Calling
  it ``additive`` would read as "safe to ship" about a schema nobody analysed.
- **Truncating the diff must not change the class.** The diff is bounded, but classification runs
  over the *whole* comparison — a breaking change past the cap still classifies as breaking, and
  ``diff_truncated`` says the list is partial so nobody reads it as complete.
- **Digests are over canonical content, not file bytes.** NF-165 hashes the raw file, which is
  right for *"which file did I check?"*. Here it would make a whitespace-only reformat produce
  two different digests with an **empty diff** — a recorded change containing no changes. The two
  constructions are deliberately different because they answer different questions.

It **classifies; it does not gate**: there is no ``safe``/``allowed``/verdict field, and a
``breaking`` class is a fact about two schemas, not a decision about shipping one.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "0.1.0"
FACET_NAME = "tool_schema_change"

#: The closed change vocabulary (ADR-0148 D2 / spec §3.8).
ChangeClass = Literal["additive", "breaking", "deprecation", "unknown"]

#: Diff operations. ``deprecated`` is what makes the ``deprecation`` class derivable from the
#: evidence rather than merely asserted.
DiffOp = Literal["added", "removed", "tightened", "deprecated"]

#: The diff is bounded so one pathological schema pair cannot produce an unbounded record.
MAX_DIFF_ENTRIES = 100

#: Keywords whose semantics this does not attempt to diff. Encountering one anywhere it walks
#: makes the result ``unknown`` — see the module docstring.
UNTRAVERSED_KEYWORDS: tuple[str, ...] = ("allOf", "anyOf", "oneOf", "not", "$ref")


class ToolSchemaChangeError(ValueError):
    """Raised when two schemas cannot be compared at all."""


class SchemaDiffEntry(BaseModel):
    """One difference between the two schemas."""

    model_config = ConfigDict(frozen=True)

    op: DiffOp
    path: str
    was: str | None = None
    now: str | None = None


class ToolSchemaChange(BaseModel):
    """A classified change between two versions of one tool's schema.

    Intentionally carries no ``safe``/``allowed``/``verdict`` field: ``breaking`` is a fact about
    two schemas, not a decision about whether to ship one.
    """

    model_config = ConfigDict(frozen=True)

    tool_id: str
    from_schema_digest: str
    to_schema_digest: str
    change_class: ChangeClass
    diff: list[SchemaDiffEntry] = Field(default_factory=list)
    #: True when the bounded diff omits entries. The *class* is still computed over all of them.
    diff_truncated: bool = False
    #: How many differences were found in total, including any the bounded diff omits.
    diff_total: int = 0
    schema_version: str = SCHEMA_VERSION
    #: Present only for ``unknown``, saying what could not be analysed.
    reason: str | None = None


def schema_digest(schema: Any) -> str:
    """Digest a schema by canonical content, so formatting is not a change.

    Deliberately **not** :func:`novafabric.supplychain.toolschema.impact._schema_digest`, which
    hashes the file's raw bytes. That identifies a *file*; this identifies a *schema*, and a
    reformatted file is the same schema.
    """
    encoded = json.dumps(schema, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _type_of(node: Any) -> str | None:
    if isinstance(node, dict):
        declared = node.get("type")
        if isinstance(declared, str):
            return declared
        if isinstance(declared, list):
            return "|".join(sorted(str(t) for t in declared))
    return None


def _untraversed_in(node: Any) -> str | None:
    if isinstance(node, dict):
        for keyword in UNTRAVERSED_KEYWORDS:
            if keyword in node:
                return keyword
    return None


def _walk(
    before: Any,
    after: Any,
    path: str,
    diff: list[SchemaDiffEntry],
    unknown: list[str],
) -> None:
    """Collect differences between two schema nodes into *diff*, depth-first.

    Records into *unknown* the path of any node using a keyword this does not traverse, so the
    caller can refuse to classify rather than guess.
    """
    for node, side in ((before, "from"), (after, "to")):
        keyword = _untraversed_in(node)
        if keyword is not None:
            unknown.append(f"{path} uses {keyword!r} in the {side} schema")

    before_props = before.get("properties", {}) if isinstance(before, dict) else {}
    after_props = after.get("properties", {}) if isinstance(after, dict) else {}
    if not isinstance(before_props, dict) or not isinstance(after_props, dict):
        return
    before_required = set(before.get("required", []) or []) if isinstance(before, dict) else set()
    after_required = set(after.get("required", []) or []) if isinstance(after, dict) else set()

    for name in sorted(set(before_props) | set(after_props)):
        child = f"{path}.properties.{name}"
        in_before, in_after = name in before_props, name in after_props

        if in_after and not in_before:
            # An addition is only safe while it is optional: a new *required* property breaks
            # every payload ever recorded, which is why this is not simply "added = additive".
            required_now = name in after_required
            diff.append(
                SchemaDiffEntry(
                    op="tightened" if required_now else "added",
                    path=child,
                    was="absent",
                    now="required" if required_now else "optional",
                )
            )
            continue
        if in_before and not in_after:
            diff.append(SchemaDiffEntry(op="removed", path=child, was="present", now="absent"))
            continue

        before_child, after_child = before_props[name], after_props[name]
        before_type, after_type = _type_of(before_child), _type_of(after_child)
        if before_type != after_type:
            diff.append(
                SchemaDiffEntry(op="tightened", path=child, was=before_type, now=after_type)
            )
        if name not in before_required and name in after_required:
            diff.append(
                SchemaDiffEntry(op="tightened", path=child, was="optional", now="required")
            )
        if _deprecated(after_child) and not _deprecated(before_child):
            diff.append(
                SchemaDiffEntry(op="deprecated", path=child, was="active", now="deprecated")
            )

        _walk(before_child, after_child, child, diff, unknown)


def _deprecated(node: Any) -> bool:
    return isinstance(node, dict) and node.get("deprecated") is True


def _class_for(diff: list[SchemaDiffEntry]) -> ChangeClass:
    """Precedence: breaking beats deprecation beats additive.

    A release that both deprecates a field and removes another is breaking — the deprecation is
    true but it is not the fact a caller needs to act on.
    """
    ops = {entry.op for entry in diff}
    if ops & {"removed", "tightened"}:
        return "breaking"
    if "deprecated" in ops:
        return "deprecation"
    return "additive"


def classify_schema_change(
    *,
    tool_id: str,
    from_schema: Any,
    to_schema: Any,
    max_diff_entries: int = MAX_DIFF_ENTRIES,
) -> ToolSchemaChange:
    """Classify the change from *from_schema* to *to_schema* for one tool.

    Args:
        tool_id: the stable tool identity the change is recorded against.
        from_schema: the previous JSON Schema, as a parsed object.
        to_schema: the new JSON Schema, as a parsed object.
        max_diff_entries: bound on the recorded diff. The **class is computed over the full
            comparison** regardless — truncating the evidence must never soften the verdict.

    Raises:
        ToolSchemaChangeError: if either schema is not a JSON object.
    """
    for schema, label in ((from_schema, "from_schema"), (to_schema, "to_schema")):
        if not isinstance(schema, dict):
            raise ToolSchemaChangeError(
                f"{label} is {type(schema).__name__}, not a JSON Schema object; there is nothing "
                "to compare, and reporting that as 'additive' would read as safe"
            )
    if max_diff_entries < 1:
        raise ToolSchemaChangeError("max_diff_entries must be at least 1")

    diff: list[SchemaDiffEntry] = []
    unknown: list[str] = []
    _walk(from_schema, to_schema, "$", diff, unknown)

    from_digest = schema_digest(from_schema)
    to_digest = schema_digest(to_schema)

    if unknown:
        return ToolSchemaChange(
            tool_id=tool_id,
            from_schema_digest=from_digest,
            to_schema_digest=to_digest,
            change_class="unknown",
            diff=diff[:max_diff_entries],
            diff_truncated=len(diff) > max_diff_entries,
            diff_total=len(diff),
            reason=(
                "cannot classify: " + "; ".join(sorted(set(unknown))[:5]) + ". Composition "
                "keywords are not traversed here, and calling an unanalysed schema 'additive' "
                "would read as safe to ship"
            ),
        )

    # Computed over `diff` in full, before the bound is applied.
    change_class = _class_for(diff)
    return ToolSchemaChange(
        tool_id=tool_id,
        from_schema_digest=from_digest,
        to_schema_digest=to_digest,
        change_class=change_class,
        diff=diff[:max_diff_entries],
        diff_truncated=len(diff) > max_diff_entries,
        diff_total=len(diff),
    )


# ── Capsule facet ─────────────────────────────────────────────────────────


def attach_facet(
    capsule: dict[str, Any], changes: list[ToolSchemaChange] | None
) -> dict[str, Any]:
    """Attach *changes* to a capsule dict additively; returns a new dict.

    The facet is a **list** (spec §4.3): one capsule can record a change for several tools.
    Writes nothing when *changes* is None or empty, so a run with no schema change stays
    byte-identical to one captured before this feature existed.
    """
    if not changes:
        return capsule
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    facets[FACET_NAME] = [c.model_dump(exclude_none=True) for c in changes]
    out["facets"] = facets
    return out


def facet_from_capsule(capsule: dict[str, Any]) -> list[ToolSchemaChange] | None:
    """Read the tool-schema changes back out of a capsule dict, or None if absent."""
    facets = capsule.get("facets")
    if not isinstance(facets, dict):
        return None
    block = facets.get(FACET_NAME)
    if not isinstance(block, list):
        return None
    try:
        return [ToolSchemaChange.model_validate(entry) for entry in block]
    except ValueError as exc:
        raise ToolSchemaChangeError(
            f"capsule holds an invalid {FACET_NAME} facet: {exc}"
        ) from exc


__all__ = [
    "FACET_NAME",
    "MAX_DIFF_ENTRIES",
    "SCHEMA_VERSION",
    "UNTRAVERSED_KEYWORDS",
    "ChangeClass",
    "DiffOp",
    "SchemaDiffEntry",
    "ToolSchemaChange",
    "ToolSchemaChangeError",
    "attach_facet",
    "classify_schema_change",
    "facet_from_capsule",
    "schema_digest",
]
