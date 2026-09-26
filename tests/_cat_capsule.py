"""Shared capsule builders for the ADR-0159 NF-280 CAT-style trail tests (not a test module)."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

FIXTURES = Path(__file__).parent / "fixtures" / "finance-cat"
STREAMS = (
    "model-calls.jsonl",
    "tool-permission-events.jsonl",
    "human_approvals.jsonl",
    "tool-calls.jsonl",
)
SECRET = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH"


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def valid_fixture() -> dict[str, Any]:
    return json.loads((FIXTURES / "lifecycle_valid.json").read_text())


def invalid_tool_calls() -> dict[str, dict[str, Any]]:
    cases: dict[str, dict[str, Any]] = json.loads(
        (FIXTURES / "lifecycle_invalid.json").read_text()
    )["tool-calls.jsonl"]
    cases["overlong_id"]["tool_call_id"] = "x" * 1025
    return cases


def make_capsule(
    root: Path,
    *,
    streams: dict[str, list[dict[str, Any]]] | None = None,
    manifest: dict[str, Any] | None = None,
    seal: bool = True,
    bind: bool = True,
) -> Path:
    """Write a capsule the way capture does: streams, then their digests into capsule.yaml.

    ``streams`` / ``manifest`` default to the golden valid fixture; pass ``{}`` to omit.
    """
    fx = valid_fixture()
    if streams is None:
        streams = {name: copy.deepcopy(fx[name]) for name in STREAMS}
    cap = root / "01RUNCAT"
    cap.mkdir(parents=True)
    for name, records in streams.items():
        (cap / name).write_text(
            "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records)
        )
    body: dict[str, Any] = {"schema_version": 1, "run_id": "01RUNCAT"}
    body.update(fx["manifest"] if manifest is None else manifest)
    if bind:
        body["evidence_digests"] = {
            p.relative_to(cap).as_posix(): {"sha256": sha(p.read_bytes())}
            for p in sorted(cap.rglob("*"))
            if p.is_file()
        }
    (cap / "capsule.yaml").write_text(yaml.safe_dump(body, sort_keys=True))
    if seal:
        (cap / ".seal").mkdir()
        (cap / ".seal" / "manifest.dsse").write_bytes(b"{}")
    return cap
