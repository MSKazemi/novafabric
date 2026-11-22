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

"""``tool_calls`` evidence: the capsule recorded at least one tool call.

``nova capture`` creates an empty ``tool-calls.jsonl`` for every run, so the file's
existence says nothing; only a non-empty regular file counts.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from novafabric.compliance.audit.engine import _has_evidence


def _capsule(tmp_path: Path) -> Path:
    cap = tmp_path / "cap"
    cap.mkdir()
    (cap / "capsule.yaml").write_text("run_id: r1\n", encoding="utf-8")
    return cap


def test_a_recorded_tool_call_is_evidence(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    (cap / "tool-calls.jsonl").write_text('{"tool": "search"}\n', encoding="utf-8")
    assert _has_evidence(cap, "tool_calls") is True


def test_the_empty_file_every_capture_creates_is_not_evidence(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    (cap / "tool-calls.jsonl").touch()
    assert _has_evidence(cap, "tool_calls") is False


def test_a_missing_file_is_not_evidence(tmp_path: Path) -> None:
    assert _has_evidence(_capsule(tmp_path), "tool_calls") is False


def test_a_symlinked_stream_is_not_evidence(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    outside = tmp_path / "outside.jsonl"
    outside.write_text('{"tool": "search"}\n', encoding="utf-8")
    (cap / "tool-calls.jsonl").symlink_to(outside)
    assert _has_evidence(cap, "tool_calls") is False


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_a_fifo_stream_is_not_evidence_and_does_not_block(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    os.mkfifo(cap / "tool-calls.jsonl")
    assert _has_evidence(cap, "tool_calls") is False
