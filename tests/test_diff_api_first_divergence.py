"""``GET /api/diff`` carries the same first divergence as ``nova diff --output-format json``.

The dashboard's aligned A/B view (issue #270) reads ``report.first_divergence`` and
the per-pair record ids from the API response instead of re-diffing in the browser,
so the API must expose exactly what the CLI computes (issue #74), and the record
ids it names must identify one aligned pair.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from novafabric.cli.main import app as cli_app
from novafabric.serve.app import create_app

TOKEN = "test-token-1234567890abcdef"
runner = CliRunner()


def _model(n: int, answer: str) -> dict[str, Any]:
    return {
        "model_call_id": f"MC-{n}",
        "parent_span_id": "root",
        "gen_ai.system": "openai",
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.request.messages": [{"role": "user", "content": f"p{n}"}],
        "gen_ai.response.choices": [{"index": 0, "message": {"content": answer}}],
    }


def _capsule(root: Path, run_id: str, models: list[dict[str, Any]]) -> Path:
    cap = root / run_id
    (cap / "outputs").mkdir(parents=True)
    (cap / "capsule.yaml").write_text(yaml.dump({"run_id": run_id, "status": "success"}))
    (cap / "env.lock").write_text(yaml.dump({"python": {"version": "3.12.3"}}))
    (cap / "model-calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in models))
    (cap / "tool-calls.jsonl").write_text("")
    (cap / "outputs" / "stdout.txt").write_text("hello\n")
    return cap


def _api_diff(runs: Path, tmp_path: Path, run_a: str, run_b: str) -> dict[str, Any]:
    app_ = create_app(
        token=TOKEN, capsule_dir=runs, db_path=tmp_path / "registry.db", static_dir=None
    )
    with TestClient(app_) as client:
        res = client.get(
            f"/api/diff?run_a={run_a}&run_b={run_b}&token={TOKEN}",
            headers={"host": "127.0.0.1:4321"},
        )
    assert res.status_code == 200, res.text
    body: dict[str, Any] = res.json()
    return body


def test_api_first_divergence_matches_the_cli_and_names_one_pair(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    a = _capsule(runs, "run-a", [_model(1, "same"), _model(2, "yes")])
    b = _capsule(runs, "run-b", [_model(1, "same"), _model(2, "no")])

    body = _api_diff(runs, tmp_path, "run-a", "run-b")
    cli = runner.invoke(cli_app, ["diff", str(a), str(b), "--output-format", "json"])
    assert cli.exit_code == 0, cli.output
    expected = json.loads(cli.stdout)["first_divergence"]

    divergence = body["report"]["first_divergence"]
    assert divergence == expected
    assert divergence["stream"] == "model_calls"
    assert divergence["reason"] == "changed"
    # The ids the view matches on identify exactly one aligned pair.
    pairs = body["report"]["model_call_pairs"]
    hits = [
        p
        for p in pairs
        if p.get("model_call_id_a") == divergence["record_id_a"]
        and p.get("model_call_id_b") == divergence["record_id_b"]
    ]
    assert len(hits) == 1 and hits[0]["changed"] is True
    assert body["run_a_id"] == "run-a" and body["run_b_id"] == "run-b"


def test_api_first_divergence_is_null_for_agreeing_calls(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _capsule(runs, "run-a", [_model(1, "same")])
    _capsule(runs, "run-b", [_model(1, "same")])

    body = _api_diff(runs, tmp_path, "run-a", "run-b")
    assert "first_divergence" in body["report"]
    assert body["report"]["first_divergence"] is None
    assert body["has_changes"] is False
