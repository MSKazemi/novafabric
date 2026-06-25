"""Tests for the reference plugin.

Verifies (a) the hook satisfies NovaFabric's HookProtocol, (b) install
and uninstall are clean, (c) a captured call lands one record with the
expected shape including the plugin's own extension fields, and (d) the
``info()`` classmethod returns the documented metadata.

These tests run from this example directory; they exercise the plugin
in isolation, not via end-to-end discovery (which the project's main
``tests/test_capture_plugin_contract.py`` already covers via fake
entry points).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from novafabric_example_plugin.hook import ExampleHook, fake_acme_ai

from novafabric.capture.capsule import CapsuleWriter
from novafabric.capture.hooks._plugin import HookPluginInfo, HookProtocol

RUN_ID = "01TESTPLUGINREF00000000000000"


@pytest.fixture
def writer(tmp_path: Path) -> CapsuleWriter:
    w = CapsuleWriter(run_id=RUN_ID, base_dir=tmp_path)
    w.open()
    return w


def _model_calls(tmp_path: Path) -> list[dict]:  # type: ignore[type-arg]
    text = (tmp_path / RUN_ID / "model-calls.jsonl").read_text().strip()
    return [json.loads(line) for line in text.splitlines() if line]


def test_hook_satisfies_protocol() -> None:
    """The runtime-checkable HookProtocol has install/uninstall; the
    plugin must instantiate without error and pass the structural check."""
    hook = ExampleHook(writer=None, parent_span_id="0" * 16)  # type: ignore[arg-type]
    assert isinstance(hook, HookProtocol)


def test_info_returns_documented_metadata() -> None:
    info = ExampleHook.info()
    assert isinstance(info, HookPluginInfo)
    assert info.name == "acme-ai-reference"
    assert info.version == "0.1.0"
    assert "model_calls" in info.capabilities


def test_install_and_uninstall_round_trip(writer: CapsuleWriter) -> None:
    """install() patches; uninstall() restores. After uninstall the
    target's `create` is byte-identical to the original."""
    original_create = fake_acme_ai.create
    hook = ExampleHook(writer=writer, parent_span_id="0" * 16)
    hook.install()
    assert fake_acme_ai.create is not original_create
    hook.uninstall()
    assert fake_acme_ai.create is original_create


def test_uninstall_is_idempotent(writer: CapsuleWriter) -> None:
    hook = ExampleHook(writer=writer, parent_span_id="0" * 16)
    hook.uninstall()  # not installed; should be a no-op
    hook.install()
    hook.uninstall()
    hook.uninstall()  # second uninstall — no error


def test_captured_call_writes_one_record(writer: CapsuleWriter, tmp_path: Path) -> None:
    """The defining test: install the hook, call the target, check
    the capsule's model-calls.jsonl has the expected record."""
    hook = ExampleHook(writer=writer, parent_span_id="aabbccddeeff0011")
    hook.install()
    try:
        response = fake_acme_ai.create(
            model="acme-large-v1",
            prompt="Hi",
            temperature=0.5,
        )
        assert response["model"] == "acme-large-v1"
        assert "fake" in response["completion"]
    finally:
        hook.uninstall()

    records = _model_calls(tmp_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["gen_ai.system"] == "acme-ai"
    assert rec["gen_ai.request.model"] == "acme-large-v1"
    assert rec["gen_ai.request.messages"] == [
        {"role": "user", "content": "Hi"}
    ]
    assert rec["gen_ai.usage.input_tokens"] == 8
    assert rec["gen_ai.usage.output_tokens"] == 12
    # Plugin-attribution extensions present.
    assert rec["extensions"]["io.novafabric.capture_method"] == "plugin"
    assert rec["extensions"]["io.novafabric.plugin_name"] == "acme-ai-reference"
    assert rec["parent_span_id"] == "aabbccddeeff0011"


def test_captured_error_records_status_error(
    writer: CapsuleWriter, tmp_path: Path
) -> None:
    """If the wrapped call raises, the hook records status=error and
    re-raises (mirrors the built-in OpenAI/Anthropic hook pattern)."""
    hook = ExampleHook(writer=writer, parent_span_id="0" * 16)
    hook.install()
    # Replace the original captured by the hook with one that raises.
    raising = lambda model, prompt, **kw: (_ for _ in ()).throw(  # noqa: E731
        ValueError("acme outage")
    )
    hook._original = raising
    try:
        with pytest.raises(ValueError, match="acme outage"):
            fake_acme_ai.create(model="x", prompt="y")
    finally:
        hook.uninstall()

    rec = _model_calls(tmp_path)[0]
    assert rec["status"] == "error"
    assert rec["error"]["type"] == "ValueError"
    assert "acme outage" in rec["error"]["message"]
