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
"""Widget and dashboard models, validation, and the D6 round-trip guarantee."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

__all__ = [
    "Dashboard",
    "DashboardError",
    "Widget",
    "WidgetValidationError",
    "load_dashboard",
    "load_widget",
]

_SCHEMA_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "schemas"
WIDGET_SCHEMA_PATH: Final[Path] = _SCHEMA_DIR / "dashboard-widget.schema.json"
DASHBOARD_SCHEMA_PATH: Final[Path] = _SCHEMA_DIR / "dashboard.schema.json"

#: The format version this build writes. Reading a *newer* file is refused
#: rather than attempted: D6 guarantees unknown *fields* round-trip, which is a
#: promise about additive change, not about a format revision that may have
#: redefined something.
WIDGET_VERSION: Final[int] = 1
DASHBOARD_VERSION: Final[int] = 1


class DashboardError(Exception):
    """Base for dashboard/widget failures."""


class WidgetValidationError(DashboardError):
    """A widget or dashboard file was rejected.

    Carries the offending pointer where one is known, because "invalid widget"
    sends the reader back to the whole file.
    """


@lru_cache(maxsize=4)
def _schema(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise DashboardError(
            f"schema missing at {path}. It is listed in pyproject's wheel `include`; "
            "if this fires on an installed build, packaging dropped it."
        )
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _validate_against(document: Any, schema_path: Path, *, what: str) -> None:
    try:
        import jsonschema  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - jsonschema is a core dep
        raise DashboardError("jsonschema is required to validate a widget") from exc

    validator = jsonschema.Draft202012Validator(_schema(schema_path))
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        pointer = "/".join(str(p) for p in first.absolute_path) or "<root>"
        raise WidgetValidationError(f"{what} is invalid at {pointer}: {first.message}")


@dataclass(frozen=True)
class Widget:
    """One chart definition. ``raw`` is authoritative for every write path (D6)."""

    id: str
    title: str
    raw: dict[str, Any] = field(repr=False)

    @property
    def version(self) -> int:
        return int(self.raw["version"])

    @property
    def query(self) -> dict[str, Any]:
        return dict(self.raw["query"])

    @property
    def chart(self) -> str:
        return str(self.raw["presentation"]["chart"])

    def to_json(self) -> str:
        """Serialise from ``raw``, so unknown fields survive (D6).

        Re-rendering from the parsed attributes would silently drop any field a
        newer version added — which is exactly how a mixed-version team destroys
        each other's work through ordinary use.
        """
        return json.dumps(self.raw, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


@dataclass(frozen=True)
class Dashboard:
    """An ordered set of widget *references* plus layout."""

    id: str
    title: str
    raw: dict[str, Any] = field(repr=False)

    @property
    def version(self) -> int:
        return int(self.raw["version"])

    @property
    def builtin(self) -> bool:
        return bool(self.raw.get("builtin", False))

    @property
    def widget_ids(self) -> tuple[str, ...]:
        return tuple(str(entry["widget"]) for entry in self.raw.get("widgets", []))

    def to_json(self) -> str:
        return json.dumps(self.raw, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def load_widget(document: Any) -> Widget:
    """Validate and parse a widget document. Raises on anything unrecognised.

    Order matters and is the whole of D7: **schema first**, so the shape is known
    before anything reads a field; then the ADR-0129 DSL, which owns what a query
    may express. Doing the DSL check first would mean trusting `document["query"]`
    to exist and be a mapping on input that has not been checked at all.
    """
    if not isinstance(document, dict):
        raise WidgetValidationError("a widget must be a JSON object")
    _validate_against(document, WIDGET_SCHEMA_PATH, what="widget")

    version = int(document["version"])
    if version > WIDGET_VERSION:
        raise WidgetValidationError(
            f"widget {document['id']!r} is version {version}; this build reads up to "
            f"{WIDGET_VERSION}. Refusing rather than guessing: unknown *fields* are "
            "preserved (ADR-0235 D6), but a newer format version may have redefined "
            "one, and a wrong chart is worse than no chart."
        )

    # D7: the embedded query inherits the DSL's closed allow-list, so a widget
    # cannot express a query `nova query` would itself refuse.
    from novafabric.query import QueryError, validate_query_object

    try:
        validate_query_object(document["query"])
    except QueryError as exc:
        raise WidgetValidationError(
            f"widget {document['id']!r} carries a query the DSL rejects: {exc}"
        ) from exc

    return Widget(id=str(document["id"]), title=str(document["title"]), raw=document)


def load_dashboard(document: Any) -> Dashboard:
    """Validate and parse a dashboard document."""
    if not isinstance(document, dict):
        raise WidgetValidationError("a dashboard must be a JSON object")
    _validate_against(document, DASHBOARD_SCHEMA_PATH, what="dashboard")

    version = int(document["version"])
    if version > DASHBOARD_VERSION:
        raise WidgetValidationError(
            f"dashboard {document['id']!r} is version {version}; this build reads up "
            f"to {DASHBOARD_VERSION}"
        )

    seen: set[str] = set()
    for entry in document.get("widgets", []):
        widget_id = str(entry["widget"])
        if widget_id in seen:
            raise WidgetValidationError(
                f"dashboard {document['id']!r} references widget {widget_id!r} twice; "
                "layout position, not repetition, is how a widget appears in two places"
            )
        seen.add(widget_id)

    return Dashboard(id=str(document["id"]), title=str(document["title"]), raw=document)
