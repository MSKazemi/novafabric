"""Prompt-composability tests (ADR-0115, prompt-composition v0).

Covers:
- JSON Schema conformance: 12 golden fixtures (2+2 valid / 4+4 invalid)
  against the graduated block + manifest schemas; live manifests validate
- Reference syntax: strict form, malformed/dangling markers fail closed
- Register-time gate (D5): composition block snapshot, unknown ref, cycle,
  depth bound, chat-form child, fail-closed (no row written)
- Resolution (D4): single / nested / pinned / label includes, diamond dedup,
  same child at two versions, duplicate ref sites, chat-form root
- The acceptance gate: byte-identical rebuild from the frozen manifest after
  child edits and label moves; drift (tamper/hash/edge) raises named errors
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from novafabric.registry.composition import (
    CompositionCycleError,
    CompositionDepthError,
    CompositionDriftError,
    CompositionFormError,
    CompositionRefError,
    rebuild_from_manifest,
    resolve_composition,
    validate_composition,
)
from novafabric.registry.labels import set_label
from novafabric.registry.prompts import (
    PromptNotFoundError,
    get_prompt_version,
    register_prompt_version,
)
from novafabric.spec.prompt_composition import (
    MAX_COMPOSITION_DEPTH,
    CompositionSyntaxError,
    check_composition_syntax,
    compute_assembled_hash,
    has_composition_refs,
    parse_composition_ref,
)

FIXTURES = Path(__file__).parent / "fixtures" / "prompt_composition"
SCHEMAS = Path(__file__).parents[1] / "schemas"

BLOCK_FIXTURES = sorted(p.name for p in FIXTURES.glob("composition-*.json"))
MANIFEST_FIXTURES = sorted(p.name for p in FIXTURES.glob("manifest-*.json"))


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def block_validator() -> Draft202012Validator:
    schema = json.loads(
        (SCHEMAS / "prompt-composition-block.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.fixture(scope="module")
def manifest_validator() -> Draft202012Validator:
    schema = json.loads(
        (SCHEMAS / "resolved-composition-manifest.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


# ---------------------------------------------------------------------------
# Schema conformance (golden fixtures, graduated from design/spec/)
# ---------------------------------------------------------------------------


def test_fixture_inventory_complete() -> None:
    assert len(BLOCK_FIXTURES) == 6
    assert len(MANIFEST_FIXTURES) == 6


@pytest.mark.parametrize(
    "name", [n for n in BLOCK_FIXTURES if "-valid" in n]
)
def test_valid_block_fixture_passes(
    block_validator: Draft202012Validator, name: str
) -> None:
    assert list(block_validator.iter_errors(_load(name))) == []


@pytest.mark.parametrize(
    "name", [n for n in BLOCK_FIXTURES if "-invalid" in n]
)
def test_invalid_block_fixture_rejected(
    block_validator: Draft202012Validator, name: str
) -> None:
    assert list(block_validator.iter_errors(_load(name)))


@pytest.mark.parametrize(
    "name", [n for n in MANIFEST_FIXTURES if "-valid" in n]
)
def test_valid_manifest_fixture_passes(
    manifest_validator: Draft202012Validator, name: str
) -> None:
    assert list(manifest_validator.iter_errors(_load(name))) == []


@pytest.mark.parametrize(
    "name", [n for n in MANIFEST_FIXTURES if "-invalid" in n]
)
def test_invalid_manifest_fixture_rejected(
    manifest_validator: Draft202012Validator, name: str
) -> None:
    assert list(manifest_validator.iter_errors(_load(name)))


# ---------------------------------------------------------------------------
# Reference syntax
# ---------------------------------------------------------------------------


def test_has_composition_refs() -> None:
    assert has_composition_refs("a {{@prompt:x@1}} b")
    assert not has_composition_refs("no refs, just {vars}")
    assert has_composition_refs(
        [{"role": "user", "content": "hi {{@prompt:x@production}}"}]
    )


def test_parse_composition_ref() -> None:
    assert parse_composition_ref("@prompt:safety-footer@7") == ("safety-footer", "7")
    assert parse_composition_ref("@prompt:pre@production") == ("pre", "production")
    with pytest.raises(CompositionSyntaxError):
        parse_composition_ref("@prompt:UPPER@1")


@pytest.mark.parametrize(
    "text",
    [
        "{{@prompt:Bad-Name@1}}",  # uppercase slug
        "{{@prompt:x@}}",  # empty selector
        "{{@prompt:x}}",  # no selector
        "{{@prompt:-x@1}}",  # bad slug edge
    ],
)
def test_malformed_reference_fails_closed(text: str) -> None:
    with pytest.raises(CompositionSyntaxError):
        check_composition_syntax(text)


def test_dangling_reference_marker_fails_closed() -> None:
    with pytest.raises(CompositionSyntaxError):
        check_composition_syntax("start {{@prompt:x@1 and no close")


def test_plain_text_passes_syntax_check() -> None:
    check_composition_syntax("no markers here {var}")
    check_composition_syntax("ok {{@prompt:x@1}} ok")


def test_compute_assembled_hash_forms() -> None:
    h1 = compute_assembled_hash("hello")
    assert h1.startswith("sha256:") and len(h1) == 71
    chat = [{"role": "user", "content": "hello"}]
    assert compute_assembled_hash(chat) != h1
    assert compute_assembled_hash(chat) == compute_assembled_hash(list(chat))


# ---------------------------------------------------------------------------
# Register-time gate (D3 + D5)
# ---------------------------------------------------------------------------


def test_register_snapshots_direct_composition_block(tmp_db: Path) -> None:
    register_prompt_version("footer", "Be safe.", db_path=tmp_db)
    register_prompt_version("pre", "Preamble.", db_path=tmp_db)
    set_label("pre", "production", "1", db_path=tmp_db)
    record, created = register_prompt_version(
        "root",
        "{{@prompt:pre@production}} body {{@prompt:footer@1}}",
        db_path=tmp_db,
    )
    assert created is True
    block = record["composition"]
    assert [c["ref"] for c in block] == [
        "@prompt:pre@production",
        "@prompt:footer@1",
    ]
    assert block[0]["selector_kind"] == "label"
    assert block[0]["resolved_version"] == "1"
    assert block[1]["selector_kind"] == "version"
    fetched = get_prompt_version("root", db_path=tmp_db)
    assert fetched["composition"] == block


def test_block_validates_against_graduated_schema(
    tmp_db: Path, block_validator: Draft202012Validator
) -> None:
    register_prompt_version("footer", "Be safe.", db_path=tmp_db)
    record, _ = register_prompt_version(
        "root", "x {{@prompt:footer@1}}", db_path=tmp_db
    )
    assert list(block_validator.iter_errors(record["composition"])) == []


def test_prompt_without_refs_omits_composition_block(tmp_db: Path) -> None:
    record, _ = register_prompt_version("plain", "no refs", db_path=tmp_db)
    assert "composition" not in record
    assert "composition" not in get_prompt_version("plain", db_path=tmp_db)


def test_composition_excluded_from_content_hash(tmp_db: Path) -> None:
    """The canonical body is {template, variables, config} — the block is
    derived metadata, so the hash matches a ref-free computation."""
    from novafabric.spec.prompt_asset import compute_content_hash

    register_prompt_version("footer", "Be safe.", db_path=tmp_db)
    template = "x {{@prompt:footer@1}}"
    record, _ = register_prompt_version("root", template, db_path=tmp_db)
    assert record["content_hash"] == compute_content_hash(template)


def test_register_unknown_ref_fails_closed(tmp_db: Path) -> None:
    with pytest.raises(CompositionRefError):
        register_prompt_version("root", "x {{@prompt:ghost@1}}", db_path=tmp_db)
    with pytest.raises(PromptNotFoundError):
        get_prompt_version("root", db_path=tmp_db)  # no row was written


def test_register_unknown_label_fails_closed(tmp_db: Path) -> None:
    register_prompt_version("pre", "Preamble.", db_path=tmp_db)
    with pytest.raises(CompositionRefError):
        register_prompt_version(
            "root", "x {{@prompt:pre@staging}}", db_path=tmp_db
        )


def test_register_malformed_ref_fails_closed(tmp_db: Path) -> None:
    with pytest.raises(CompositionSyntaxError):
        register_prompt_version("root", "x {{@prompt:pre@1", db_path=tmp_db)


def test_register_chat_child_reference_fails_closed(tmp_db: Path) -> None:
    register_prompt_version(
        "chatty", [{"role": "system", "content": "sys"}], db_path=tmp_db
    )
    with pytest.raises(CompositionFormError):
        register_prompt_version("root", "x {{@prompt:chatty@1}}", db_path=tmp_db)


def test_register_cycle_via_moved_label_fails_closed(tmp_db: Path) -> None:
    # a@1 plain; a@2 refs a@production (-> a@1, acyclic at its register time);
    # move the label onto a@2: now a@2 -> a@2 and any new parent must refuse.
    register_prompt_version("a", "base", db_path=tmp_db)
    set_label("a", "production", "1", db_path=tmp_db)
    register_prompt_version("a", "v2 {{@prompt:a@production}}", db_path=tmp_db)
    set_label("a", "production", "2", db_path=tmp_db)
    with pytest.raises(CompositionCycleError):
        register_prompt_version("b", "x {{@prompt:a@2}}", db_path=tmp_db)
    with pytest.raises(PromptNotFoundError):
        get_prompt_version("b", db_path=tmp_db)


def test_depth_bound_chain_at_limit_ok_and_over_limit_rejected(
    tmp_db: Path,
) -> None:
    # leaf c8, then c7 -> c8, ..., c0 -> c1: deepest node depth == 8 == bound.
    register_prompt_version("c8", "leaf", db_path=tmp_db)
    for i in range(7, -1, -1):
        register_prompt_version(
            f"c{i}", f"lvl{i} {{{{@prompt:c{i + 1}@1}}}}", db_path=tmp_db
        )
    manifest, _ = resolve_composition("c0", db_path=tmp_db)
    assert manifest["max_depth"] == MAX_COMPOSITION_DEPTH == 8
    # one more level pushes the leaf to depth 9 -> rejected at register time
    with pytest.raises(CompositionDepthError):
        register_prompt_version("c-over", "x {{@prompt:c0@1}}", db_path=tmp_db)


def test_validate_composition_direct_api(tmp_db: Path) -> None:
    register_prompt_version("footer", "Be safe.", db_path=tmp_db)
    block = validate_composition("x {{@prompt:footer@1}}", db_path=tmp_db)
    assert len(block) == 1
    assert block[0].ref == "@prompt:footer@1"
    assert block[0].selector_kind == "version"
    assert validate_composition("no refs", db_path=tmp_db) == []


# ---------------------------------------------------------------------------
# Resolution + manifest (D4)
# ---------------------------------------------------------------------------


def test_single_include_resolves_and_splices(tmp_db: Path) -> None:
    register_prompt_version("footer", "Never guess.", db_path=tmp_db)
    register_prompt_version(
        "root", "Do the task.\n{{@prompt:footer@1}}", db_path=tmp_db
    )
    manifest, assembled = resolve_composition("root", db_path=tmp_db)
    assert assembled == "Do the task.\nNever guess."
    assert manifest["root"]["name"] == "root"
    assert manifest["root"]["version"] == "1"
    assert [(n["name"], n["depth"]) for n in manifest["included"]] == [
        ("root", 0),
        ("footer", 1),
    ]
    assert len(manifest["edges"]) == 1
    assert manifest["edges"][0]["parent_hash"] == manifest["root"]["hash"]
    assert manifest["max_depth"] == 1
    assert manifest["assembled_prompt_hash"] == compute_assembled_hash(assembled)


def test_nested_include_depths(tmp_db: Path) -> None:
    register_prompt_version("org", "OrgHeader", db_path=tmp_db)
    register_prompt_version("pre", "P {{@prompt:org@1}}", db_path=tmp_db)
    register_prompt_version("root", "R {{@prompt:pre@1}}", db_path=tmp_db)
    manifest, assembled = resolve_composition("root", db_path=tmp_db)
    assert assembled == "R P OrgHeader"
    depths = {n["name"]: n["depth"] for n in manifest["included"]}
    assert depths == {"root": 0, "pre": 1, "org": 2}
    assert manifest["max_depth"] == 2


def test_pinned_version_include_uses_old_version(tmp_db: Path) -> None:
    register_prompt_version("footer", "old footer", db_path=tmp_db)
    register_prompt_version("footer", "new footer", db_path=tmp_db)
    register_prompt_version("root", "x {{@prompt:footer@1}}", db_path=tmp_db)
    _, assembled = resolve_composition("root", db_path=tmp_db)
    assert assembled == "x old footer"


def test_label_include_resolves_at_this_instant(tmp_db: Path) -> None:
    register_prompt_version("pre", "v1 pre", db_path=tmp_db)
    register_prompt_version("pre", "v2 pre", db_path=tmp_db)
    set_label("pre", "production", "1", db_path=tmp_db)
    register_prompt_version("root", "{{@prompt:pre@production}}!", db_path=tmp_db)
    _, before = resolve_composition("root", db_path=tmp_db)
    assert before == "v1 pre!"
    set_label("pre", "production", "2", db_path=tmp_db)
    manifest, after = resolve_composition("root", db_path=tmp_db)
    assert after == "v2 pre!"  # capture-time resolution follows the label
    edge = manifest["edges"][0]
    assert edge["selector_kind"] == "label"
    assert edge["resolved_version"] == "2"


def test_root_selector_forms(tmp_db: Path) -> None:
    register_prompt_version("solo", "one", db_path=tmp_db)
    register_prompt_version("solo", "two", db_path=tmp_db)
    set_label("solo", "staging", "1", db_path=tmp_db)
    assert resolve_composition("solo", db_path=tmp_db)[1] == "two"  # latest
    assert resolve_composition("solo", "1", db_path=tmp_db)[1] == "one"
    assert resolve_composition("solo", "staging", db_path=tmp_db)[1] == "one"
    assert resolve_composition("solo", "latest", db_path=tmp_db)[1] == "two"
    with pytest.raises(CompositionRefError):
        resolve_composition("solo", "nolabel", db_path=tmp_db)
    with pytest.raises(PromptNotFoundError):
        resolve_composition("ghost", db_path=tmp_db)


def test_uncomposed_prompt_yields_flat_manifest(tmp_db: Path) -> None:
    register_prompt_version("plain", "just text", db_path=tmp_db)
    manifest, assembled = resolve_composition("plain", db_path=tmp_db)
    assert assembled == "just text"
    assert manifest["edges"] == []
    assert len(manifest["included"]) == 1
    assert manifest["max_depth"] == 0
    assert rebuild_from_manifest(manifest, db_path=tmp_db) == "just text"


def test_diamond_dedup_included_once_edges_per_site(tmp_db: Path) -> None:
    register_prompt_version("footer", "F", db_path=tmp_db)
    register_prompt_version("b", "B {{@prompt:footer@1}}", db_path=tmp_db)
    register_prompt_version("c", "C {{@prompt:footer@1}}", db_path=tmp_db)
    register_prompt_version(
        "a", "{{@prompt:b@1}} + {{@prompt:c@1}}", db_path=tmp_db
    )
    manifest, assembled = resolve_composition("a", db_path=tmp_db)
    assert assembled == "B F + C F"
    footer_nodes = [n for n in manifest["included"] if n["name"] == "footer"]
    assert len(footer_nodes) == 1  # dedup by (name, version)
    footer_edges = [
        e for e in manifest["edges"] if e["ref"] == "@prompt:footer@1"
    ]
    assert len(footer_edges) == 2  # one per reference site
    assert len({e["parent_hash"] for e in footer_edges}) == 2


def test_same_child_at_two_versions_both_included(tmp_db: Path) -> None:
    register_prompt_version("footer", "F1", db_path=tmp_db)
    register_prompt_version("footer", "F2", db_path=tmp_db)
    register_prompt_version(
        "root", "{{@prompt:footer@1}}|{{@prompt:footer@2}}", db_path=tmp_db
    )
    manifest, assembled = resolve_composition("root", db_path=tmp_db)
    assert assembled == "F1|F2"
    versions = sorted(
        n["version"] for n in manifest["included"] if n["name"] == "footer"
    )
    assert versions == ["1", "2"]


def test_duplicate_ref_in_one_parent_splices_twice_one_edge(tmp_db: Path) -> None:
    register_prompt_version("footer", "F", db_path=tmp_db)
    register_prompt_version(
        "root", "{{@prompt:footer@1}} and {{@prompt:footer@1}}", db_path=tmp_db
    )
    manifest, assembled = resolve_composition("root", db_path=tmp_db)
    assert assembled == "F and F"
    assert len(manifest["edges"]) == 1  # dedup by (parent, ref)


def test_chat_form_root_splices_message_contents(tmp_db: Path) -> None:
    register_prompt_version("pre", "PRE", db_path=tmp_db)
    register_prompt_version(
        "chat-root",
        [
            {"role": "system", "content": "{{@prompt:pre@1}} sys"},
            {"role": "user", "content": "plain"},
        ],
        db_path=tmp_db,
    )
    manifest, assembled = resolve_composition("chat-root", db_path=tmp_db)
    assert assembled == [
        {"role": "system", "content": "PRE sys"},
        {"role": "user", "content": "plain"},
    ]
    assert manifest["assembled_prompt_hash"] == compute_assembled_hash(assembled)
    assert rebuild_from_manifest(manifest, db_path=tmp_db) == assembled


def test_resolve_cycle_fails_closed(tmp_db: Path) -> None:
    register_prompt_version("a", "base", db_path=tmp_db)
    set_label("a", "production", "1", db_path=tmp_db)
    register_prompt_version("a", "v2 {{@prompt:a@production}}", db_path=tmp_db)
    set_label("a", "production", "2", db_path=tmp_db)
    with pytest.raises(CompositionCycleError):
        resolve_composition("a", "2", db_path=tmp_db)


def test_live_manifest_validates_against_graduated_schema(
    tmp_db: Path, manifest_validator: Draft202012Validator
) -> None:
    register_prompt_version("org", "O", db_path=tmp_db)
    register_prompt_version("pre", "P {{@prompt:org@1}}", db_path=tmp_db)
    set_label("pre", "production", "1", db_path=tmp_db)
    register_prompt_version(
        "root",
        "{{@prompt:pre@production}} body {{@prompt:org@1}}",
        db_path=tmp_db,
    )
    manifest, _ = resolve_composition("root", db_path=tmp_db)
    assert list(manifest_validator.iter_errors(manifest)) == []


# ---------------------------------------------------------------------------
# Byte-identical rebuild (the ADR-0115 acceptance gate) + drift
# ---------------------------------------------------------------------------


def _composed_graph(tmp_db: Path) -> tuple[dict[str, Any], Any]:
    register_prompt_version("org", "OrgHeader v1", db_path=tmp_db)
    register_prompt_version(
        "pre", "Preamble {{@prompt:org@1}}", db_path=tmp_db
    )
    set_label("pre", "production", "1", db_path=tmp_db)
    register_prompt_version("footer", "Footer v1", db_path=tmp_db)
    register_prompt_version(
        "root",
        "{{@prompt:pre@production}}\n\nTask: {t}\n\n{{@prompt:footer@1}}",
        ["t"],
        db_path=tmp_db,
    )
    return resolve_composition("root", db_path=tmp_db)


def test_rebuild_from_snapshot_is_byte_identical(tmp_db: Path) -> None:
    manifest, assembled = _composed_graph(tmp_db)
    assert assembled == "Preamble OrgHeader v1\n\nTask: {t}\n\nFooter v1"
    # Post-capture churn: children edited, label moved, root edited.
    register_prompt_version("pre", "Preamble CHANGED", db_path=tmp_db)
    set_label("pre", "production", "2", db_path=tmp_db)
    register_prompt_version("footer", "Footer CHANGED", db_path=tmp_db)
    rebuilt = rebuild_from_manifest(manifest, db_path=tmp_db)
    assert rebuilt == assembled  # exact bytes
    assert compute_assembled_hash(rebuilt) == manifest["assembled_prompt_hash"]
    # A fresh resolution now legitimately differs (labels moved) —
    # demonstrating exactly what the frozen snapshot protects against.
    _, fresh = resolve_composition("root", db_path=tmp_db)
    assert fresh != assembled


def test_rebuild_detects_registry_tamper(tmp_db: Path) -> None:
    manifest, _ = _composed_graph(tmp_db)
    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "UPDATE assets SET spec_json = replace(spec_json, 'Footer v1', 'EVIL')"
        " WHERE name = 'footer' AND version = '1'"
    )
    conn.commit()
    conn.close()
    with pytest.raises(CompositionDriftError):
        rebuild_from_manifest(manifest, db_path=tmp_db)


def test_rebuild_detects_assembled_hash_mismatch(tmp_db: Path) -> None:
    manifest, _ = _composed_graph(tmp_db)
    manifest["assembled_prompt_hash"] = "sha256:" + "ab" * 32
    with pytest.raises(CompositionDriftError):
        rebuild_from_manifest(manifest, db_path=tmp_db)


def test_rebuild_detects_missing_edge_and_missing_version(tmp_db: Path) -> None:
    manifest, _ = _composed_graph(tmp_db)
    broken = json.loads(json.dumps(manifest))
    broken["edges"] = []
    with pytest.raises(CompositionDriftError):
        rebuild_from_manifest(broken, db_path=tmp_db)
    gone = json.loads(json.dumps(manifest))
    gone["root"]["version"] = "99"
    with pytest.raises(CompositionDriftError):
        rebuild_from_manifest(gone, db_path=tmp_db)
