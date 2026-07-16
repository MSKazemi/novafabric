"""Shared fixture maskers for the ADR-0135 masking pipeline tests."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from novafabric.masking import UNCHANGED, MaskContext, MaskField


class CaseIdMasker:
    """Masks ACME-CASE-<digits> identifiers (the ADR's running example)."""

    masker_id = "acme-case-id"
    masker_version = "1"
    pattern_ids = ("acme-case-number",)

    def mask(self, field: MaskField, value: str, context: MaskContext) -> Any:
        import re

        prefix = str(context.masker_config.get("prefix", "ACME-CASE"))
        pattern = re.compile(re.escape(prefix) + r"-\d+")
        masked = pattern.sub(f"[MASKED:{self.masker_id}]", value)
        return UNCHANGED if masked == value else masked


class CrashingMasker:
    masker_id = "crashing-masker"
    masker_version = "1"
    pattern_ids = ("crash",)

    def mask(self, field: MaskField, value: str, context: MaskContext) -> Any:
        raise RuntimeError("boom")


class SleepyMasker:
    masker_id = "sleepy-masker"
    masker_version = "1"
    pattern_ids = ("sleep",)

    def mask(self, field: MaskField, value: str, context: MaskContext) -> Any:
        time.sleep(0.5)
        return UNCHANGED


class LeakyMasker:
    """Returns a 'masked' value that still contains the raw value verbatim."""

    masker_id = "leaky-masker"
    masker_version = "1"
    pattern_ids = ("leak",)

    def mask(self, field: MaskField, value: str, context: MaskContext) -> Any:
        return f"prefix-{value}-suffix"


class NotAStringMasker:
    masker_id = "not-a-string"
    masker_version = "1"
    pattern_ids = ("bad-type",)

    def mask(self, field: MaskField, value: str, context: MaskContext) -> Any:
        return 42


class NoOpMasker:
    """Returns the raw value unchanged — must be treated as UNCHANGED."""

    masker_id = "noop-masker"
    masker_version = "1"
    pattern_ids = ("noop",)

    def mask(self, field: MaskField, value: str, context: MaskContext) -> Any:
        return value


def write_capsule_files(capsule_dir: Path, secret: str = "", email: str = "") -> None:
    """Write a minimal set of scan-target files into ``capsule_dir``."""
    capsule_dir.mkdir(parents=True, exist_ok=True)
    model_calls = [
        {
            "call_id": "call-1",
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": f"open case ACME-CASE-12345 {secret}".strip()},
            ],
        },
    ]
    tool_calls = [
        {"tool_name": "open_ticket", "arguments": {"case": "ACME-CASE-99", "note": email}},
    ]
    trace = [{"span_id": "s1", "name": "root", "attributes": {"command": "python agent.py"}}]
    (capsule_dir / "model-calls.jsonl").write_text(
        "\n".join(json.dumps(x) for x in model_calls) + "\n"
    )
    (capsule_dir / "tool-calls.jsonl").write_text(
        "\n".join(json.dumps(x) for x in tool_calls) + "\n"
    )
    (capsule_dir / "trace.jsonl").write_text("\n".join(json.dumps(x) for x in trace) + "\n")


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    d = tmp_path / "capsule"
    write_capsule_files(d)
    return d
