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

"""Tests for the ADR-0121 append-only ``Comment`` record + ``comments.jsonl`` IO."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from novafabric.capsule.comments import (
    COMMENTS_FILENAME,
    Comment,
    CommentSecretError,
    CommentThreadCycleError,
    SubjectKind,
    append_comment,
    apply_tombstones,
    capsule_subject_digest,
    gate_comment_body,
    iter_comments,
    read_comments,
    resolve_thread,
    scan_comment_body,
)
from novafabric.capture._ulid import new_ulid
from novafabric.evidence.merkle import capsule_merkle_root

_REPO = Path(__file__).resolve().parents[2]
_DIGEST = "sha256:" + "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
_SCHEMA = json.loads((_REPO / "schemas" / "comment.schema.json").read_text())
_FIXTURES = _REPO / "tests" / "fixtures" / "capsule-comments"
_VALIDATOR = jsonschema.Draft202012Validator(
    _SCHEMA, format_checker=jsonschema.FormatChecker()
)


def _comment(**over: object) -> Comment:
    base: dict[str, object] = dict(
        subject=_DIGEST,
        subject_kind=SubjectKind.CAPSULE,
        author="m.ardebili",
        body="output looks suspect — flagging for review",
    )
    base.update(over)
    return Comment(**base)  # type: ignore[arg-type]


# ── Record model validation ────────────────────────────────────────────────────


def test_defaults_are_generated() -> None:
    c = _comment()
    assert len(c.comment_id) == 26
    assert c.schema_version == "0.1.0"
    assert c.created_at.endswith("+00:00") or c.created_at.endswith("Z")
    assert c.in_reply_to is None
    assert c.tombstone is False
    assert c.redaction_applied is False


def test_roundtrips_json() -> None:
    c = _comment(tags=["review", "blocked-promotion"])
    again = Comment.model_validate_json(c.model_dump_json())
    assert again == c


def test_bad_comment_id() -> None:
    with pytest.raises(ValueError, match="comment_id"):
        _comment(comment_id="not-a-ulid")


def test_bad_in_reply_to() -> None:
    with pytest.raises(ValueError, match="in_reply_to"):
        _comment(in_reply_to="nope")


def test_bad_schema_version() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        _comment(schema_version="9.9.9")


def test_digest_kind_rejects_asset_subject() -> None:
    with pytest.raises(ValueError, match="sha256"):
        _comment(subject="asset://prompt/triage@1.0.0")


def test_asset_kind_rejects_sha_subject() -> None:
    with pytest.raises(ValueError, match="asset://"):
        _comment(subject_kind=SubjectKind.ASSET)


def test_asset_kind_accepts_asset_ref() -> None:
    c = _comment(subject="asset://prompt/triage-router@3.1.0", subject_kind=SubjectKind.ASSET)
    assert c.subject_kind is SubjectKind.ASSET


def test_empty_author_and_body_rejected() -> None:
    with pytest.raises(ValueError):
        _comment(author="")
    with pytest.raises(ValueError):
        _comment(body="")


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValueError):
        _comment(surprise="boo")


def test_span_run_score_kinds_accept_digest() -> None:
    for kind in (SubjectKind.SPAN, SubjectKind.RUN, SubjectKind.SCORE):
        assert _comment(subject_kind=kind).subject == _DIGEST


# ── JSONL IO + append-only invariant ──────────────────────────────────────────


def test_append_and_read_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / COMMENTS_FILENAME
    first = _comment()
    second = _comment(body="second note")
    append_comment(path, first)
    append_comment(path, second)
    assert read_comments(path) == [first, second]


def test_append_never_rewrites_prior_bytes(tmp_path: Path) -> None:
    # Append-only invariant (D3): appending leaves existing bytes untouched.
    path = tmp_path / COMMENTS_FILENAME
    append_comment(path, _comment())
    before = path.read_bytes()
    append_comment(path, _comment(body="later"))
    assert path.read_bytes()[: len(before)] == before


def test_no_overwrite_api_exists() -> None:
    # The module deliberately exposes no write/overwrite/delete function.
    import novafabric.capsule.comments as mod

    assert not any(name.startswith(("write_", "delete_", "update_")) for name in dir(mod))


def test_read_missing_returns_empty(tmp_path: Path) -> None:
    # Additive-first: a capsule with no comments.jsonl is valid.
    assert read_comments(tmp_path / "absent.jsonl") == []
    assert list(iter_comments(tmp_path / "absent.jsonl")) == []


def test_read_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / COMMENTS_FILENAME
    path.write_text(_comment().model_dump_json() + "\n\n   \n" + _comment().model_dump_json() + "\n")
    assert len(read_comments(path)) == 2


def test_read_invalid_line_raises_with_context(tmp_path: Path) -> None:
    path = tmp_path / COMMENTS_FILENAME
    path.write_text('{"not": "a comment"}\n')
    with pytest.raises(ValueError, match=r"comments\.jsonl:1: invalid Comment"):
        read_comments(path)


# ── Thread resolution (bounded, cycle-reporting) ───────────────────────────────


def test_resolve_thread_root_first() -> None:
    root = _comment()
    reply = _comment(body="confirmed", in_reply_to=root.comment_id)
    edit = _comment(body="correction", in_reply_to=reply.comment_id)
    chain = resolve_thread([root, reply, edit], edit.comment_id)
    assert [c.comment_id for c in chain] == [root.comment_id, reply.comment_id, edit.comment_id]


def test_resolve_thread_unknown_id() -> None:
    with pytest.raises(KeyError):
        resolve_thread([_comment()], new_ulid())


def test_resolve_thread_orphan_parent_is_root() -> None:
    orphan = _comment(in_reply_to=new_ulid())  # parent not present locally
    chain = resolve_thread([orphan], orphan.comment_id)
    assert chain == [orphan]


def test_resolve_thread_reports_cycle() -> None:
    a_id, b_id = new_ulid(), new_ulid()
    a = _comment(comment_id=a_id, in_reply_to=b_id)
    b = _comment(comment_id=b_id, in_reply_to=a_id)
    with pytest.raises(CommentThreadCycleError, match="cycle"):
        resolve_thread([a, b], a_id)


# ── Tombstone (append-only delete) view ───────────────────────────────────────


def test_apply_tombstones_hides_retracted_and_marker() -> None:
    root = _comment()
    keep = _comment(body="unrelated")
    stone = _comment(body="retracting", in_reply_to=root.comment_id, tombstone=True)
    assert apply_tombstones([root, keep, stone]) == [keep]


def test_tombstone_of_unknown_id_is_tolerated() -> None:
    keep = _comment()
    stone = _comment(body="retracting", in_reply_to=new_ulid(), tombstone=True)
    assert apply_tombstones([keep, stone]) == [keep]


# ── Secret-scan gate (D4, ADR-0009) ───────────────────────────────────────────

_SECRET = "sk-ant-" + "a" * 40


def test_scan_flags_secret_body() -> None:
    assert "anthropic-api-key" in scan_comment_body(f"reran with key {_SECRET} — see logs")


def test_clean_body_passes_unchanged() -> None:
    assert gate_comment_body("plain human note") == ("plain human note", False)


def test_secret_body_refused_by_default() -> None:
    with pytest.raises(CommentSecretError, match="anthropic-api-key"):
        gate_comment_body(f"key is {_SECRET}")


def test_refusal_never_echoes_the_secret() -> None:
    with pytest.raises(CommentSecretError) as err:
        gate_comment_body(f"key is {_SECRET}")
    assert _SECRET not in str(err.value)


def test_redact_masks_and_flags() -> None:
    body, redacted = gate_comment_body(f"reran with {_SECRET} ok", redact=True)
    assert redacted is True
    assert _SECRET not in body
    assert "[REDACTED:anthropic-api-key]" in body


def test_body_emptied_by_redaction_is_refused() -> None:
    # An all-secret comment carries no evidence value. Masking never yields an
    # empty string, so force the drop-style empty replacement to hit the guard.
    from unittest.mock import patch

    with patch("novafabric.capture.secrets._replacement", return_value=""):
        with pytest.raises(CommentSecretError, match="empty"):
            gate_comment_body(_SECRET, redact=True)


# ── Subject digest stability ──────────────────────────────────────────────────


def _capsule(tmp_path: Path) -> Path:
    cap = tmp_path / "capsule"
    cap.mkdir()
    (cap / "capsule.yaml").write_text("run_id: 01HXAY7M5JZ8R7K4P9DPBYK2WX\n")
    (cap / "trace.jsonl").write_text('{"event": "start"}\n')
    return cap


def test_subject_digest_matches_merkle_root_without_comments(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    assert capsule_subject_digest(cap) == capsule_merkle_root(cap)


def test_subject_digest_stable_across_comments(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    digest_before = capsule_subject_digest(cap)
    append_comment(cap / COMMENTS_FILENAME, _comment(subject=digest_before))
    # The annotation stream never alters the identity it annotates.
    assert capsule_subject_digest(cap) == digest_before


# ── Sealing parity with scores.jsonl (evidence flow unaffected) ───────────────


def test_adding_a_comment_changes_the_capsule_root(tmp_path: Path) -> None:
    # Same convention as scores.jsonl (tests/eval/test_score_sealing.py): the
    # Evidence-Bundle subject digest covers comments.jsonl, so comment
    # tampering is detected; no change to the seal path is required.
    cap = _capsule(tmp_path)
    root_before = capsule_merkle_root(cap)
    append_comment(cap / COMMENTS_FILENAME, _comment())
    assert capsule_merkle_root(cap) != root_before


def test_capsule_without_comments_still_seals(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    assert capsule_merkle_root(cap).startswith("sha256:")
    assert not (cap / COMMENTS_FILENAME).exists()


# ── JSON Schema conformance (schemas/comment.schema.json) ─────────────────────


def test_comment_validates_against_json_schema() -> None:
    instance = json.loads(
        _comment(tags=["review"], in_reply_to=new_ulid()).model_dump_json(exclude_none=True)
    )
    _VALIDATOR.validate(instance)


def test_json_schema_rejects_bad_digest() -> None:
    instance = json.loads(_comment().model_dump_json(exclude_none=True))
    instance["subject"] = "nope"
    with pytest.raises(jsonschema.ValidationError):
        _VALIDATOR.validate(instance)


def test_all_golden_fixtures_behave_as_named() -> None:
    fixtures = sorted(_FIXTURES.glob("*.json"))
    assert len(fixtures) == 13
    for fixture in fixtures:
        instance = json.loads(fixture.read_text())
        errors = list(_VALIDATOR.iter_errors(instance))
        if fixture.name.startswith("valid-"):
            assert not errors, f"{fixture.name} should validate: {errors}"
        else:
            assert errors, f"{fixture.name} should be rejected"


def test_valid_fixtures_load_as_comment_models() -> None:
    # The Pydantic model accepts exactly what the promoted schema accepts.
    for fixture in sorted(_FIXTURES.glob("valid-*.json")):
        Comment.model_validate_json(fixture.read_text())
