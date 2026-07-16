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

"""CLI tests for ``nova annotate`` (ADR-0118).

The ``iso_env`` fixture redirects the registry SQLite DB (``NOVAFABRIC_HOME``)
and the Ed25519 keyring into a temp dir, so nothing touches real machine state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import novafabric.trust.keyring as kr
from novafabric.cli.main import app
from novafabric.eval.scores import SCORES_FILENAME, read_scores

runner = CliRunner()

_SUBJECT = "sha256:" + "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"


@pytest.fixture(autouse=True)
def iso_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "nfhome"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(kr, "_KEYRING_DIR", tmp_path / "keyring")


@pytest.fixture()
def capsule(tmp_path: Path) -> Path:
    cap = tmp_path / "capsule"
    cap.mkdir()
    (cap / "capsule.yaml").write_text("run_id: 01HXAY7M5JZ8R7K4P9DPBYK2WX\n")
    return cap


def _register_config(name: str = "factuality") -> None:
    result = runner.invoke(
        app,
        ["eval", "score", "config", "add", "--name", name,
         "--value-type", "boolean", "--description", "Is it factual?"],
    )
    assert result.exit_code == 0, result.output


def _create_queue(*extra: str, criteria: str = "factuality") -> None:
    result = runner.invoke(
        app,
        ["annotate", "queue", "create", "--name", "q1", "--criteria", criteria, *extra],
    )
    assert result.exit_code == 0, result.output


def _add_item(capsule: Path) -> str:
    result = runner.invoke(
        app,
        ["annotate", "queue", "add", "q1", "--capsule", str(capsule), "--json"],
    )
    assert result.exit_code == 0, result.output
    item_id: str = json.loads(result.output)["item_id"]
    return item_id


def _claim(reviewer: str) -> str:
    result = runner.invoke(app, ["annotate", "next", "--as", reviewer, "--json"])
    assert result.exit_code == 0, result.output
    item_id: str = json.loads(result.output)["item_id"]
    return item_id


# ── help smoke ────────────────────────────────────────────────────────────────


def test_help_surfaces() -> None:
    assert runner.invoke(app, ["annotate", "--help"]).exit_code == 0
    for sub in ("next", "submit", "confirm", "skip"):
        assert runner.invoke(app, ["annotate", sub, "--help"]).exit_code == 0
    for sub in ("create", "add", "list", "show"):
        assert runner.invoke(app, ["annotate", "queue", sub, "--help"]).exit_code == 0


# ── queue lifecycle ───────────────────────────────────────────────────────────


def test_queue_create_requires_registered_config() -> None:
    result = runner.invoke(
        app, ["annotate", "queue", "create", "--name", "q1", "--criteria", "ghost"]
    )
    assert result.exit_code == 1
    assert "no registered score config" in result.output


def test_queue_create_list_show(capsule: Path) -> None:
    _register_config()
    _create_queue()
    _add_item(capsule)

    listed = runner.invoke(app, ["annotate", "queue", "list", "--json"])
    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.output)
    assert len(payload) == 1
    assert payload[0]["name"] == "q1"
    assert payload[0]["progress"]["pending"] == 1

    shown = runner.invoke(app, ["annotate", "queue", "show", "q1"])
    assert shown.exit_code == 0, shown.output
    assert "pending=1" in shown.output

    table = runner.invoke(app, ["annotate", "queue", "list"])
    assert table.exit_code == 0 and "q1" in table.output


def test_queue_create_duplicate_name_exits_1() -> None:
    _register_config()
    _create_queue()
    result = runner.invoke(
        app, ["annotate", "queue", "create", "--name", "q1", "--criteria", "factuality"]
    )
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_queue_show_unknown_exits_1() -> None:
    result = runner.invoke(app, ["annotate", "queue", "show", "ghost"])
    assert result.exit_code == 1


def test_queue_add_selector_guard(capsule: Path) -> None:
    _register_config()
    _create_queue("--select", "subject_kind=span")
    # Default kind for a bare --capsule add is 'capsule' — refused by the guard.
    result = runner.invoke(
        app, ["annotate", "queue", "add", "q1", "--capsule", str(capsule)]
    )
    assert result.exit_code == 1
    assert "subject_kind" in result.output
    # An explicit span subject passes.
    ok = runner.invoke(
        app,
        ["annotate", "queue", "add", "q1", "--capsule", str(capsule),
         "--subject", _SUBJECT],
    )
    assert ok.exit_code == 0, ok.output


def test_queue_add_bad_selector_pair() -> None:
    _register_config()
    result = runner.invoke(
        app,
        ["annotate", "queue", "create", "--name", "q1", "--criteria", "factuality",
         "--select", "malformed"],
    )
    assert result.exit_code != 0


# ── claim / submit round-trip ─────────────────────────────────────────────────


def test_next_on_empty_queue_is_not_an_error() -> None:
    _register_config()
    _create_queue()
    result = runner.invoke(app, ["annotate", "next", "--queue", "q1", "--as", "rev:a"])
    assert result.exit_code == 0
    assert "Queue empty" in result.output


def test_full_single_reviewer_round_trip(capsule: Path) -> None:
    _register_config()
    _create_queue()
    _add_item(capsule)

    claimed = runner.invoke(
        app, ["annotate", "next", "--queue", "q1", "--as", "rev:a", "--json"]
    )
    assert claimed.exit_code == 0, claimed.output
    item = json.loads(claimed.output)
    assert item["state"] == "assigned" and item["assignee"] == "rev:a"

    submitted = runner.invoke(
        app,
        ["annotate", "submit", item["item_id"], "--score", "factuality=true", "--json"],
    )
    assert submitted.exit_code == 0, submitted.output
    updated = json.loads(submitted.output)
    assert updated["state"] == "completed"
    assert len(updated["resulting_score_ids"]) == 1

    scores = read_scores(capsule / SCORES_FILENAME)
    assert len(scores) == 1
    assert scores[0].source.value == "human"
    assert scores[0].evaluator_id == "rev:a"
    assert scores[0].value is True


def test_submit_rejects_invalid_value(capsule: Path) -> None:
    _register_config()
    _create_queue()
    _add_item(capsule)
    item_id = _claim("rev:a")
    result = runner.invoke(
        app, ["annotate", "submit", item_id, "--score", "factuality=maybe"]
    )
    assert result.exit_code == 1
    assert "expects a boolean" in result.output
    assert not (capsule / SCORES_FILENAME).exists()


def test_submit_bad_score_pair_is_a_usage_error(capsule: Path) -> None:
    _register_config()
    _create_queue()
    _add_item(capsule)
    item_id = _claim("rev:a")
    result = runner.invoke(app, ["annotate", "submit", item_id, "--score", "nopair"])
    assert result.exit_code != 0


def test_maker_checker_cli_flow(capsule: Path) -> None:
    _register_config()
    _create_queue("--require-checker")
    _add_item(capsule)
    item_id = _claim("rev:maker")

    submitted = runner.invoke(
        app, ["annotate", "submit", item_id, "--score", "factuality=true", "--json"]
    )
    assert submitted.exit_code == 0, submitted.output
    assert json.loads(submitted.output)["state"] == "checker_pending"

    # Self-confirmation is refused (SoD).
    veto = runner.invoke(app, ["annotate", "confirm", item_id, "--as", "rev:maker"])
    assert veto.exit_code == 1
    assert "checker equals the maker" in veto.output

    confirmed = runner.invoke(
        app, ["annotate", "confirm", item_id, "--as", "rev:checker", "--json"]
    )
    assert confirmed.exit_code == 0, confirmed.output
    final = json.loads(confirmed.output)
    assert final["state"] == "completed" and final["checker"] == "rev:checker"
    assert "io.novafabric.annotation.checker_signature" in final["extensions"]


def test_skip_cli(capsule: Path) -> None:
    _register_config()
    _create_queue()
    item_id = _add_item(capsule)
    result = runner.invoke(app, ["annotate", "skip", item_id, "--note", "n/a"])
    assert result.exit_code == 0
    assert "Skipped" in result.output
    again = runner.invoke(app, ["annotate", "skip", item_id])
    assert again.exit_code == 1


def test_next_prints_item_and_hint(capsule: Path) -> None:
    _register_config()
    _create_queue()
    _add_item(capsule)
    result = runner.invoke(app, ["annotate", "next", "--as", "rev:a"])
    assert result.exit_code == 0, result.output
    assert "Claimed" in result.output and "assignee: rev:a" in result.output
    assert "nova annotate submit" in result.output


def test_next_claims_named_item(capsule: Path) -> None:
    _register_config()
    _create_queue("--policy", "manual")
    item_id = _add_item(capsule)
    result = runner.invoke(
        app, ["annotate", "next", "--item", item_id, "--as", "rev:a", "--json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["state"] == "assigned"


def test_create_with_selector_lists_and_sample() -> None:
    _register_config()
    created = runner.invoke(
        app,
        ["annotate", "queue", "create", "--name", "q1", "--criteria", "factuality",
         "--select", "sample=0.5", "--select", "tags=a,b",
         "--select", "subject_kind=span", "--json"],
    )
    assert created.exit_code == 0, created.output
    selector = json.loads(created.output)["subject_selector"]
    assert selector == {"subject_kind": "span", "tags": ["a", "b"], "sample": 0.5}
    bad = runner.invoke(
        app,
        ["annotate", "queue", "create", "--name", "q2", "--criteria", "factuality",
         "--select", "sample=lots"],
    )
    assert bad.exit_code != 0


def test_submit_duplicate_score_pair_is_a_usage_error(capsule: Path) -> None:
    _register_config()
    _create_queue()
    _add_item(capsule)
    item_id = _claim("rev:a")
    result = runner.invoke(
        app,
        ["annotate", "submit", item_id,
         "--score", "factuality=true", "--score", "factuality=false"],
    )
    assert result.exit_code != 0


def test_submit_by_wrong_reviewer_exits_1(capsule: Path) -> None:
    _register_config()
    _create_queue()
    _add_item(capsule)
    item_id = _claim("rev:a")
    result = runner.invoke(
        app,
        ["annotate", "submit", item_id, "--score", "factuality=true", "--as", "rev:b"],
    )
    assert result.exit_code == 1
    assert "assignee may submit" in result.output


def test_submit_human_output_shows_checker_hint(capsule: Path) -> None:
    _register_config()
    _create_queue("--require-checker")
    _add_item(capsule)
    item_id = _claim("rev:maker")
    result = runner.invoke(
        app, ["annotate", "submit", item_id, "--score", "factuality=true"]
    )
    assert result.exit_code == 0, result.output
    assert "Awaiting checker" in result.output
    confirmed = runner.invoke(app, ["annotate", "confirm", item_id, "--as", "rev:b"])
    assert confirmed.exit_code == 0, confirmed.output
    assert "Confirmed" in confirmed.output


def test_queue_show_json_includes_items(capsule: Path) -> None:
    _register_config()
    _create_queue()
    _add_item(capsule)
    shown = runner.invoke(app, ["annotate", "queue", "show", "q1", "--json"])
    assert shown.exit_code == 0, shown.output
    payload = json.loads(shown.output)
    assert payload["name"] == "q1"
    assert len(payload["items"]) == 1
    assert payload["progress"]["pending"] == 1


def test_seal_queue_prints_planned_note(capsule: Path) -> None:
    _register_config()
    created = runner.invoke(
        app,
        ["annotate", "queue", "create", "--name", "q1", "--criteria", "factuality",
         "--seal"],
    )
    assert created.exit_code == 0
    assert "planned" in created.output
    _add_item(capsule)
    item_id = _claim("rev:a")
    submitted = runner.invoke(
        app, ["annotate", "submit", item_id, "--score", "factuality=true"]
    )
    assert submitted.exit_code == 0, submitted.output
    assert "planned" in submitted.output
