"""Asset deployment-label tests (ADR-0113, asset-labels v0).

Covers:
- JSON Schema conformance: 7 move + 7 ref golden fixtures (14/14)
- AssetLabelMove / ResolvedAssetRef model validation (closed schemas,
  label slug, hash format, ULID, the tagged-union invariant)
- Registry ops: set/move/get/list/history, append-only invariant (incl.
  SQL trigger enforcement), no-op skip, fail-closed unknown targets,
  reserved auto-maintained 'latest', per-asset label scoping
- Resolution freeze: label + explicit-version refs freeze version +
  content hash; frozen record validates against the graduated schema;
  determinism after a later label move; unresolvable refs fail closed
- Coexistence with ADR-0112 managed prompt rows and the generic registry
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from novafabric.registry.labels import (
    LabelNotFoundError,
    LabelTargetNotFoundError,
    ReservedLabelError,
    UnresolvableRefError,
    get_label,
    label_history,
    list_labels,
    parse_asset_ref,
    resolve_asset_ref,
    set_label,
)
from novafabric.registry.prompts import register_prompt_version
from novafabric.spec.asset_labels import (
    AssetLabelMove,
    ResolvedAssetRef,
    validate_label_name,
)

REPO_ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures" / "asset_labels"

MOVE_SCHEMA = json.loads(
    (REPO_ROOT / "schemas" / "asset-label-move.schema.json").read_text()
)
REF_SCHEMA = json.loads(
    (REPO_ROOT / "schemas" / "resolved-asset-ref.schema.json").read_text()
)


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


def _register(db_path: Path, prompt_id: str = "triage", body: str = "v1 body") -> dict[str, Any]:
    record, _ = register_prompt_version(
        prompt_id=prompt_id, template=body, db_path=db_path
    )
    return record


# ---------------------------------------------------------------------------
# Schema conformance (golden fixtures)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["move-valid.json", "move-valid-first-set.json"]
)
def test_valid_move_fixtures_pass_schema(name: str) -> None:
    Draft202012Validator(MOVE_SCHEMA).validate(_fixture(name))


@pytest.mark.parametrize(
    "name",
    [
        "move-invalid-uppercase-label.json",
        "move-invalid-missing-target.json",
        "move-invalid-unknown-key.json",
        "move-invalid-bad-ulid.json",
        "move-invalid-bad-content-hash.json",
    ],
)
def test_invalid_move_fixtures_rejected(name: str) -> None:
    errors = list(Draft202012Validator(MOVE_SCHEMA).iter_errors(_fixture(name)))
    assert errors, f"{name} unexpectedly passed the move schema"


@pytest.mark.parametrize(
    "name", ["ref-valid.json", "ref-valid-explicit-version.json"]
)
def test_valid_ref_fixtures_pass_schema(name: str) -> None:
    Draft202012Validator(REF_SCHEMA).validate(_fixture(name))


@pytest.mark.parametrize(
    "name",
    [
        "ref-invalid-bad-resolved-via.json",
        "ref-invalid-label-without-name.json",
        "ref-invalid-missing-resolved-version.json",
        "ref-invalid-bad-hash.json",
        "ref-invalid-unknown-key.json",
    ],
)
def test_invalid_ref_fixtures_rejected(name: str) -> None:
    errors = list(Draft202012Validator(REF_SCHEMA).iter_errors(_fixture(name)))
    assert errors, f"{name} unexpectedly passed the ref schema"


def test_valid_fixtures_also_pass_pydantic_models() -> None:
    AssetLabelMove.model_validate(_fixture("move-valid.json"))
    AssetLabelMove.model_validate(_fixture("move-valid-first-set.json"))
    ResolvedAssetRef.model_validate(_fixture("ref-valid.json"))
    ResolvedAssetRef.model_validate(_fixture("ref-valid-explicit-version.json"))


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------


def test_label_name_rules() -> None:
    assert validate_label_name("production") == "production"
    assert validate_label_name("eu-prod.v2_x") == "eu-prod.v2_x"
    for bad in ("Production", "", "-lead", "_x", "spa ce"):
        with pytest.raises(ValueError):
            validate_label_name(bad)


@pytest.mark.parametrize(
    "name",
    [
        "move-invalid-uppercase-label.json",
        "move-invalid-missing-target.json",
        "move-invalid-unknown-key.json",
        "move-invalid-bad-ulid.json",
        "move-invalid-bad-content-hash.json",
    ],
)
def test_move_model_rejects_invalid_fixtures(name: str) -> None:
    with pytest.raises(ValidationError):
        AssetLabelMove.model_validate(_fixture(name))


@pytest.mark.parametrize(
    "name",
    [
        "ref-invalid-bad-resolved-via.json",
        "ref-invalid-label-without-name.json",
        "ref-invalid-missing-resolved-version.json",
        "ref-invalid-bad-hash.json",
        "ref-invalid-unknown-key.json",
    ],
)
def test_ref_model_rejects_invalid_fixtures(name: str) -> None:
    with pytest.raises(ValidationError):
        ResolvedAssetRef.model_validate(_fixture(name))


def test_ref_tagged_union_rejects_label_on_explicit_version() -> None:
    record = _fixture("ref-valid-explicit-version.json")
    record["resolved_label"] = "production"
    with pytest.raises(ValidationError):
        ResolvedAssetRef.model_validate(record)


# ---------------------------------------------------------------------------
# Registry: set / move / get
# ---------------------------------------------------------------------------


def test_first_set_records_null_previous(db_path: Path) -> None:
    _register(db_path)
    record, moved = set_label("triage", "production", "1", db_path=db_path)
    assert moved is True
    assert record["previous_version"] is None
    assert record["target_version"] == "1"
    assert record["asset_type"] == "prompt"
    assert record["content_hash"].startswith("sha256:")
    assert record["move_id"] is not None
    assert record["moved_by"]
    AssetLabelMove.model_validate(
        {k: v for k, v in record.items() if k != "auto"}
    )


def test_move_records_previous_version(db_path: Path) -> None:
    _register(db_path)
    _register(db_path, body="v2 body")
    set_label("triage", "production", "1", db_path=db_path)
    record, moved = set_label("triage", "production", "2", db_path=db_path)
    assert moved is True
    assert record["previous_version"] == "1"
    current = get_label("triage", "production", db_path=db_path)
    assert current["target_version"] == "2"


def test_noop_set_is_skipped(db_path: Path) -> None:
    _register(db_path)
    set_label("triage", "production", "1", db_path=db_path)
    record, moved = set_label("triage", "production", "1", db_path=db_path)
    assert moved is False
    assert record["target_version"] == "1"
    assert len(label_history("triage", "production", db_path=db_path)) == 1


def test_set_unknown_version_fails_closed(db_path: Path) -> None:
    _register(db_path)
    with pytest.raises(LabelTargetNotFoundError):
        set_label("triage", "production", "99", db_path=db_path)
    assert label_history("triage", db_path=db_path) == []


def test_set_on_unknown_asset_fails_closed(db_path: Path) -> None:
    with pytest.raises(LabelTargetNotFoundError):
        set_label("ghost", "production", "1", db_path=db_path)


def test_set_rejects_reserved_latest_and_bad_names(db_path: Path) -> None:
    _register(db_path)
    with pytest.raises(ReservedLabelError):
        set_label("triage", "latest", "1", db_path=db_path)
    with pytest.raises(ValueError):
        set_label("triage", "Production", "1", db_path=db_path)


def test_get_unset_label_raises(db_path: Path) -> None:
    _register(db_path)
    with pytest.raises(LabelNotFoundError):
        get_label("triage", "production", db_path=db_path)


def test_labels_are_scoped_per_asset(db_path: Path) -> None:
    _register(db_path, "triage")
    _register(db_path, "router", body="router body")
    _register(db_path, "router", body="router body v2")
    set_label("triage", "production", "1", db_path=db_path)
    set_label("router", "production", "2", db_path=db_path)
    assert get_label("triage", "production", db_path=db_path)["target_version"] == "1"
    assert get_label("router", "production", db_path=db_path)["target_version"] == "2"


# ---------------------------------------------------------------------------
# Auto-maintained 'latest'
# ---------------------------------------------------------------------------


def test_latest_tracks_highest_version(db_path: Path) -> None:
    _register(db_path)
    assert get_label("triage", "latest", db_path=db_path)["target_version"] == "1"
    _register(db_path, body="v2 body")
    latest = get_label("triage", "latest", db_path=db_path)
    assert latest["target_version"] == "2"
    assert latest["auto"] is True
    assert latest["move_id"] is None


def test_latest_on_unknown_asset_raises(db_path: Path) -> None:
    with pytest.raises(LabelNotFoundError):
        get_label("ghost", "latest", db_path=db_path)


# ---------------------------------------------------------------------------
# History (audit trail) + append-only enforcement
# ---------------------------------------------------------------------------


def test_history_is_complete_and_newest_first(db_path: Path) -> None:
    _register(db_path)
    _register(db_path, body="v2 body")
    set_label("triage", "production", "1", reason="first", db_path=db_path)
    set_label("triage", "production", "2", reason="ship v2", db_path=db_path)
    set_label(
        "triage", "production", "1", reason="rollback", db_path=db_path
    )
    set_label("triage", "staging", "2", db_path=db_path)

    all_moves = label_history("triage", db_path=db_path)
    assert len(all_moves) == 4
    prod = label_history("triage", "production", db_path=db_path)
    assert [(m["previous_version"], m["target_version"]) for m in prod] == [
        ("2", "1"),
        ("1", "2"),
        (None, "1"),
    ]
    assert prod[0]["reason"] == "rollback"
    # Every row is schema-valid audit evidence.
    validator = Draft202012Validator(MOVE_SCHEMA)
    for move in all_moves:
        validator.validate({k: v for k, v in move.items() if k != "auto"})


def test_history_rows_cannot_be_updated_or_deleted(db_path: Path) -> None:
    _register(db_path)
    set_label("triage", "production", "1", db_path=db_path)
    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE asset_label_history SET target_version = '9'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM asset_label_history")
    finally:
        conn.close()


def test_current_pointer_is_projection_of_newest_row(db_path: Path) -> None:
    _register(db_path)
    _register(db_path, body="v2 body")
    set_label("triage", "canary", "1", db_path=db_path)
    set_label("triage", "canary", "2", db_path=db_path)
    history = label_history("triage", "canary", db_path=db_path)
    current = get_label("triage", "canary", db_path=db_path)
    assert current["move_id"] == history[0]["move_id"]
    assert current["target_version"] == history[0]["target_version"]


# ---------------------------------------------------------------------------
# list_labels
# ---------------------------------------------------------------------------


def test_list_labels_includes_auto_latest_and_explicit(db_path: Path) -> None:
    _register(db_path)
    _register(db_path, body="v2 body")
    set_label("triage", "production", "1", db_path=db_path)
    set_label("triage", "staging", "2", db_path=db_path)
    records = list_labels("triage", db_path=db_path)
    by_label = {r["label"]: r for r in records}
    assert set(by_label) == {"latest", "production", "staging"}
    assert by_label["latest"]["auto"] is True
    assert by_label["latest"]["target_version"] == "2"
    assert by_label["production"]["target_version"] == "1"


def test_list_labels_empty_for_unknown_asset(db_path: Path) -> None:
    assert list_labels("ghost", db_path=db_path) == []


# ---------------------------------------------------------------------------
# Resolution freeze (ADR-0113 D3)
# ---------------------------------------------------------------------------


def test_parse_asset_ref() -> None:
    assert parse_asset_ref("prompt:triage@production") == (
        "prompt",
        "triage",
        "production",
    )
    for bad in ("triage@production", "prompt:triage", "prompt:@production", ":x@y", "prompt:triage@"):
        with pytest.raises(UnresolvableRefError):
            parse_asset_ref(bad)


def test_resolve_by_label_freezes_version_and_hash(db_path: Path) -> None:
    record = _register(db_path)
    set_label("triage", "production", "1", db_path=db_path)
    frozen = resolve_asset_ref("prompt:triage@production", db_path=db_path)
    assert frozen["resolved_via"] == "label"
    assert frozen["resolved_label"] == "production"
    assert frozen["resolved_version"] == "1"
    assert frozen["resolved_content_hash"] == record["content_hash"]
    assert frozen["label_move_id"] is not None
    assert frozen["requested_ref"] == "prompt:triage@production"
    Draft202012Validator(REF_SCHEMA).validate(frozen)
    ResolvedAssetRef.model_validate(frozen)


def test_resolve_by_explicit_version(db_path: Path) -> None:
    record = _register(db_path)
    frozen = resolve_asset_ref("prompt:triage@1", db_path=db_path)
    assert frozen["resolved_via"] == "explicit-version"
    assert frozen["resolved_label"] is None
    assert frozen["resolved_version"] == "1"
    assert frozen["resolved_content_hash"] == record["content_hash"]
    Draft202012Validator(REF_SCHEMA).validate(frozen)


def test_resolve_latest_label(db_path: Path) -> None:
    _register(db_path)
    v2 = _register(db_path, body="v2 body")
    frozen = resolve_asset_ref("prompt:triage@latest", db_path=db_path)
    assert frozen["resolved_via"] == "label"
    assert frozen["resolved_version"] == "2"
    assert frozen["resolved_content_hash"] == v2["content_hash"]
    assert frozen["label_move_id"] is None
    Draft202012Validator(REF_SCHEMA).validate(frozen)


def test_frozen_record_survives_later_label_move(db_path: Path) -> None:
    """The load-bearing determinism property: freeze, then move the label."""
    v1 = _register(db_path)
    _register(db_path, body="v2 body")
    set_label("triage", "production", "1", db_path=db_path)
    frozen = resolve_asset_ref("prompt:triage@production", db_path=db_path)

    set_label("triage", "production", "2", db_path=db_path)

    assert frozen["resolved_version"] == "1"
    assert frozen["resolved_content_hash"] == v1["content_hash"]
    # A fresh resolution reflects the move; the frozen record does not.
    refrozen = resolve_asset_ref("prompt:triage@production", db_path=db_path)
    assert refrozen["resolved_version"] == "2"


def test_resolve_unknown_label_or_asset_fails_closed(db_path: Path) -> None:
    _register(db_path)
    with pytest.raises(UnresolvableRefError):
        resolve_asset_ref("prompt:triage@production", db_path=db_path)
    with pytest.raises(UnresolvableRefError):
        resolve_asset_ref("prompt:ghost@production", db_path=db_path)
    with pytest.raises(UnresolvableRefError):
        resolve_asset_ref("prompt:triage@99", db_path=db_path)


# ---------------------------------------------------------------------------
# Coexistence with ADR-0112 prompt rows and the generic registry
# ---------------------------------------------------------------------------


def test_labels_coexist_with_0112_prompt_rows(db_path: Path) -> None:
    """Labelling never mutates the immutable 0112 registry rows."""
    from novafabric.registry.prompts import get_prompt_version, list_prompt_versions

    _register(db_path)
    _register(db_path, body="v2 body")
    before = list_prompt_versions("triage", db_path=db_path)
    set_label("triage", "production", "1", db_path=db_path)
    set_label("triage", "production", "2", db_path=db_path)
    after = list_prompt_versions("triage", db_path=db_path)
    assert before == after
    # And the pinned version fetch still matches the label resolution.
    frozen = resolve_asset_ref("prompt:triage@production", db_path=db_path)
    pinned = get_prompt_version("triage", 2, db_path=db_path)
    assert frozen["resolved_content_hash"] == pinned["content_hash"]


def _insert_generic_asset(
    db_path: Path, name: str, version: str, created_at: str
) -> None:
    """A legacy/generic registry row (semver, no content_hash in spec_json)."""
    from novafabric.registry.store import get_connection, init_schema

    conn = get_connection(db_path)
    init_schema(conn)
    try:
        conn.execute(
            "INSERT INTO assets (id, name, asset_type, version, status, "
            "spec_json, git_commit_sha, created_at, forced_promotion) "
            "VALUES (?, ?, 'dataset', ?, 'development', ?, NULL, ?, 0)",
            (f"{name}-{version}", name, version, json.dumps({"name": name}), created_at),
        )
        conn.commit()
    finally:
        conn.close()


def test_generic_semver_asset_labels_without_content_hash(db_path: Path) -> None:
    """Non-content-addressed assets can be labelled; the freeze fails closed."""
    _insert_generic_asset(db_path, "corpus", "1.0.0", "2026-07-01T00:00:00+00:00")
    _insert_generic_asset(db_path, "corpus", "1.1.0", "2026-07-02T00:00:00+00:00")

    # 'latest' falls back to newest created_at for non-integer versions.
    latest = get_label("corpus", "latest", db_path=db_path)
    assert latest["target_version"] == "1.1.0"
    assert latest["content_hash"] is None

    record, moved = set_label("corpus", "production", "1.0.0", db_path=db_path)
    assert moved is True
    assert record["content_hash"] is None
    assert get_label("corpus", "production", db_path=db_path)["target_version"] == "1.0.0"

    # The capsule freeze requires a content-addressed asset — fail-closed
    # for both the label path and the explicit-version path.
    with pytest.raises(UnresolvableRefError, match="content_hash"):
        resolve_asset_ref("dataset:corpus@production", db_path=db_path)
    with pytest.raises(UnresolvableRefError, match="content_hash"):
        resolve_asset_ref("dataset:corpus@1.0.0", db_path=db_path)


def test_label_table_is_additive_to_registry_schema(db_path: Path) -> None:
    """init_schema-created databases gain the table without any migration."""
    from novafabric.registry.store import get_connection, init_schema

    conn = get_connection(db_path)
    init_schema(conn)
    conn.close()
    _register(db_path)
    set_label("triage", "production", "1", db_path=db_path)
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        conn.close()
    assert "asset_label_history" in tables
    assert "assets" in tables
