"""ADR-0235 — widgets and dashboards are portable, versioned, untrusted JSON files.

Three properties carry the design, and each is a way this could quietly go wrong:

* **D6 — unknown fields round-trip.** Without it a mixed-version team destroys
  each other's work through ordinary use: one member opens and re-saves, another
  member's newer settings are gone, and nothing errors anywhere.
* **D7 — a widget is untrusted input.** These files are *designed* to be shared,
  so they will be shared by people who should not be trusted. The threat model is
  "someone pastes a widget from the internet".
* **D3 — `apply` is idempotent**, and never half-applies a directory.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from novafabric.dashboards import (
    DashboardError,
    DashboardStore,
    WidgetValidationError,
    apply_path,
    load_dashboard,
    load_widget,
)


def _widget(**overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "$novafabricWidget": True,
        "version": 1,
        "id": "run-throughput",
        "title": "Runs per day",
        "query": {"select": ["count()"], "group_by": ["status"], "since": "7d"},
        "presentation": {"chart": "bar"},
    }
    doc.update(overrides)
    return doc


def _dashboard(**overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "$novafabricDashboard": True,
        "version": 1,
        "id": "ops",
        "title": "Ops",
        "widgets": [{"widget": "run-throughput", "position": {"x": 0, "y": 0, "w": 6, "h": 4}}],
    }
    doc.update(overrides)
    return doc


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DashboardStore]:
    root = tmp_path / "dashboards"
    root.mkdir()
    yield DashboardStore(root=root)


# ---------------------------------------------------------------------------
# D1 — self-describing and versioned
# ---------------------------------------------------------------------------


def test_a_valid_widget_loads() -> None:
    widget = load_widget(_widget())
    assert widget.id == "run-throughput"
    assert widget.chart == "bar"
    assert widget.version == 1


def test_the_sentinel_is_required() -> None:
    """It is what makes the file identifiable with no accompanying context."""
    doc = _widget()
    del doc["$novafabricWidget"]
    with pytest.raises(WidgetValidationError, match="novafabricWidget"):
        load_widget(doc)


def test_a_wrong_sentinel_value_is_refused() -> None:
    with pytest.raises(WidgetValidationError):
        load_widget(_widget(**{"$novafabricWidget": False}))


def test_a_newer_format_version_is_refused_rather_than_guessed() -> None:
    """D6 promises unknown *fields* survive — not that a format revision is safe.

    A newer version may have redefined something, and a wrong chart is worse
    than no chart.
    """
    with pytest.raises(WidgetValidationError, match="version 99"):
        load_widget(_widget(version=99))


def test_the_error_names_where_the_problem_is() -> None:
    """"Invalid widget" sends the reader back to the whole file."""
    with pytest.raises(WidgetValidationError, match="presentation"):
        load_widget(_widget(presentation={"chart": "sunburst"}))


# ---------------------------------------------------------------------------
# D7 — untrusted input
# ---------------------------------------------------------------------------


def test_a_widget_cannot_express_a_query_the_dsl_forbids() -> None:
    """The embedded query inherits ADR-0129's closed allow-list.

    Structural, not advisory: the widget format has no way to smuggle a
    predicate past the same validator `nova query` uses.
    """
    with pytest.raises(WidgetValidationError, match="DSL rejects"):
        load_widget(_widget(query={"select": ["exfiltrate(secrets)"]}))


def test_an_id_cannot_traverse_the_filesystem() -> None:
    """`id` is the one field that reaches a path, so the schema constrains it."""
    for hostile in ("../../etc/passwd", "/abs", "a/b", "..", "UPPER", ""):
        with pytest.raises(WidgetValidationError):
            load_widget(_widget(id=hostile))


def test_a_non_object_document_is_refused() -> None:
    for junk in ([], "widget", 7, None):
        with pytest.raises(WidgetValidationError):
            load_widget(junk)


def test_the_schema_is_checked_before_the_query() -> None:
    """Order is the whole of D7.

    Validating the query first would mean indexing `document["query"]` on input
    that has not been checked at all — so a document with no `query` key must
    fail as a *schema* error, not a KeyError.
    """
    doc = _widget()
    del doc["query"]
    with pytest.raises(WidgetValidationError, match="invalid at"):
        load_widget(doc)


# ---------------------------------------------------------------------------
# D6 — unknown fields round-trip
# ---------------------------------------------------------------------------


def test_unknown_fields_survive_a_round_trip() -> None:
    """Otherwise a mixed-version team silently destroys each other's work."""
    doc = _widget(**{"x-future": {"palette": "viridis", "nested": [1, 2]}})
    reloaded = json.loads(load_widget(doc).to_json())
    assert reloaded["x-future"] == {"palette": "viridis", "nested": [1, 2]}


def test_a_round_trip_through_the_store_preserves_unknown_fields(
    store: DashboardStore,
) -> None:
    """The property must hold through the *write path*, not just in memory."""
    widget = load_widget(_widget(**{"x-future": {"keep": "me"}}))
    store.save_widget(widget)
    assert store.get_widget("run-throughput").raw["x-future"] == {"keep": "me"}


# ---------------------------------------------------------------------------
# D2 — a dashboard references widgets, never inlines them
# ---------------------------------------------------------------------------


def test_a_dashboard_references_widgets_by_id() -> None:
    assert load_dashboard(_dashboard()).widget_ids == ("run-throughput",)


def test_a_dashboard_cannot_reference_the_same_widget_twice() -> None:
    """Layout position, not repetition, is how a widget appears in two places."""
    doc = _dashboard(widgets=[{"widget": "a"}, {"widget": "a"}])
    with pytest.raises(WidgetValidationError, match="twice"):
        load_dashboard(doc)


def test_missing_widget_references_are_reported_not_skipped(store: DashboardStore) -> None:
    """A dashboard that renders 3 of its 4 panels silently is a partial answer."""
    dashboard = load_dashboard(_dashboard(widgets=[{"widget": "absent-one"}]))
    assert store.unresolved_widget_ids(dashboard) == ("absent-one",)


def test_resolved_references_report_nothing_missing(store: DashboardStore) -> None:
    """Non-vacuity: a checker that always reports a gap proves nothing."""
    store.save_widget(load_widget(_widget()))
    assert store.unresolved_widget_ids(load_dashboard(_dashboard())) == ()


# ---------------------------------------------------------------------------
# D3 — apply is idempotent and never half-applies
# ---------------------------------------------------------------------------


def test_apply_is_idempotent(tmp_path: Path, store: DashboardStore) -> None:
    src = tmp_path / "w.widget.json"
    src.write_text(json.dumps(_widget()), encoding="utf-8")

    first = apply_path(src, store)
    assert [changed for _, changed, _ in first] == [True]
    second = apply_path(src, store)
    assert [changed for _, changed, _ in second] == [False], (
        "re-applying an unchanged file must not churn the mtime — something will "
        "eventually watch these files"
    )


def test_apply_writes_nothing_when_any_document_in_a_directory_is_invalid(
    tmp_path: Path, store: DashboardStore
) -> None:
    """A half-applied directory leaves a state matching neither old nor new.

    ⚠ The names matter. `apply_path` sorts, so the **valid** file must sort
    *first*: with `bad.widget.json` before `good.widget.json` this test passes
    even against a write-as-you-parse implementation, because the failure
    happens before anything valid has been reached. That is exactly what a
    mutation run caught — the first version of this test was green against the
    defect it exists to detect.
    """
    src = tmp_path / "bundle"
    src.mkdir()
    (src / "a-good.widget.json").write_text(json.dumps(_widget()), encoding="utf-8")
    (src / "z-bad.widget.json").write_text(
        json.dumps(_widget(id="bad", presentation={"chart": "nope"})), encoding="utf-8"
    )
    assert sorted(p.name for p in src.iterdir())[0].startswith("a-"), (
        "the valid document must be parsed first, or this test cannot fail"
    )

    with pytest.raises(WidgetValidationError):
        apply_path(src, store)
    assert list(store.root.iterdir()) == [], "a refused apply must write nothing at all"


def test_apply_handles_a_directory_of_widgets_and_dashboards(
    tmp_path: Path, store: DashboardStore
) -> None:
    src = tmp_path / "bundle"
    src.mkdir()
    (src / "w.widget.json").write_text(json.dumps(_widget()), encoding="utf-8")
    (src / "d.dashboard.json").write_text(json.dumps(_dashboard()), encoding="utf-8")

    kinds = sorted(kind for _, _, kind in apply_path(src, store))
    assert kinds == ["dashboard", "widget"]
    assert store.get_dashboard("ops").widget_ids == ("run-throughput",)


def test_apply_refuses_a_missing_path(tmp_path: Path, store: DashboardStore) -> None:
    with pytest.raises(DashboardError, match="does not exist"):
        apply_path(tmp_path / "nope.widget.json", store)


def test_apply_refuses_an_empty_directory(tmp_path: Path, store: DashboardStore) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(DashboardError, match="no \\*"):
        apply_path(empty, store)


def test_malformed_json_is_a_clear_error(tmp_path: Path, store: DashboardStore) -> None:
    src = tmp_path / "broken.widget.json"
    src.write_text("{not json", encoding="utf-8")
    with pytest.raises(WidgetValidationError, match="not valid JSON"):
        apply_path(src, store)


# ---------------------------------------------------------------------------
# D4 — files are the storage, and they stay out of capsule directories
# ---------------------------------------------------------------------------


def test_the_store_lives_outside_any_capsule_directory() -> None:
    """Capsules are signed evidence and stay read-only (ADR-0225 D2's boundary)."""
    from novafabric._paths import dashboards_dir, default_capsule_dir

    assert dashboards_dir() != default_capsule_dir()
    assert default_capsule_dir() not in dashboards_dir().parents


def test_the_schemas_ship_in_the_package() -> None:
    """Three validators once raised FileNotFoundError on every pip install.

    They loaded schemas that were never in the wheel, and a source-tree test run
    could not see it. These two live under `src/novafabric/schemas/`, which
    pyproject's wheel `include` covers.
    """
    from novafabric.dashboards._models import DASHBOARD_SCHEMA_PATH, WIDGET_SCHEMA_PATH

    for path in (WIDGET_SCHEMA_PATH, DASHBOARD_SCHEMA_PATH):
        assert path.is_file(), path
        assert "src/novafabric/schemas" in path.as_posix()
