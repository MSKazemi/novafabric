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
"""File-backed widget/dashboard storage — ADR-0235 D3/D4.

There is no database. ``$NOVAFABRIC_HOME/dashboards`` holds ``*.widget.json``
and ``*.dashboard.json``, and that directory *is* the storage — deleting it
removes every user dashboard with nothing left behind to migrate.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from novafabric._paths import dashboards_dir
from novafabric.dashboards._models import (
    Dashboard,
    DashboardError,
    Widget,
    WidgetValidationError,
    load_dashboard,
    load_widget,
)

__all__ = ["DashboardStore", "apply_path", "default_store"]

WIDGET_SUFFIX = ".widget.json"
DASHBOARD_SUFFIX = ".dashboard.json"


@dataclass(frozen=True)
class DashboardStore:
    """Widgets and dashboards on disk under one root."""

    root: Path

    # -- reads ------------------------------------------------------------

    def widget_path(self, widget_id: str) -> Path:
        return self.root / f"{widget_id}{WIDGET_SUFFIX}"

    def dashboard_path(self, dashboard_id: str) -> Path:
        return self.root / f"{dashboard_id}{DASHBOARD_SUFFIX}"

    def list_widgets(self) -> list[Widget]:
        return [self._read_widget(p) for p in sorted(self.root.glob(f"*{WIDGET_SUFFIX}"))]

    def list_dashboards(self) -> list[Dashboard]:
        return [
            self._read_dashboard(p) for p in sorted(self.root.glob(f"*{DASHBOARD_SUFFIX}"))
        ]

    def get_widget(self, widget_id: str) -> Widget:
        path = self.widget_path(widget_id)
        if not path.is_file():
            raise DashboardError(f"no widget {widget_id!r} in {self.root}")
        return self._read_widget(path)

    def get_dashboard(self, dashboard_id: str) -> Dashboard:
        path = self.dashboard_path(dashboard_id)
        if not path.is_file():
            raise DashboardError(f"no dashboard {dashboard_id!r} in {self.root}")
        return self._read_dashboard(path)

    def _read_widget(self, path: Path) -> Widget:
        return load_widget(_read_json(path))

    def _read_dashboard(self, path: Path) -> Dashboard:
        return load_dashboard(_read_json(path))

    # -- writes -----------------------------------------------------------

    def save_widget(self, widget: Widget) -> tuple[Path, bool]:
        """Write *widget*. Returns ``(path, changed)``.

        ``changed`` is False when the bytes on disk already match, which is what
        makes ``nova dashboard apply`` idempotent (D3) — applying the same file
        twice must not churn mtimes, since something will eventually watch them.
        """
        return _write_if_changed(self.widget_path(widget.id), widget.to_json())

    def save_dashboard(self, dashboard: Dashboard) -> tuple[Path, bool]:
        return _write_if_changed(self.dashboard_path(dashboard.id), dashboard.to_json())

    def unresolved_widget_ids(self, dashboard: Dashboard) -> tuple[str, ...]:
        """Referenced widget ids with no file. Reported, never silently skipped.

        A dashboard that renders three of its four panels and says nothing is
        the same class of defect ADR-0234 exists to prevent: a partial answer
        presented as a whole one.
        """
        return tuple(
            widget_id
            for widget_id in dashboard.widget_ids
            if not self.widget_path(widget_id).is_file()
        )


def default_store() -> DashboardStore:
    """The store under ``$NOVAFABRIC_HOME/dashboards``, created on demand."""
    root = dashboards_dir()
    root.mkdir(parents=True, exist_ok=True)
    return DashboardStore(root=root)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WidgetValidationError(f"{path} is not valid JSON: {exc}") from exc


def _write_if_changed(path: Path, content: str) -> tuple[Path, bool]:
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return path, True


def apply_path(source: Path, store: DashboardStore) -> list[tuple[Path, bool, str]]:
    """Apply one file or a directory of them. Returns ``(path, changed, kind)`` rows.

    **Validate everything before writing anything.** A directory that is half
    applied because the fifth file was malformed leaves the operator with a
    state that matches neither the old nor the new definition, and no obvious
    way back — so parsing is done up front and writes only start once every
    document has been accepted.
    """
    if source.is_dir():
        files = sorted(
            p
            for p in source.iterdir()
            if p.name.endswith((WIDGET_SUFFIX, DASHBOARD_SUFFIX))
        )
        if not files:
            raise DashboardError(
                f"no *{WIDGET_SUFFIX} or *{DASHBOARD_SUFFIX} files in {source}"
            )
    elif source.is_file():
        files = [source]
    else:
        raise DashboardError(f"{source} does not exist")

    parsed: list[tuple[str, Widget | Dashboard]] = []
    for path in files:
        document = _read_json(path)
        if path.name.endswith(DASHBOARD_SUFFIX) or (
            isinstance(document, dict) and document.get("$novafabricDashboard")
        ):
            parsed.append(("dashboard", load_dashboard(document)))
        else:
            parsed.append(("widget", load_widget(document)))

    results: list[tuple[Path, bool, str]] = []
    for kind, item in parsed:
        if isinstance(item, Widget):
            written, changed = store.save_widget(item)
        else:
            written, changed = store.save_dashboard(item)
        results.append((written, changed, kind))
    return results
