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
"""W3C PROV-N text export for NovaFabric Run Capsules (ADR-0176).

PROV-N (https://www.w3.org/TR/prov-n/) is the human-readable notation of the
same W3C PROV data model that :mod:`prov_json` already emits. To guarantee the
two serializations describe the **same** provenance graph, this module builds
the PROV-JSON document first (:func:`export_prov_json`) and renders it to PROV-N
— there is a single source of truth for the graph, and only the surface syntax
differs.

Faithful for entities, activities, and the three relation types NovaFabric emits
(``wasGeneratedBy``, ``used``, ``wasDerivedFrom``). Optional activity start/end
times are carried in PROV-JSON; in PROV-N they are emitted positionally only when
they parse as an xsd:dateTime, otherwise omitted (`-`) so output stays valid.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from novafabric.compliance.export.prov_json import export_prov_json

# xsd:dateTime-ish guard: PROV-N writes dateTime literals bare, so we only emit a
# time positionally when it is unambiguously a dateTime.
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def export_prov_n(capsule_dir: Path) -> str:
    """Generate a W3C PROV-N document (as text) from a capsule's lineage graph.

    Args:
        capsule_dir: Path to the capsule directory containing ``lineage.jsonl``.

    Returns:
        A PROV-N document string (``document`` … ``endDocument``).

    Raises:
        FileNotFoundError: If ``capsule_dir`` does not exist.
    """
    return prov_json_to_prov_n(export_prov_json(capsule_dir))


def _escape_string(value: str) -> str:
    """Escape a PROV-N string literal (backslash and double-quote)."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _render_attrs(attrs: dict[str, Any]) -> str:
    """Render an optional-attribute list ``[k=v, ...]`` or ``""`` if empty.

    ``prov:type`` values are qualified names (e.g. ``nf:Capsule``) → single-quoted
    per PROV-N; positional time keys are skipped (handled by the caller).
    """
    parts: list[str] = []
    for key, val in attrs.items():
        if key in ("prov:startTime", "prov:endTime"):
            continue  # positional on activity(), never an attribute
        if key == "prov:type":
            parts.append(f"{key}='{val}'")
        else:
            parts.append(f'{key}="{_escape_string(str(val))}"')
    return f"[{', '.join(parts)}]" if parts else ""


def _time_or_dash(value: Any) -> str:
    if isinstance(value, str) and _DATETIME_RE.match(value):
        return value
    return "-"


def prov_json_to_prov_n(doc: dict[str, Any]) -> str:
    """Render a PROV-JSON ``dict`` (as produced by :func:`export_prov_json`) to PROV-N."""
    lines: list[str] = ["document"]

    for prefix, uri in doc.get("prefix", {}).items():
        lines.append(f"  prefix {prefix} <{uri}>")
    if doc.get("prefix"):
        lines.append("")

    # Entities
    for eid, attrs in doc.get("entity", {}).items():
        rendered = _render_attrs(attrs or {})
        lines.append(f"  entity({eid}, {rendered})" if rendered else f"  entity({eid})")

    # Activities: activity(id, startTime, endTime, [attrs])
    for aid, attrs in doc.get("activity", {}).items():
        attrs = attrs or {}
        start = _time_or_dash(attrs.get("prov:startTime"))
        end = _time_or_dash(attrs.get("prov:endTime"))
        rendered = _render_attrs(attrs)
        if start == "-" and end == "-" and not rendered:
            lines.append(f"  activity({aid})")
        else:
            tail = f", {rendered}" if rendered else ""
            lines.append(f"  activity({aid}, {start}, {end}{tail})")

    # Relations (identifier before ';' is optional but we keep it for traceability)
    for rid, rel in doc.get("wasGeneratedBy", {}).items():
        lines.append(f"  wasGeneratedBy({rid}; {rel['prov:entity']}, {rel['prov:activity']})")
    for rid, rel in doc.get("used", {}).items():
        lines.append(f"  used({rid}; {rel['prov:activity']}, {rel['prov:entity']})")
    for rid, rel in doc.get("wasDerivedFrom", {}).items():
        lines.append(
            f"  wasDerivedFrom({rid}; {rel['prov:generatedEntity']}, {rel['prov:usedEntity']})"
        )

    lines.append("endDocument")
    return "\n".join(lines) + "\n"
