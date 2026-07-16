"""SavedView envelope model + content hash (ADR-0130) — golden fixtures, hash stability.

The 11 golden fixtures under ``design/spec/fixtures/saved-views/`` must behave
as their filename asserts against BOTH the graduated ``/schemas/`` copy (the
graduation changed identity metadata only) and the Pydantic envelope model.
Note the fixtures' ``query`` blocks use the ADR's *illustrative* grammar — the
envelope carries ``query`` opaquely; the real ADR-0129 parse happens at save
time (see ``test_view_store.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from novafabric.views import SavedView, view_hash
from novafabric.views.model import DisplayPrefs, SortKey

_ROOT = Path(__file__).parents[2]
_SCHEMA = _ROOT / "schemas" / "saved-view.schema.json"
_FIXTURES = _ROOT / "design" / "spec" / "fixtures" / "saved-views"
_FIXTURE_FILES = sorted(_FIXTURES.glob("*.json"))


def _validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(_SCHEMA.read_text())
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


def test_fixture_corpus_is_complete() -> None:
    names = [p.name for p in _FIXTURE_FILES]
    assert len(names) == 11
    assert sum(n.startswith("valid-") for n in names) == 4
    assert sum(n.startswith("invalid-") for n in names) == 7


@pytest.mark.parametrize("fixture", _FIXTURE_FILES, ids=lambda p: p.stem)
def test_fixture_against_graduated_schema(fixture: Path) -> None:
    document = json.loads(fixture.read_text())
    errors = list(_validator().iter_errors(document))
    if fixture.name.startswith("valid-"):
        assert errors == [], f"{fixture.name}: {[e.message for e in errors]}"
    else:
        assert errors, f"{fixture.name} unexpectedly passed schema validation"


@pytest.mark.parametrize("fixture", _FIXTURE_FILES, ids=lambda p: p.stem)
def test_fixture_against_pydantic_envelope(fixture: Path) -> None:
    document = json.loads(fixture.read_text())
    if fixture.name.startswith("valid-"):
        view = SavedView.model_validate(document)
        assert view.view_id == document["view_id"]
    else:
        with pytest.raises(ValidationError):
            SavedView.model_validate(document)


def _view(**overrides: object) -> SavedView:
    base: dict[str, object] = {
        "view_id": "avg-cost-by-model",
        "name": "avg-cost-by-model",
        "query": {
            "schema_version": "0.1.0",
            "select": ["avg(cost) AS avg_cost", "count()"],
            "where": ["model = m1"],
            "limit": 100,
        },
        "created_at": "2026-07-15T10:00:00Z",
    }
    base.update(overrides)
    return SavedView.model_validate(base)


def test_schema_version_is_const() -> None:
    with pytest.raises(ValidationError):
        _view(schema_version="0.2.0")


def test_hash_format() -> None:
    digest = view_hash(_view())
    assert digest.startswith("sha256:")
    assert len(digest) == 7 + 64
    assert all(c in "0123456789abcdef" for c in digest[7:])


def test_hash_pinned_golden_value() -> None:
    """Cross-run / cross-machine stability: the definition hash is pinned."""
    assert view_hash(_view()) == (
        "sha256:f8cf745546b6f7cedf33fa013238e9592e008a159d076537731132f853b83193"
    )


def test_hash_ignores_provenance_fields() -> None:
    a = _view(created_at="2026-07-15T10:00:00Z", created_by=None)
    b = _view(created_at="2027-01-01T00:00:00Z", created_by="mohsen", updated_at="2027-01-02T00:00:00Z")
    assert view_hash(a) == view_hash(b)


def test_hash_changes_with_query() -> None:
    a = _view()
    b = _view(query={"schema_version": "0.1.0", "select": ["count()"], "limit": 100})
    assert view_hash(a) != view_hash(b)


def test_hash_changes_with_display_and_description() -> None:
    plain = _view()
    described = _view(description="cost triage")
    displayed = _view(
        display=DisplayPrefs(
            columns=["model"], sort=[SortKey(field="avg_cost", order="desc")], format="table"
        )
    )
    assert len({view_hash(plain), view_hash(described), view_hash(displayed)}) == 3


def test_to_document_omits_unset_optionals() -> None:
    document = _view().to_document()
    assert set(document) == {"schema_version", "view_id", "name", "query", "created_at"}
