"""Prompt-as-versioned-asset tests (ADR-0112, prompt-asset v0).

Covers:
- JSON Schema conformance: 3 valid + 7 invalid golden fixtures (10/10)
- Canonicalization determinism: fixture hashes recompute byte-for-byte;
  variable/config ordering insensitivity; chat-order sensitivity; non-ASCII
- PromptAsset model validation (closed schema, slug, hash format, roles)
- Registry service: monotonic immutable versions, idempotent register,
  get/list/history, legacy-row coexistence, additivity with generic registry
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from novafabric.registry.prompts import (
    PromptNotFoundError,
    frozen_ref,
    get_prompt_version,
    list_prompt_versions,
    list_prompts,
    register_prompt_version,
)
from novafabric.registry.service import get_asset, list_assets
from novafabric.spec.prompt_asset import (
    PromptAsset,
    canonical_body,
    compute_content_hash,
    validate_template,
    variable_mismatch_warnings,
)

FIXTURES = Path(__file__).parent / "fixtures" / "prompt_asset"
SCHEMA_PATH = Path(__file__).parents[1] / "schemas" / "prompt-asset.schema.json"

VALID_FIXTURES = sorted(p.name for p in FIXTURES.glob("valid-*.json"))
INVALID_FIXTURES = sorted(p.name for p in FIXTURES.glob("invalid-*.json"))


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Schema conformance (golden fixtures)
# ---------------------------------------------------------------------------


def test_fixture_inventory_complete() -> None:
    assert len(VALID_FIXTURES) == 3
    assert len(INVALID_FIXTURES) == 7


@pytest.mark.parametrize("name", VALID_FIXTURES)
def test_valid_fixture_passes_schema(validator: Draft202012Validator, name: str) -> None:
    errors = list(validator.iter_errors(_load(name)))
    assert errors == []


@pytest.mark.parametrize("name", INVALID_FIXTURES)
def test_invalid_fixture_rejected_by_schema(
    validator: Draft202012Validator, name: str
) -> None:
    errors = list(validator.iter_errors(_load(name)))
    assert errors, f"{name} unexpectedly passed schema validation"


# ---------------------------------------------------------------------------
# Canonicalization + content hash
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", VALID_FIXTURES)
def test_fixture_content_hash_recomputes(name: str) -> None:
    record = _load(name)
    assert (
        compute_content_hash(
            record["template"], record.get("variables"), record.get("config")
        )
        == record["content_hash"]
    )


def test_canonical_body_sorts_variables_and_config() -> None:
    a = canonical_body("t {x}", ["b", "a"], {"z": 1, "m": {"k2": 2, "k1": 1}})
    b = canonical_body("t {x}", ["a", "b"], {"m": {"k1": 1, "k2": 2}, "z": 1})
    assert a == b
    assert compute_content_hash("t {x}", ["b", "a"]) == compute_content_hash(
        "t {x}", ["a", "b"]
    )


def test_canonical_body_excludes_metadata_keys() -> None:
    body = json.loads(canonical_body("hello", [], {}))
    assert set(body) == {"template", "variables", "config"}


def test_chat_message_order_changes_hash() -> None:
    m1 = [{"role": "system", "content": "a"}, {"role": "user", "content": "b"}]
    m2 = [{"role": "user", "content": "b"}, {"role": "system", "content": "a"}]
    assert compute_content_hash(m1) != compute_content_hash(m2)


def test_non_ascii_preserved_not_escaped() -> None:
    body = canonical_body("Résumé: 日本語 {q}", ["q"], {})
    assert "日本語" in body  # ensure_ascii=False — no \\u escapes
    assert "\\u" not in body


def test_empty_variables_and_config_participate_as_empty() -> None:
    assert compute_content_hash("x") == compute_content_hash("x", [], {})


# ---------------------------------------------------------------------------
# PromptAsset model
# ---------------------------------------------------------------------------


def _valid_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "prompt_id": "triage",
        "version": 1,
        "content_hash": compute_content_hash("hello {q}", ["q"], {}),
        "template": "hello {q}",
        "variables": ["q"],
        "commit_message": "",
        "created_at": "2026-07-15T00:00:00+00:00",
    }
    base.update(overrides)
    return base


def test_model_accepts_valid_record_and_builds_frozen_ref() -> None:
    asset = PromptAsset(**_valid_kwargs())
    assert asset.frozen_ref() == f"prompt:triage@1+{asset.content_hash}"
    assert asset.status.value == "development"


def test_model_rejects_unknown_toplevel_key() -> None:
    with pytest.raises(ValidationError):
        PromptAsset(**_valid_kwargs(surprise="nope"))


@pytest.mark.parametrize("bad_id", ["Triage", "-x", "x-", "a b", ""])
def test_model_rejects_non_slug_prompt_id(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        PromptAsset(**_valid_kwargs(prompt_id=bad_id))


def test_model_rejects_version_zero_and_bad_hash() -> None:
    with pytest.raises(ValidationError):
        PromptAsset(**_valid_kwargs(version=0))
    with pytest.raises(ValidationError):
        PromptAsset(**_valid_kwargs(content_hash="sha256:short"))


def test_model_rejects_bad_chat_role_and_empty_chat() -> None:
    with pytest.raises(ValidationError):
        PromptAsset(
            **_valid_kwargs(template=[{"role": "narrator", "content": "x"}])
        )
    with pytest.raises(ValidationError):
        PromptAsset(**_valid_kwargs(template=[]))


def test_validate_template_normalizes_and_rejects() -> None:
    msgs = validate_template([{"role": "user", "content": "hi"}])
    assert not isinstance(msgs, str)
    assert msgs[0].role == "user"
    with pytest.raises(ValueError):
        validate_template([])
    with pytest.raises(ValidationError):
        validate_template([{"role": "user"}])  # missing content


def test_variable_mismatch_warnings() -> None:
    assert variable_mismatch_warnings("hello {q}", ["q"]) == []
    warnings = variable_mismatch_warnings("hello {q}", ["unused"])
    assert any("unused" in w for w in warnings)
    assert any("{q}" in w for w in warnings)
    chat = [{"role": "user", "content": "hi {name}"}]
    assert variable_mismatch_warnings(validate_template(chat), ["name"]) == []


# ---------------------------------------------------------------------------
# Registry service
# ---------------------------------------------------------------------------


def test_register_first_version_is_one(tmp_db: Path) -> None:
    record, created = register_prompt_version(
        "triage", "v1 body {q}", ["q"], {"temperature": 0.2},
        commit_message="first", db_path=tmp_db,
    )
    assert created is True
    assert record["version"] == 1
    assert record["status"] == "development"
    assert record["content_hash"].startswith("sha256:")
    assert frozen_ref(record) == (
        f"prompt:triage@1+{record['content_hash']}"
    )


def test_register_changed_content_increments_version(tmp_db: Path) -> None:
    register_prompt_version("triage", "v1", db_path=tmp_db)
    record, created = register_prompt_version("triage", "v2", db_path=tmp_db)
    assert created is True
    assert record["version"] == 2


def test_register_identical_content_is_idempotent(tmp_db: Path) -> None:
    first, _ = register_prompt_version(
        "triage", "same body", ["a", "b"], {"k": 1}, db_path=tmp_db
    )
    again, created = register_prompt_version(
        "triage", "same body", ["b", "a"], {"k": 1}, db_path=tmp_db
    )
    assert created is False
    assert again["version"] == first["version"]
    assert len(list_prompt_versions("triage", db_path=tmp_db)) == 1


def test_register_validates_slug_and_template(tmp_db: Path) -> None:
    with pytest.raises(ValidationError):
        register_prompt_version("Not-A-Slug!", "x", db_path=tmp_db)
    with pytest.raises(ValidationError):
        register_prompt_version(
            "ok", [{"role": "narrator", "content": "x"}], db_path=tmp_db
        )


def test_register_chat_form_round_trips(tmp_db: Path) -> None:
    msgs = [
        {"role": "system", "content": "You triage tickets."},
        {"role": "user", "content": "Ticket: {ticket_body}"},
    ]
    record, _ = register_prompt_version(
        "chat-prompt", msgs, ["ticket_body"], db_path=tmp_db
    )
    fetched = get_prompt_version("chat-prompt", db_path=tmp_db)
    assert fetched["template"] == msgs
    assert fetched["content_hash"] == compute_content_hash(msgs, ["ticket_body"])


def test_get_latest_and_specific_version(tmp_db: Path) -> None:
    register_prompt_version("p", "one", db_path=tmp_db)
    register_prompt_version("p", "two", db_path=tmp_db)
    assert get_prompt_version("p", db_path=tmp_db)["template"] == "two"
    assert get_prompt_version("p", 1, db_path=tmp_db)["template"] == "one"


def test_get_missing_prompt_or_version_raises(tmp_db: Path) -> None:
    with pytest.raises(PromptNotFoundError):
        get_prompt_version("ghost", db_path=tmp_db)
    register_prompt_version("p", "one", db_path=tmp_db)
    with pytest.raises(PromptNotFoundError):
        get_prompt_version("p", 99, db_path=tmp_db)
    with pytest.raises(PromptNotFoundError):
        list_prompt_versions("ghost", db_path=tmp_db)


def test_list_prompts_summaries_and_status_filter(tmp_db: Path) -> None:
    register_prompt_version("a-prompt", "one", db_path=tmp_db)
    register_prompt_version("a-prompt", "two", db_path=tmp_db)
    register_prompt_version("b-prompt", "solo", db_path=tmp_db)
    summaries = list_prompts(db_path=tmp_db)
    assert [s["prompt_id"] for s in summaries] == ["a-prompt", "b-prompt"]
    assert summaries[0]["latest_version"] == 2
    assert summaries[0]["versions"] == 2
    assert list_prompts(status="production", db_path=tmp_db) == []


def test_history_is_chronological_with_commit_messages(tmp_db: Path) -> None:
    register_prompt_version("p", "one", commit_message="first", db_path=tmp_db)
    register_prompt_version("p", "two", commit_message="second", db_path=tmp_db)
    history = list_prompt_versions("p", db_path=tmp_db)
    assert [(r["version"], r["commit_message"]) for r in history] == [
        (1, "first"),
        (2, "second"),
    ]


def test_records_validate_against_json_schema(
    tmp_db: Path, validator: Draft202012Validator
) -> None:
    register_prompt_version(
        "p", "body {q}", ["q"], {"temperature": 0.0}, db_path=tmp_db
    )
    record = get_prompt_version("p", db_path=tmp_db)
    assert list(validator.iter_errors(record)) == []


def test_legacy_semver_prompt_rows_are_ignored_not_broken(tmp_db: Path) -> None:
    """A generic `prompt` asset registered via `nova register` (SemVer version)
    must coexist: it is invisible to the managed-version APIs but the managed
    versions still start at 1."""
    import sqlite3
    import uuid

    from novafabric.registry.store import get_connection, init_schema

    conn = get_connection(tmp_db)
    init_schema(conn)
    conn.execute(
        "INSERT INTO assets (id, name, asset_type, version, status, spec_json,"
        " created_at, forced_promotion) VALUES (?, 'legacy', 'prompt', '1.0.0',"
        " 'development', '{}', '2026-01-01T00:00:00+00:00', 0)",
        (str(uuid.uuid4()),),
    )
    conn.commit()
    conn.close()
    assert isinstance(conn, sqlite3.Connection)

    with pytest.raises(PromptNotFoundError):
        get_prompt_version("legacy", db_path=tmp_db)
    assert list_prompts(db_path=tmp_db) == []

    record, created = register_prompt_version("legacy", "managed", db_path=tmp_db)
    assert created is True
    assert record["version"] == 1


def test_additivity_with_generic_registry(tmp_db: Path) -> None:
    """Managed prompt rows are plain assets: visible to nova list / inspect
    paths without any schema change."""
    register_prompt_version("triage", "body", db_path=tmp_db)
    rows = list_assets(asset_type="prompt", status=None, db_path=tmp_db)
    assert len(rows) == 1
    assert rows[0]["name"] == "triage"
    assert rows[0]["version"] == "1"
    generic = get_asset("triage", "1", db_path=tmp_db)
    spec = json.loads(generic["spec_json"])
    assert spec["asset_type"] == "prompt"
    assert spec["content_hash"].startswith("sha256:")
