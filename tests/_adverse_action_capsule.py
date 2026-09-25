"""Shared capsule builders for the ADR-0159 NF-278 adverse-action tests (not a test module).

Capsules built with a ``facet`` are reader fixtures only: ``feature_attribution`` is not in the
closed run-capsule facet registry (ADR-0196 D2), so they are NOT schema-valid run capsules and
no test here validates them against ``schemas/run-capsule.schema.json``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

FIXTURES = Path(__file__).parent / "fixtures" / "finance-adverse-action"
PROMPT = "Applicant Jane Q. Public, income 52000, requests 20000 USD"
SECRET = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH"


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def call(call_id: str, model: str = "credit-underwriter-v4") -> dict[str, Any]:
    return {
        "model_call_id": call_id,
        "gen_ai.system": "openai",
        "gen_ai.request.model": model,
        "gen_ai.response.model": model,
        "gen_ai.request.messages": [{"role": "user", "content": PROMPT}],
        "gen_ai.response.choices": [{"message": {"content": "DENY"}}],
        "status": "success",
        "started_at": "2026-09-01T10:00:00Z",
    }


def valid_facet() -> dict[str, Any]:
    return yaml.safe_load((FIXTURES / "feature_attribution_valid.yaml").read_text())


def make_capsule(
    root: Path,
    *,
    calls: list[dict[str, Any]] | None = None,
    inputs: dict[str, bytes] | None = None,
    facet: Any = None,
    seal: bool = True,
    bind: bool = True,
    extra_manifest: dict[str, Any] | None = None,
) -> Path:
    """Write a capsule the way capture does: evidence files, then digests into capsule.yaml."""
    cap = root / "01RUNCREDIT"
    cap.mkdir(parents=True)
    (cap / "inputs").mkdir()
    if calls is not None:
        (cap / "model-calls.jsonl").write_text(
            "".join(json.dumps(c, separators=(",", ":")) + "\n" for c in calls)
        )
    for name, data in (inputs or {}).items():
        (cap / "inputs" / name).write_bytes(data)
    manifest: dict[str, Any] = {"schema_version": 1, "run_id": "01RUNCREDIT", "status": "success"}
    if bind:
        manifest["evidence_digests"] = {
            p.relative_to(cap).as_posix(): {"sha256": sha(p.read_bytes())}
            for p in sorted(cap.rglob("*"))
            if p.is_file()
        }
    if facet is not None:
        manifest["facets"] = {"feature_attribution": facet}
    manifest.update(extra_manifest or {})
    (cap / "capsule.yaml").write_text(yaml.safe_dump(manifest, sort_keys=True))
    if seal:
        (cap / ".seal").mkdir()
        (cap / ".seal" / "manifest.dsse").write_bytes(b"{}")
    return cap
