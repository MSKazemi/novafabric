"""End-to-end regression test for examples/prompt-and-analytics.

Mirrors what the README walks a user through: register a prompt twice,
label v1 `production`, capture one run per version as two A/B variants,
then analyze offline (query / view / trend / session / diff). If the
example breaks (schema change, CLI rename, etc.), this test fails loudly.

Pure stdlib example — no keys, no network — so this always runs. The
autouse conftest fixture points NOVAFABRIC_HOME at a per-test tmp dir,
which the captured agent subprocess inherits, so the registry, sessions,
and capsules never touch a developer's real data.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

EXAMPLE = (
    Path(__file__).resolve().parent.parent
    / "examples" / "prompt-and-analytics" / "agent.py"
)

_V1_TEMPLATE = (
    "You are a support triage assistant. Classify the ticket into "
    "billing/bug/other and draft a one-line reply. Ticket: {ticket}"
)
_V2_TEMPLATE = (
    "You are a support triage assistant. Classify the ticket into "
    "billing/bug/other and draft a one-line reply. Be empathetic, "
    "acknowledge the issue, and mention the 24h SLA. Ticket: {ticket}"
)

_ULID_RE = re.compile(r"\b[0-9A-HJKMNP-TV-Z]{26}\b")


def _invoke(args: list[str]) -> str:
    result = runner.invoke(app, args)
    assert result.exit_code == 0, (
        f"nova {' '.join(args)} failed: exit={result.exit_code}\n{result.output}"
    )
    return result.output


def _capture(
    runs_dir: Path, *, variant: str, session_id: str, sequence: int,
    monkeypatch: pytest.MonkeyPatch, prompt_env: tuple[str, str],
) -> Path:
    # CliRunner's invoke() runs in-process; the agent subprocess spawned by
    # `nova capture` inherits os.environ, so monkeypatch.setenv reaches it.
    monkeypatch.setenv(prompt_env[0], prompt_env[1])
    monkeypatch.setenv("NOVAFABRIC_SUGGEST", "0")
    before = {d.name for d in runs_dir.iterdir()} if runs_dir.is_dir() else set()
    _invoke(
        ["capture", "--output-dir", str(runs_dir),
         "--experiment", "prompt-rollout", "--variant", variant,
         "--variant-source", "manual",
         "--session-id", session_id, "--session-sequence", str(sequence),
         "--", sys.executable, str(EXAMPLE)],
    )
    monkeypatch.delenv(prompt_env[0], raising=False)
    new = [d for d in runs_dir.iterdir() if d.is_dir() and d.name not in before]
    assert len(new) == 1, f"expected exactly one new capsule, got {new}"
    return new[0]


def test_example_file_exists() -> None:
    assert EXAMPLE.is_file(), f"example missing: {EXAMPLE}"


def test_prompt_analytics_flow_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs_dir = tmp_path / "runs"
    views_dir = tmp_path / "views"
    monkeypatch.setenv("NOVAFABRIC_VIEWS_DIR", str(views_dir))

    # 1. Register v1 + v2, label v1 `production`.
    out = _invoke(
        ["prompt", "register", "support-triage", "-t", _V1_TEMPLATE,
         "--var", "ticket", "-m", "first cut"]
    )
    assert "support-triage@1" in out
    out = _invoke(
        ["prompt", "register", "support-triage", "-t", _V2_TEMPLATE,
         "--var", "ticket", "-m", "add tone + SLA guidance"]
    )
    assert "support-triage@2" in out
    _invoke(["label", "set", "prompt:support-triage", "production", "1"])

    # The label resolves to v1.
    pointer = json.loads(
        _invoke(["label", "get", "prompt:support-triage", "production", "--json"])
    )
    assert pointer["target_version"] == "1"
    assert pointer["content_hash"].startswith("sha256:")

    # 2. Capture two runs: label-resolved v1 (arm A), pinned v2 (arm B).
    sid_out = _invoke(["session", "new", "--kind", "workflow"])
    match = _ULID_RE.search(sid_out)
    assert match, f"no session ULID in output:\n{sid_out}"
    sid = match.group(0)

    cap_a = _capture(
        runs_dir, variant="prompt-v1", session_id=sid, sequence=0,
        monkeypatch=monkeypatch, prompt_env=("PROMPT_LABEL", "production"),
    )
    cap_b = _capture(
        runs_dir, variant="prompt-v2", session_id=sid, sequence=1,
        monkeypatch=monkeypatch, prompt_env=("PROMPT_REF", "support-triage@2"),
    )
    assert (cap_a / "capsule.yaml").exists()
    assert (cap_b / "capsule.yaml").exists()

    # The agent pinned the exact prompt version it ran with.
    stdout_a = (cap_a / "outputs" / "stdout.txt").read_text()
    stdout_b = (cap_b / "outputs" / "stdout.txt").read_text()
    assert "prompt:support-triage@1+sha256:" in stdout_a
    assert "prompt:support-triage@2+sha256:" in stdout_b

    _invoke(["session", "add", sid, str(cap_a)])
    _invoke(["session", "add", sid, str(cap_b)])

    # 3a. nova query: two variant groups with distinct cost/latency.
    report = json.loads(
        _invoke(
            ["query", "--select",
             "count() AS runs, avg(cost) AS avg_cost, p95(latency) AS p95_ms",
             "--group-by", "variant", "--capsule-dir", str(runs_dir), "--json"]
        )
    )
    rows = {row["variant"]: row for row in report["rows"]}
    assert set(rows) == {"prompt-v1", "prompt-v2"}
    assert rows["prompt-v1"]["runs"] == 1
    assert rows["prompt-v2"]["runs"] == 1
    # v2 is a longer template -> deterministically costlier and slower.
    assert rows["prompt-v2"]["avg_cost"] > rows["prompt-v1"]["avg_cost"]
    assert rows["prompt-v2"]["p95_ms"] > rows["prompt-v1"]["p95_ms"]

    # 3b. Saved view: save once, run it as exactly the same query.
    _invoke(
        ["view", "save", "cost-by-variant", "--select",
         "count() AS runs, avg(cost) AS avg_cost, p95(latency) AS p95_ms",
         "--group-by", "variant",
         "--description", "Cost + latency per prompt variant"]
    )
    view_out = _invoke(
        ["view", "run", "cost-by-variant", "--capsule-dir", str(runs_dir), "--json"]
    )
    assert {row["variant"] for row in json.loads(view_out)["rows"]} == {
        "prompt-v1", "prompt-v2",
    }

    # 3c. nova trend: both capsules land in the latency series.
    trend = json.loads(
        _invoke(
            ["trend", "--metric", "latency", "--stat", "p95",
             "--since", "7d", "--capsule-dir", str(runs_dir)]
        )
    )
    assert trend["capsule_count"] == 2
    assert sum(point["n"] for point in trend["series"]) == 2

    # 3d. nova session show: both turns resolve, in order.
    session = json.loads(_invoke(["session", "show", sid, "--json"]))
    members = session["members"]
    assert len(members) == 2
    assert [m["member"]["sequence"] for m in members] == [0, 1]
    assert all(m["status"] == "ok" for m in members)

    # 3e. nova diff --group-by variant labels the arms and exits 0.
    diff_out = _invoke(["diff", "--group-by", "variant", str(cap_a), str(cap_b)])
    assert "prompt-rollout/prompt-v1" in diff_out
    assert "prompt-rollout/prompt-v2" in diff_out
