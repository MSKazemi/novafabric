"""File store behavior for saved views (ADR-0130 D2) — fail-closed, non-blocking."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from novafabric.query.errors import QueryParseError
from novafabric.views import (
    SavedView,
    ViewExistsError,
    ViewNotFoundError,
    ViewParseError,
    delete_view,
    list_views,
    load_view,
    save_view,
    slugify_view_name,
    view_hash,
)
from novafabric.views.store import default_views_dir, resolve_view_file


def _view(view_id: str = "avg-cost", name: str | None = None, **overrides: object) -> SavedView:
    base: dict[str, object] = {
        "view_id": view_id,
        "name": name or view_id,
        "query": {"select": ["avg(cost) AS avg_cost", "count()"], "where": ["model = m1"]},
        "created_at": "2026-07-15T10:00:00Z",
    }
    base.update(overrides)
    return SavedView.model_validate(base)


# ── slug derivation ───────────────────────────────────────────────────────────


def test_slugify_collapses_and_trims() -> None:
    assert slugify_view_name("Failed Runs (last 7d)") == "failed-runs-last-7d"
    assert slugify_view_name("avg_cost.by.model") == "avg-cost-by-model"


def test_slugify_unusable_name_refused() -> None:
    with pytest.raises(ViewParseError, match="--view-id"):
        slugify_view_name("***")


# ── save / load round-trip ───────────────────────────────────────────────────


def test_save_yaml_roundtrip_and_lazy_dir(views_dir: Path) -> None:
    assert not views_dir.exists()
    path = save_view(_view(), views_dir)
    assert path == views_dir / "avg-cost.yaml"
    loaded = load_view("avg-cost", views_dir)
    assert loaded == _view()
    assert view_hash(loaded) == view_hash(_view())


def test_save_json_roundtrip(views_dir: Path) -> None:
    path = save_view(_view(), views_dir, as_json=True)
    assert path == views_dir / "avg-cost.json"
    assert json.loads(path.read_text())["view_id"] == "avg-cost"
    assert load_view("avg-cost", views_dir) == _view()


def test_load_by_name_and_by_id(views_dir: Path) -> None:
    save_view(_view("avg-cost", name="Average cost"), views_dir)
    assert load_view("avg-cost", views_dir).name == "Average cost"
    assert load_view("Average cost", views_dir).view_id == "avg-cost"


def test_view_id_inside_file_wins_over_filename(views_dir: Path) -> None:
    save_view(_view("inner-id"), views_dir)
    (views_dir / "inner-id.yaml").rename(views_dir / "other-stem.yaml")
    assert load_view("inner-id", views_dir).view_id == "inner-id"


# ── fail-closed save ─────────────────────────────────────────────────────────


def test_save_invalid_query_refused_nothing_written(views_dir: Path) -> None:
    bad = _view(query={"select": ["median(cost)"]})
    with pytest.raises(QueryParseError, match="median"):
        save_view(bad, views_dir)
    assert not views_dir.exists()


def test_save_unknown_clause_refused(views_dir: Path) -> None:
    bad = _view(query={"from": "capsules", "select": ["count()"]})
    with pytest.raises(QueryParseError, match="unknown query clause"):
        save_view(bad, views_dir)
    assert not views_dir.exists()


# ── overwrite semantics ──────────────────────────────────────────────────────


def test_save_existing_refused_without_force(views_dir: Path) -> None:
    save_view(_view(), views_dir)
    with pytest.raises(ViewExistsError, match="--force"):
        save_view(_view(), views_dir)


def test_force_preserves_created_at_and_sets_updated_at(views_dir: Path) -> None:
    save_view(_view(created_at="2026-01-01T00:00:00Z"), views_dir)
    save_view(
        _view(created_at="2026-07-15T10:00:00Z", description="v2"), views_dir, force=True
    )
    loaded = load_view("avg-cost", views_dir)
    assert loaded.created_at == "2026-01-01T00:00:00Z"
    assert loaded.updated_at is not None
    assert loaded.description == "v2"


def test_force_format_switch_removes_old_file(views_dir: Path) -> None:
    save_view(_view(), views_dir)
    save_view(_view(), views_dir, as_json=True, force=True)
    assert not (views_dir / "avg-cost.yaml").exists()
    assert (views_dir / "avg-cost.json").exists()


def test_force_over_corrupt_file_replaces_it(views_dir: Path) -> None:
    views_dir.mkdir(parents=True)
    (views_dir / "avg-cost.yaml").write_text("{ not: [valid")
    path = save_view(_view(), views_dir, force=True)
    assert load_view("avg-cost", views_dir) == _view()
    assert path.exists()


# ── list / delete / missing ──────────────────────────────────────────────────


def test_list_missing_dir_is_empty_not_error(views_dir: Path) -> None:
    views, warnings = list_views(views_dir)
    assert views == [] and warnings == []


def test_list_sorted_and_corrupt_file_skipped_with_warning(views_dir: Path) -> None:
    save_view(_view("b-view"), views_dir)
    save_view(_view("a-view"), views_dir)
    (views_dir / "broken.yaml").write_text(":::")
    views, warnings = list_views(views_dir)
    assert [v.view_id for v in views] == ["a-view", "b-view"]
    assert len(warnings) == 1 and "broken.yaml" in warnings[0]


def test_load_unknown_view_names_known_views(views_dir: Path) -> None:
    save_view(_view(), views_dir)
    with pytest.raises(ViewNotFoundError, match="avg-cost"):
        load_view("nope", views_dir)


def test_load_corrupt_view_reports_parse_error(views_dir: Path) -> None:
    views_dir.mkdir(parents=True)
    (views_dir / "bad.yaml").write_text("view_id: [")
    with pytest.raises(ViewParseError):
        load_view("bad", views_dir)


def test_load_envelope_invalid_file_reports_parse_error(views_dir: Path) -> None:
    views_dir.mkdir(parents=True)
    (views_dir / "bad.yaml").write_text(yaml.safe_dump({"view_id": "bad"}))
    with pytest.raises(ViewParseError, match="not a valid saved view"):
        load_view("bad", views_dir)


def test_delete_view(views_dir: Path) -> None:
    save_view(_view(), views_dir)
    removed = delete_view("avg-cost", views_dir)
    assert not removed.exists()
    with pytest.raises(ViewNotFoundError):
        resolve_view_file("avg-cost", views_dir)


def test_delete_unknown_raises(views_dir: Path) -> None:
    with pytest.raises(ViewNotFoundError):
        delete_view("nope", views_dir)


# ── default location ─────────────────────────────────────────────────────────


def test_default_views_dir_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NOVAFABRIC_VIEWS_DIR", str(tmp_path / "custom"))
    assert default_views_dir() == tmp_path / "custom"


def test_default_views_dir_is_project_local(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("NOVAFABRIC_VIEWS_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert default_views_dir() == tmp_path / ".novafabric" / "views"
