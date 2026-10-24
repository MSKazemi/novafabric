"""``nova diff`` sees a tool call that started failing, and digest-only changes.

Review replay-lineage-registry-06: tool-call pairs compared only ``result`` and
``arguments``. At the default capture level ``record.tool``, ADK and Agents-SDK
records carry only digests, so a side-effecting tool that went from success to
"card declined" with different arguments diffed as unchanged and the
``--assert-no-regressions`` gate passed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from novafabric.diff._engine import DiffEngine


def _capsule(root: Path, name: str, call: dict[str, Any]) -> Path:
    cap = root / name
    cap.mkdir()
    (cap / "capsule.yaml").write_text(f"run_id: {name}\n")
    (cap / "tool-calls.jsonl").write_text(json.dumps(call) + "\n")
    return cap


def _call(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "tool_call_id": "t1", "tool_name": "charge_card", "status": "success",
        "extensions": {
            "io.novafabric.arguments_digest": "sha256:" + "a" * 64,
            "io.novafabric.result_digest": "sha256:" + "b" * 64,
        },
    }
    base.update(over)
    return base


def _pair(tmp_path: Path, a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    report = DiffEngine().compare(_capsule(tmp_path, "a", a), _capsule(tmp_path, "b", b))
    (pair,) = report.tool_call_pairs
    return pair


def test_success_to_failure_is_a_change(tmp_path: Path) -> None:
    failed = _call(status="error", error={"type": "CardDeclined", "message": "card declined"})
    pair = _pair(tmp_path, _call(), failed)
    assert pair["changed"] and pair["status_changed"]


@pytest.mark.parametrize("key", ["io.novafabric.arguments_digest", "io.novafabric.result_digest"])
def test_a_digest_only_change_is_a_change(tmp_path: Path, key: str) -> None:
    other = _call()
    other["extensions"] = {**other["extensions"], key: "sha256:" + "c" * 64}
    assert _pair(tmp_path, _call(), other)["changed"]


def test_a_digest_on_one_side_only_is_a_change(tmp_path: Path) -> None:
    other = _call()
    other["extensions"] = {"io.novafabric.result_digest": "sha256:" + "b" * 64}
    assert _pair(tmp_path, _call(), other)["arguments_changed"]


def test_identical_calls_are_unchanged(tmp_path: Path) -> None:
    pair = _pair(tmp_path, _call(), _call())
    assert not pair["changed"] and not pair["status_changed"]
