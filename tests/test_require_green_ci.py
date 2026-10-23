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

"""The release gate (scripts/require_green_ci.py) and its wiring (ADR-0316).

v0.106.0 was published while CI was red on the tagged commit. The
gate must fail closed in every state except "CI finished green on this SHA", and
every tag-triggered publishing job must sit behind it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"

_spec = importlib.util.spec_from_file_location(
    "require_green_ci", REPO / "scripts" / "require_green_ci.py"
)
assert _spec and _spec.loader
gate = importlib.util.module_from_spec(_spec)
sys.modules["require_green_ci"] = gate
_spec.loader.exec_module(gate)


def _run(status: str, conclusion: str | None, created: str = "2026-10-22T10:00:00Z", rid: int = 1):
    return {
        "id": rid,
        "status": status,
        "conclusion": conclusion,
        "created_at": created,
        "html_url": f"https://example.invalid/runs/{rid}",
    }


# ---------------------------------------------------------------------------
# verdict: fail closed everywhere but success
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "conclusion",
    ["failure", "cancelled", "timed_out", "skipped", "neutral", "action_required", None],
)
def test_any_completed_conclusion_but_success_blocks(conclusion: str | None) -> None:
    v = gate.verdict([_run("completed", conclusion)], waited=0, grace=60)
    assert v.state == "fail"
    assert "runs/1" in v.message


def test_success_passes() -> None:
    assert gate.verdict([_run("completed", "success")], waited=0, grace=60).state == "pass"


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting", "requested", "pending"])
def test_unfinished_ci_waits(status: str) -> None:
    assert gate.verdict([_run(status, None)], waited=9999, grace=60).state == "wait"


def test_no_run_waits_out_the_grace_then_fails() -> None:
    assert gate.verdict([], waited=10, grace=60).state == "wait"
    v = gate.verdict([], waited=60, grace=60)
    assert v.state == "fail"
    assert "no CI run" in v.message
    assert "gh workflow run ci.yml --ref" in v.message


def test_ci_can_be_run_on_a_tag_that_was_not_a_pushed_head() -> None:
    """The gate's own remedy for "no CI run" must exist: push CI covers only the head."""
    doc = yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
    on = doc.get("on", doc.get(True))
    assert "workflow_dispatch" in on


def test_the_newest_run_decides() -> None:
    old_red = _run("completed", "failure", "2026-10-22T10:00:00Z", 1)
    new_green = _run("completed", "success", "2026-10-22T11:00:00Z", 2)
    assert gate.verdict([new_green, old_red], waited=0, grace=0).state == "pass"
    assert gate.verdict([old_red, new_green], waited=0, grace=0).state == "pass"
    new_red = _run("completed", "failure", "2026-10-22T12:00:00Z", 3)
    assert gate.verdict([new_green, new_red], waited=0, grace=0).state == "fail"


# ---------------------------------------------------------------------------
# polling loop
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_waits_until_ci_finishes_then_passes() -> None:
    answers = [
        [_run("in_progress", None)],
        [_run("in_progress", None)],
        [_run("completed", "success")],
    ]
    seen: list[str] = []

    def fetch(path: str) -> dict[str, Any]:
        seen.append(path)
        return {"workflow_runs": answers.pop(0)}

    clock = _Clock()
    v = gate.wait_for_green(
        fetch,
        "o/r",
        "abc",
        workflow="ci.yml",
        timeout=600,
        grace=60,
        interval=30,
        clock=clock,
        sleep=clock.sleep,
    )
    assert v.state == "pass"
    assert len(seen) == 3
    assert seen[0] == "/repos/o/r/actions/workflows/ci.yml/runs?head_sha=abc&per_page=50"


def test_gives_up_at_the_timeout() -> None:
    clock = _Clock()
    v = gate.wait_for_green(
        lambda _p: {"workflow_runs": [_run("in_progress", None)]},
        "o/r",
        "abc",
        workflow="ci.yml",
        timeout=90,
        grace=60,
        interval=30,
        clock=clock,
        sleep=clock.sleep,
    )
    assert v.state == "fail"
    assert "gave up after 90s" in v.message


def test_main_refuses_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert gate.main(["--repo", "o/r", "--sha", "abc"]) == 2


# ---------------------------------------------------------------------------
# wiring: every publishing workflow is behind the gate
# ---------------------------------------------------------------------------

_RELEASE_WORKFLOWS = ["release.yml", "publish-pypi.yml", "publish-image.yml", "publish-chart.yml"]
_GATE = "./.github/workflows/require-green-ci.yml"


def _jobs(name: str) -> dict[str, Any]:
    return dict(yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))["jobs"])


def test_the_tag_triggered_workflows_are_exactly_the_gated_set() -> None:
    """A new tag-triggered workflow must be added to the gated set deliberately."""
    tagged = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        on = doc.get("on", doc.get(True)) or {}  # PyYAML reads a bare `on:` as True
        tags = ((on.get("push") or {}) if isinstance(on, dict) else {}).get("tags")
        if tags:
            tagged.append(path.name)
    assert sorted(tagged) == sorted(_RELEASE_WORKFLOWS)


@pytest.mark.parametrize("workflow", _RELEASE_WORKFLOWS)
def test_every_job_in_a_release_workflow_needs_the_gate(workflow: str) -> None:
    jobs = _jobs(workflow)
    gates = [n for n, j in jobs.items() if j.get("uses") == _GATE]
    assert len(gates) == 1, f"{workflow} must call {_GATE} exactly once"
    gate_job = gates[0]

    def depends_on_gate(name: str, trail: tuple[str, ...] = ()) -> bool:
        needs = jobs[name].get("needs") or []
        needs = [needs] if isinstance(needs, str) else needs
        return gate_job in needs or any(
            depends_on_gate(n, (*trail, name)) for n in needs if n not in trail
        )

    ungated = [n for n in jobs if n != gate_job and not depends_on_gate(n)]
    assert not ungated, f"{workflow}: jobs {ungated} can run before CI is known green"


def test_the_gate_workflow_runs_the_script_with_read_only_actions_access() -> None:
    doc = yaml.safe_load((WORKFLOWS / "require-green-ci.yml").read_text(encoding="utf-8"))
    on = doc.get("on", doc.get(True))
    assert "workflow_call" in on
    (job,) = doc["jobs"].values()
    assert job["permissions"] == {"actions": "read", "contents": "read"}
    runs = " ".join(str(s.get("run", "")) for s in job["steps"])
    assert "scripts/require_green_ci.py" in runs
