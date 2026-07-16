"""Multi-modal capture (ADR-0125, multimodal-capture-v0).

Covers: inline base64 media on model-call messages rewritten to
content-addressed MediaPart references through the real ``CapsuleWriter``
write path (Anthropic base64 source, OpenAI ``image_url`` data-URL, OpenAI
``input_audio``, canonical model-call-v1 inline source); privacy-by-default
reference-only mode vs the ``NOVAFABRIC_CAPTURE_MEDIA`` opt-in; the bounded
per-part size cap; blob dedup; byte-identical passthrough for records with no
media; caller-kwargs immutability; blob integrity verification (tamper +
missing blob); manifest Artifact collection; and the ``nova media list`` /
``nova validate`` surfaces over a real end-to-end ``nova capture`` run.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import textwrap
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from novafabric.capture.capsule import CapsuleWriter
from novafabric.capture.media import (
    DEFAULT_MEDIA_MAX_BYTES,
    MEDIA_CAPTURE_ENV,
    MEDIA_MAX_BYTES_ENV,
    annotate_model_call_media,
    collect_media_artifacts,
    iter_media_parts,
    media_capture_enabled,
    media_max_bytes,
    verify_media_blobs,
)

RUN_ID = "01HXTEST000000000000000000"
CALL_ID = "01HXCA7700000000000000MEDA"

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"not-really-pixels-but-stable-bytes"
PNG_B64 = base64.b64encode(PNG_BYTES).decode()
PNG_SHA = hashlib.sha256(PNG_BYTES).hexdigest()

WAV_BYTES = b"RIFF....WAVEfmt " + b"\x00" * 16
WAV_B64 = base64.b64encode(WAV_BYTES).decode()
WAV_SHA = hashlib.sha256(WAV_BYTES).hexdigest()

REPO_ROOT = Path(__file__).resolve().parents[2]


def make_writer(tmp_path: Path) -> CapsuleWriter:
    w = CapsuleWriter(run_id=RUN_ID, base_dir=tmp_path)
    w.open()
    return w


def _model_calls(tmp_path: Path) -> list[dict[str, Any]]:
    text = (tmp_path / RUN_ID / "model-calls.jsonl").read_text().strip()
    return [json.loads(line) for line in text.splitlines() if line]


def _record(messages: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "schema_version": "0.1.0",
        "model_call_id": CALL_ID,
        "gen_ai.system": "anthropic",
        "gen_ai.request.model": "claude-x",
        "gen_ai.request.messages": messages,
        "gen_ai.response.choices": [],
        "status": "success",
    }
    rec.update(extra)
    return rec


def _anthropic_image_message() -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "What breed is this dog?"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": PNG_B64,
                },
            },
        ],
    }


def _media_validator() -> Draft202012Validator:
    schema = json.loads(
        (REPO_ROOT / "schemas" / "media-part.schema.json").read_text()
    )
    return Draft202012Validator(schema, format_checker=FormatChecker())


# ── opt-in gating ────────────────────────────────────────────────────────────

def test_media_capture_disabled_by_default() -> None:
    # The hermetic conftest fixture strips ambient NOVAFABRIC_* vars.
    assert media_capture_enabled() is False


def test_media_capture_env_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MEDIA_CAPTURE_ENV, "1")
    assert media_capture_enabled() is True
    monkeypatch.setenv(MEDIA_CAPTURE_ENV, "0")
    assert media_capture_enabled() is False


def test_media_max_bytes_default_and_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert media_max_bytes() == DEFAULT_MEDIA_MAX_BYTES
    monkeypatch.setenv(MEDIA_MAX_BYTES_ENV, "1234")
    assert media_max_bytes() == 1234
    monkeypatch.setenv(MEDIA_MAX_BYTES_ENV, "not-an-int")
    assert media_max_bytes() == DEFAULT_MEDIA_MAX_BYTES
    monkeypatch.setenv(MEDIA_MAX_BYTES_ENV, "-1")
    assert media_max_bytes() == DEFAULT_MEDIA_MAX_BYTES


# ── reference-only default (D2) ──────────────────────────────────────────────

def test_reference_only_by_default_hash_without_bytes(tmp_path: Path) -> None:
    writer = make_writer(tmp_path)
    writer.append_model_call(_record([_anthropic_image_message()]))

    [rec] = _model_calls(tmp_path)
    part = rec["gen_ai.request.messages"][0]["content"][1]
    media = part["media"]
    assert part["type"] == "image"
    assert media["content_hash"] == f"sha256:{PNG_SHA}"
    assert media["byte_size"] == len(PNG_BYTES)
    assert media["blob_ref"] is None
    assert media["redacted"] is False
    # bytes are discarded: nothing stored, base64 gone from the record
    assert list((tmp_path / RUN_ID / "outputs").iterdir()) == []
    assert PNG_B64 not in json.dumps(rec)


# ── byte capture opt-in (D1/D2) ──────────────────────────────────────────────

def test_image_captured_content_addressed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MEDIA_CAPTURE_ENV, "1")
    writer = make_writer(tmp_path)
    writer.append_model_call(_record([_anthropic_image_message()]))

    [rec] = _model_calls(tmp_path)
    media = rec["gen_ai.request.messages"][0]["content"][1]["media"]
    assert media["blob_ref"] == f"outputs/{PNG_SHA}.png"
    blob = tmp_path / RUN_ID / media["blob_ref"]
    assert blob.read_bytes() == PNG_BYTES
    assert hashlib.sha256(blob.read_bytes()).hexdigest() == PNG_SHA
    # the record carries the reference, never the inline base64
    assert PNG_B64 not in json.dumps(rec)


def test_openai_data_url_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MEDIA_CAPTURE_ENV, "1")
    writer = make_writer(tmp_path)
    writer.append_model_call(_record([{
        "role": "user",
        "content": [
            {"type": "text", "text": "Describe"},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{PNG_B64}"},
            },
        ],
    }], **{"gen_ai.system": "openai"}))

    [rec] = _model_calls(tmp_path)
    part = rec["gen_ai.request.messages"][0]["content"][1]
    assert part["type"] == "image"
    assert part["media"]["media_type"] == "image/png"
    assert part["media"]["content_hash"] == f"sha256:{PNG_SHA}"
    assert part["media"]["blob_ref"] == f"outputs/{PNG_SHA}.png"


def test_openai_input_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MEDIA_CAPTURE_ENV, "1")
    writer = make_writer(tmp_path)
    writer.append_model_call(_record([{
        "role": "user",
        "content": [
            {"type": "input_audio", "input_audio": {"data": WAV_B64, "format": "wav"}},
        ],
    }], **{"gen_ai.system": "openai"}))

    [rec] = _model_calls(tmp_path)
    part = rec["gen_ai.request.messages"][0]["content"][0]
    assert part["type"] == "audio"
    assert part["media"]["media_type"] == "audio/wav"
    assert part["media"]["content_hash"] == f"sha256:{WAV_SHA}"
    assert (tmp_path / RUN_ID / f"outputs/{WAV_SHA}.wav").read_bytes() == WAV_BYTES


def test_response_choice_media_rewritten(tmp_path: Path) -> None:
    """Canonical inline media on the response side gets the same treatment."""
    writer = make_writer(tmp_path)
    record = _record([], **{"gen_ai.response.choices": [{
        "index": 0,
        "message": {
            "role": "assistant",
            "content": [{
                "type": "image",
                "source": {"kind": "inline", "media_type": "image/png", "data": PNG_B64},
            }],
        },
        "finish_reason": "stop",
    }]})
    writer.append_model_call(record)

    [rec] = _model_calls(tmp_path)
    part = rec["gen_ai.response.choices"][0]["message"]["content"][0]
    assert part["media"]["content_hash"] == f"sha256:{PNG_SHA}"
    assert part["media"]["blob_ref"] is None  # default: reference-only


# ── schema validity of produced blocks ───────────────────────────────────────

def test_produced_media_block_validates_against_schema(tmp_path: Path) -> None:
    writer = make_writer(tmp_path)
    writer.append_model_call(_record([_anthropic_image_message()]))
    parts = list(iter_media_parts(tmp_path / RUN_ID))
    assert len(parts) == 1
    call_id, media = parts[0]
    assert call_id == CALL_ID
    _media_validator().validate(media)


# ── byte-identical passthrough & immutability ────────────────────────────────

def test_absent_media_record_is_byte_identical(tmp_path: Path) -> None:
    writer = make_writer(tmp_path)
    record = _record([
        {"role": "user", "content": "plain text"},
        {"role": "user", "content": [{"type": "text", "text": "multi-part text"}]},
    ])
    expected = json.dumps(deepcopy(record), separators=(",", ":"))
    writer.append_model_call(record)
    line = (tmp_path / RUN_ID / "model-calls.jsonl").read_text().strip()
    assert line == expected


def test_caller_messages_are_not_mutated(tmp_path: Path) -> None:
    writer = make_writer(tmp_path)
    message = _anthropic_image_message()
    writer.append_model_call(_record([message]))
    # the caller-owned message object still carries its inline bytes
    assert message["content"][1]["source"]["data"] == PNG_B64
    assert "media" not in message["content"][1]


def test_invalid_base64_part_left_untouched(tmp_path: Path) -> None:
    writer = make_writer(tmp_path)
    part = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "!!not-b64!!"},
    }
    writer.append_model_call(_record([{"role": "user", "content": [part]}]))
    [rec] = _model_calls(tmp_path)
    assert rec["gen_ai.request.messages"][0]["content"][0] == part


def test_url_referenced_part_left_untouched(tmp_path: Path) -> None:
    """No inline bytes at the boundary ⇒ never fetched, never rewritten."""
    writer = make_writer(tmp_path)
    part = {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}}
    writer.append_model_call(_record([{"role": "user", "content": [part]}]))
    [rec] = _model_calls(tmp_path)
    assert rec["gen_ai.request.messages"][0]["content"][0] == part


# ── dedup & size bound (D4) ──────────────────────────────────────────────────

def test_identical_media_dedups_to_one_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MEDIA_CAPTURE_ENV, "1")
    writer = make_writer(tmp_path)
    writer.append_model_call(_record([_anthropic_image_message()]))
    writer.append_model_call(_record([_anthropic_image_message()]))
    blobs = list((tmp_path / RUN_ID / "outputs").iterdir())
    assert [b.name for b in blobs] == [f"{PNG_SHA}.png"]
    assert len(_model_calls(tmp_path)) == 2


def test_size_bound_degrades_to_reference_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MEDIA_CAPTURE_ENV, "1")
    monkeypatch.setenv(MEDIA_MAX_BYTES_ENV, str(len(PNG_BYTES) - 1))
    writer = make_writer(tmp_path)
    writer.append_model_call(_record([_anthropic_image_message()]))

    [rec] = _model_calls(tmp_path)
    media = rec["gen_ai.request.messages"][0]["content"][1]["media"]
    # hash + size still recorded; bytes neither stored nor inlined
    assert media["content_hash"] == f"sha256:{PNG_SHA}"
    assert media["blob_ref"] is None
    assert list((tmp_path / RUN_ID / "outputs").iterdir()) == []
    assert PNG_B64 not in json.dumps(rec)


# ── integrity verification (tamper detection) ────────────────────────────────

def _captured_capsule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(MEDIA_CAPTURE_ENV, "1")
    writer = make_writer(tmp_path)
    writer.append_model_call(_record([_anthropic_image_message()]))
    return tmp_path / RUN_ID


def test_intact_blob_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule_dir = _captured_capsule(tmp_path, monkeypatch)
    assert verify_media_blobs(capsule_dir) == []


def test_tampered_blob_is_an_integrity_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule_dir = _captured_capsule(tmp_path, monkeypatch)
    blob = capsule_dir / "outputs" / f"{PNG_SHA}.png"
    blob.write_bytes(b"TAMPERED" + PNG_BYTES)
    errors = verify_media_blobs(capsule_dir)
    assert len(errors) == 1
    assert "integrity error" in errors[0]
    assert f"sha256:{PNG_SHA}" in errors[0]


def test_missing_blob_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule_dir = _captured_capsule(tmp_path, monkeypatch)
    (capsule_dir / "outputs" / f"{PNG_SHA}.png").unlink()
    errors = verify_media_blobs(capsule_dir)
    assert len(errors) == 1
    assert "missing media blob" in errors[0]


def test_reference_only_capsule_has_no_integrity_errors(tmp_path: Path) -> None:
    writer = make_writer(tmp_path)
    writer.append_model_call(_record([_anthropic_image_message()]))
    assert verify_media_blobs(tmp_path / RUN_ID) == []


# ── manifest artifacts ───────────────────────────────────────────────────────

def test_collect_media_artifacts_builds_manifest_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule_dir = _captured_capsule(tmp_path, monkeypatch)
    artifacts = collect_media_artifacts(capsule_dir)
    assert artifacts == [{
        "name": f"image-{PNG_SHA[:12]}",
        "path": f"outputs/{PNG_SHA}.png",
        "content_hash": f"sha256:{PNG_SHA}",
        "size_bytes": len(PNG_BYTES),
        "media_type": "image/png",
        "produced_by_call": CALL_ID,
    }]


def test_collect_media_artifacts_skips_reference_only_and_dedups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MEDIA_CAPTURE_ENV, "1")
    writer = make_writer(tmp_path)
    writer.append_model_call(_record([_anthropic_image_message()]))
    writer.append_model_call(_record([_anthropic_image_message()]))  # dedup
    monkeypatch.setenv(MEDIA_CAPTURE_ENV, "0")
    writer.append_model_call(_record([{
        "role": "user",
        "content": [{
            "type": "audio",
            "source": {"type": "base64", "media_type": "audio/wav", "data": WAV_B64},
        }],
    }]))  # reference-only: no artifact
    artifacts = collect_media_artifacts(tmp_path / RUN_ID)
    assert len(artifacts) == 1
    assert artifacts[0]["content_hash"] == f"sha256:{PNG_SHA}"


def test_collect_media_artifacts_empty_without_media(tmp_path: Path) -> None:
    writer = make_writer(tmp_path)
    writer.append_model_call(_record([{"role": "user", "content": "hi"}]))
    assert collect_media_artifacts(tmp_path / RUN_ID) == []


# ── hook funnel ──────────────────────────────────────────────────────────────

def test_anthropic_hook_records_media_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vision call through the real AnthropicHook funnel gets a MediaPart."""
    from unittest.mock import MagicMock

    from novafabric.capture.hooks._anthropic import AnthropicHook

    monkeypatch.setenv(MEDIA_CAPTURE_ENV, "1")
    writer = make_writer(tmp_path)
    hook = AnthropicHook(writer=writer, parent_span_id="aabbccddeeff0011")

    fake_response = MagicMock()
    fake_response.model = "claude-x"
    fake_response.content = [MagicMock(text="A golden retriever.")]
    fake_response.stop_reason = "end_turn"
    fake_response.usage = MagicMock(input_tokens=10, output_tokens=5)

    kwargs_messages = [_anthropic_image_message()]
    hook._intercept(
        MagicMock(return_value=fake_response),
        model="claude-x",
        messages=kwargs_messages,
    )

    [rec] = _model_calls(tmp_path)
    media = rec["gen_ai.request.messages"][0]["content"][1]["media"]
    assert media["content_hash"] == f"sha256:{PNG_SHA}"
    assert media["blob_ref"] == f"outputs/{PNG_SHA}.png"
    # the SDK caller's kwargs were not mutated
    assert kwargs_messages[0]["content"][1]["source"]["data"] == PNG_B64


# ── end-to-end: orchestrator + validate + media list ─────────────────────────

WORKLOAD = textwrap.dedent(
    """\
    import base64, json, os
    from pathlib import Path
    from novafabric.capture.capsule import CapsuleWriter

    capsule_dir = Path(os.environ["NOVAFABRIC_CAPSULE_DIR"])
    w = CapsuleWriter(run_id=capsule_dir.name, base_dir=capsule_dir.parent)
    w._dir = capsule_dir
    png = {png!r}
    w.append_model_call({{
        "schema_version": "0.1.0",
        "model_call_id": "01HXCA7700000000000000MEDA",
        "gen_ai.system": "anthropic",
        "gen_ai.request.model": "claude-x",
        "gen_ai.request.messages": [{{
            "role": "user",
            "content": [{{
                "type": "image",
                "source": {{
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.b64encode(png).decode(),
                }},
            }}],
        }}],
        "gen_ai.response.choices": [],
        "status": "success",
    }})
    print("workload-done")
    """
)


@pytest.fixture()
def captured_run(tmp_path: Path) -> Path:
    """A real `nova capture --capture-media` run over a media-emitting workload."""
    from novafabric.capture.orchestrator import CaptureOrchestrator

    script = tmp_path / "workload.py"
    script.write_text(WORKLOAD.format(png=PNG_BYTES))
    orch = CaptureOrchestrator(base_dir=tmp_path / "runs", capture_media=True)
    result = orch.run(command=[sys.executable, str(script)])
    assert result.exit_code == 0
    return result.capsule_dir


def test_end_to_end_capture_media_blob_and_manifest(captured_run: Path) -> None:
    blob = captured_run / "outputs" / f"{PNG_SHA}.png"
    assert blob.read_bytes() == PNG_BYTES

    import yaml

    manifest = yaml.safe_load((captured_run / "capsule.yaml").read_text())
    media_outputs = [
        a for a in manifest["outputs"]
        if a["content_hash"] == f"sha256:{PNG_SHA}"
    ]
    assert media_outputs and media_outputs[0]["path"] == f"outputs/{PNG_SHA}.png"


def test_end_to_end_nova_validate_accepts_then_flags_tamper(
    captured_run: Path,
) -> None:
    import typer

    from novafabric.cli.validate import _validate_capsule

    _validate_capsule(captured_run)  # intact capsule validates

    blob = captured_run / "outputs" / f"{PNG_SHA}.png"
    blob.write_bytes(b"TAMPERED")
    with pytest.raises(typer.Exit) as excinfo:
        _validate_capsule(captured_run)
    assert excinfo.value.exit_code == 1


def test_nova_media_list_reads_capsule(captured_run: Path) -> None:
    from typer.testing import CliRunner

    from novafabric.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["media", "list", str(captured_run)])
    assert result.exit_code == 0
    assert "Media parts" in result.output  # rich table may wrap the hash

    result_json = runner.invoke(app, ["media", "list", "--json", str(captured_run)])
    assert result_json.exit_code == 0
    parsed = json.loads(result_json.output)
    assert parsed[0]["content_hash"] == f"sha256:{PNG_SHA}"


def test_nova_media_list_empty_capsule(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from novafabric.cli.main import app

    make_writer(tmp_path)
    capsule_dir = tmp_path / RUN_ID
    (capsule_dir / "capsule.yaml").write_text("run_id: " + RUN_ID)
    result = CliRunner().invoke(app, ["media", "list", str(capsule_dir)])
    assert result.exit_code == 0
    assert "No media parts" in result.output


def test_nova_media_list_rejects_non_capsule(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from novafabric.cli.main import app

    result = CliRunner().invoke(app, ["media", "list", str(tmp_path)])
    assert result.exit_code == 2


def test_annotate_is_fail_open_on_bad_capsule_dir(tmp_path: Path) -> None:
    """A broken blob store degrades to reference-only, never raises."""
    record = _record([_anthropic_image_message()])
    bogus = tmp_path / "not-a-dir-file"
    bogus.write_text("occupied")
    annotate_model_call_media(record, bogus / "outputs-cannot-exist", capture_bytes=True)
    media = record["gen_ai.request.messages"][0]["content"][1]["media"]
    assert media["content_hash"] == f"sha256:{PNG_SHA}"
    assert media["blob_ref"] is None
