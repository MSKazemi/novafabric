"""nova replay-equivalence — the C3 verdict surface (ADR-0144, consumed by ADR-0147 D3).

ADR-0147 D3 requires the canary scheduler and the impact report to *consume* C3 and
never re-implement it — "one verdict engine, many consumers". That only holds if the
CLI delegates, so `test_the_cli_delegates_to_the_engine` pins that it calls
`replay.equivalence.compare` rather than computing a verdict of its own.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

BASE = [{"name": "search", "arguments": {"q": "a"}},
        {"name": "db.query", "arguments": {"id": 1}}]
DIFF = [{"name": "search", "arguments": {"q": "a"}},
        {"name": "email.send", "arguments": {"to": "x"}}]


def _write(tmp_path: Path, name: str, calls: list) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(calls))
    return p


def _check(base: Path, replay: Path, *extra: str):
    return runner.invoke(app, ["replay-equivalence", "check",
                               "--baseline", str(base), "--replay", str(replay), *extra])


# ── the verdict ──────────────────────────────────────────────────────────────


def test_identical_trajectories_are_equivalent(tmp_path: Path) -> None:
    base = _write(tmp_path, "b.json", BASE)
    result = _check(base, _write(tmp_path, "r.json", BASE))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["equivalent"] is True
    assert payload["distance"] == 0.0
    assert payload["divergent_steps"] == []


def test_a_divergent_trajectory_exits_one_and_names_the_step(tmp_path: Path) -> None:
    """Non-equivalence is what a canary alarms on, so it is a non-zero exit."""
    result = _check(_write(tmp_path, "b.json", BASE),
                    _write(tmp_path, "r.json", DIFF))

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["equivalent"] is False
    step = payload["divergent_steps"][0]
    assert step["index"] == 1
    assert "db.query" in step["baseline"]
    assert "email.send" in step["replay"]


def test_tolerance_defaults_to_exact(tmp_path: Path) -> None:
    """Slack must be asked for, and the verdict records how much was allowed."""
    result = _check(_write(tmp_path, "b.json", BASE),
                    _write(tmp_path, "r.json", BASE))
    assert json.loads(result.stdout)["tolerance"] == 0.0


def test_tolerance_can_admit_a_divergence(tmp_path: Path) -> None:
    result = _check(_write(tmp_path, "b.json", BASE),
                    _write(tmp_path, "r.json", DIFF), "--tolerance", "0.9")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["equivalent"] is True
    assert payload["tolerance"] == 0.9
    assert payload["divergent_steps"], "tolerated divergences are still reported"


@pytest.mark.parametrize("mode", ["set", "ordered", "edit"])
def test_every_match_mode_is_accepted(mode: str, tmp_path: Path) -> None:
    result = _check(_write(tmp_path, "b.json", BASE),
                    _write(tmp_path, "r.json", BASE), "--mode", mode)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["mode"] == mode


# ── the verdict must stay interpretable ──────────────────────────────────────


def test_the_verdict_records_how_it_canonicalized(tmp_path: Path) -> None:
    """A verdict whose canonicalization is unknown cannot be re-derived later."""
    result = _check(_write(tmp_path, "b.json", BASE),
                    _write(tmp_path, "r.json", BASE))

    payload = json.loads(result.stdout)
    assert payload["rules_version"]
    assert payload["rules_applied"]


# ── it delegates; it does not re-implement ───────────────────────────────────


def test_the_cli_delegates_to_the_engine(tmp_path: Path, monkeypatch) -> None:
    """ADR-0147 D3: one verdict engine, many consumers."""
    called: dict[str, bool] = {}
    import novafabric.cli.replay_equivalence as mod

    real = mod.compare

    def spy(*args, **kwargs):
        called["yes"] = True
        return real(*args, **kwargs)

    monkeypatch.setattr(mod, "compare", spy)
    _check(_write(tmp_path, "b.json", BASE), _write(tmp_path, "r.json", BASE))

    assert called.get("yes"), "the CLI must call replay.equivalence.compare"


# ── error contract ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("content", "why"),
    [("{not json", "unparseable"),
     ('{"name": "x"}', "object, not an array"),
     ('[{"arguments": {}}]', "call without a name"),
     ('[{"name": "x", "arguments": "nope"}]', "arguments not an object")],
)
def test_a_malformed_trajectory_exits_two(
    content: str, why: str, tmp_path: Path
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(content)

    result = _check(bad, _write(tmp_path, "r.json", BASE))
    assert result.exit_code == 2, f"{why}: {result.output}"


def test_a_missing_file_exits_two(tmp_path: Path) -> None:
    result = _check(tmp_path / "nope.json", _write(tmp_path, "r.json", BASE))
    assert result.exit_code == 2


def test_an_unknown_mode_exits_two(tmp_path: Path) -> None:
    result = _check(_write(tmp_path, "b.json", BASE),
                    _write(tmp_path, "r.json", BASE), "--mode", "vibes")
    assert result.exit_code == 2


def test_a_negative_tolerance_exits_two(tmp_path: Path) -> None:
    result = _check(_write(tmp_path, "b.json", BASE),
                    _write(tmp_path, "r.json", BASE), "--tolerance", "-1")
    assert result.exit_code == 2


def test_an_unknown_rule_exits_two(tmp_path: Path) -> None:
    result = _check(_write(tmp_path, "b.json", BASE),
                    _write(tmp_path, "r.json", BASE), "--rule", "made_up_rule")
    assert result.exit_code == 2
