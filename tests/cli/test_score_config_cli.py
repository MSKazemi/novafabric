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

"""CLI tests for ``nova eval score config`` and ``nova eval score add --validate-scores``
(ADR-0117).

The ``iso_env`` fixture redirects the registry SQLite DB (``NOVAFABRIC_HOME``) and
the Ed25519 keyring (``HOME``) into a temp dir, so nothing touches real machine state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

_SUBJECT = "sha256:" + "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"


@pytest.fixture(autouse=True)
def iso_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "nfhome"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _add_numeric_toxicity() -> None:
    result = runner.invoke(
        app,
        ["eval", "score", "config", "add", "--name", "toxicity",
         "--value-type", "numeric", "--description", "Lower is better.",
         "--min", "0", "--max", "1", "--direction", "lower-better"],
    )
    assert result.exit_code == 0, result.output


def _registered_card(tmp_path: Path) -> str:
    card_file = tmp_path / "card.json"
    new = runner.invoke(
        app,
        ["eval", "card", "new", "--source", "code", "--card-id", "tox-scan",
         "--name", "Toxicity Scan", "--out", str(card_file)],
    )
    assert new.exit_code == 0, new.output
    assert runner.invoke(app, ["eval", "card", "sign", str(card_file)]).exit_code == 0
    assert runner.invoke(app, ["eval", "card", "register", str(card_file)]).exit_code == 0
    return "tox-scan@0.1.0"


def _score_add_args(card: str, scores_file: Path, value: str, *extra: str) -> list[str]:
    return [
        "eval", "score", "add", "--card", card, "--subject", _SUBJECT,
        "--value", value, "--value-type", "numeric", "--source", "code",
        "--name", "toxicity", "--scores-file", str(scores_file), *extra,
    ]


# ── help smoke ───────────────────────────────────────────────────────────────


def test_help_surfaces() -> None:
    assert runner.invoke(app, ["eval", "score", "config", "--help"]).exit_code == 0
    for sub in ("add", "list", "get", "show"):
        assert runner.invoke(app, ["eval", "score", "config", sub, "--help"]).exit_code == 0


# ── config add ───────────────────────────────────────────────────────────────


def test_add_numeric_emits_id_version_digest() -> None:
    result = runner.invoke(
        app,
        ["eval", "score", "config", "add", "--name", "toxicity",
         "--value-type", "numeric", "--description", "d", "--min", "0", "--max", "1"],
    )
    assert result.exit_code == 0, result.output
    assert "toxicity@1" in result.output
    assert "sha256:" in result.output


def test_add_categorical_with_ordinals_and_boolean() -> None:
    cat = runner.invoke(
        app,
        ["eval", "score", "config", "add", "--name", "helpfulness",
         "--value-type", "categorical", "--description", "d",
         "--category", "bad:0", "--category", "ok:1", "--category", "good:2"],
    )
    assert cat.exit_code == 0, cat.output
    boolean = runner.invoke(
        app,
        ["eval", "score", "config", "add", "--name", "grounded",
         "--value-type", "boolean", "--description", "d"],
    )
    assert boolean.exit_code == 0, boolean.output


def test_add_identical_body_is_noop() -> None:
    _add_numeric_toxicity()
    result = runner.invoke(
        app,
        ["eval", "score", "config", "add", "--name", "toxicity",
         "--value-type", "numeric", "--description", "Lower is better.",
         "--min", "0", "--max", "1", "--direction", "lower-better"],
    )
    assert result.exit_code == 0, result.output
    assert "toxicity@1" in result.output
    assert "already registered" in result.output.lower()


def test_add_changed_body_bumps_version() -> None:
    _add_numeric_toxicity()
    result = runner.invoke(
        app,
        ["eval", "score", "config", "add", "--name", "toxicity",
         "--value-type", "numeric", "--description", "Redefined.",
         "--min", "0", "--max", "1"],
    )
    assert result.exit_code == 0, result.output
    assert "toxicity@2" in result.output


@pytest.mark.parametrize(
    "args",
    [
        # categorical without categories
        ["--name", "x", "--value-type", "categorical", "--description", "d"],
        # numeric without a range
        ["--name", "x", "--value-type", "numeric", "--description", "d"],
        # boolean with a category
        ["--name", "x", "--value-type", "boolean", "--description", "d",
         "--category", "yes"],
        # numeric with a category
        ["--name", "x", "--value-type", "numeric", "--description", "d",
         "--min", "0", "--max", "1", "--category", "y"],
        # bad ordinal syntax
        ["--name", "x", "--value-type", "categorical", "--description", "d",
         "--category", "ok:high"],
        # min > max
        ["--name", "x", "--value-type", "numeric", "--description", "d",
         "--min", "2", "--max", "1"],
        # --min without --max
        ["--name", "x", "--value-type", "numeric", "--description", "d", "--min", "0"],
    ],
    ids=["cat-missing", "num-missing", "bool-extra", "num-extra", "bad-ordinal",
         "min-gt-max", "min-only"],
)
def test_add_invalid_shapes_refused(args: list[str]) -> None:
    result = runner.invoke(app, ["eval", "score", "config", "add", *args])
    assert result.exit_code != 0


# ── config list / get / show ─────────────────────────────────────────────────


def test_list_latest_and_all() -> None:
    _add_numeric_toxicity()
    runner.invoke(
        app,
        ["eval", "score", "config", "add", "--name", "toxicity",
         "--value-type", "numeric", "--description", "v2", "--min", "0", "--max", "1"],
    )
    latest = runner.invoke(app, ["eval", "score", "config", "list", "--json"])
    assert latest.exit_code == 0, latest.output
    rows = json.loads(latest.stdout[latest.stdout.index("[") :])
    assert [r["version"] for r in rows if r["name"] == "toxicity"] == [2]
    everything = runner.invoke(app, ["eval", "score", "config", "list", "--all", "--json"])
    rows = json.loads(everything.stdout[everything.stdout.index("[") :])
    assert {r["version"] for r in rows if r["name"] == "toxicity"} == {1, 2}


def test_list_table_output() -> None:
    _add_numeric_toxicity()
    result = runner.invoke(app, ["eval", "score", "config", "list"])
    assert result.exit_code == 0
    assert "toxicity" in result.output


def test_get_by_name_version_and_digest() -> None:
    _add_numeric_toxicity()
    by_ref = runner.invoke(app, ["eval", "score", "config", "get", "toxicity@1"])
    assert by_ref.exit_code == 0, by_ref.output
    doc = json.loads(by_ref.stdout[by_ref.stdout.index("{") :])
    assert doc["name"] == "toxicity"
    by_digest = runner.invoke(app, ["eval", "score", "config", "get", doc["content_digest"]])
    assert by_digest.exit_code == 0
    missing = runner.invoke(app, ["eval", "score", "config", "get", "nope"])
    assert missing.exit_code == 1


def test_show_version_history() -> None:
    _add_numeric_toxicity()
    runner.invoke(
        app,
        ["eval", "score", "config", "add", "--name", "toxicity",
         "--value-type", "numeric", "--description", "v2", "--min", "0", "--max", "1"],
    )
    result = runner.invoke(app, ["eval", "score", "config", "show", "toxicity"])
    assert result.exit_code == 0, result.output
    assert "toxicity" in result.output
    missing = runner.invoke(app, ["eval", "score", "config", "show", "nope"])
    assert missing.exit_code == 1


def test_show_prints_labels() -> None:
    added = runner.invoke(
        app,
        ["eval", "score", "config", "add", "--name", "grounded",
         "--value-type", "boolean", "--description", "d",
         "--label", "safety", "--label", "rag"],
    )
    assert added.exit_code == 0, added.output
    result = runner.invoke(app, ["eval", "score", "config", "show", "grounded"])
    assert result.exit_code == 0, result.output
    assert "safety" in result.output


# ── score add --validate-scores wiring (D2; default off) ─────────────────────


def test_validate_scores_accepts_in_range(tmp_path: Path) -> None:
    card = _registered_card(tmp_path)
    _add_numeric_toxicity()
    scores_file = tmp_path / "scores.jsonl"
    result = runner.invoke(
        app, _score_add_args(card, scores_file, "0.3", "--validate-scores")
    )
    assert result.exit_code == 0, result.output
    assert len(scores_file.read_text().splitlines()) == 1


def test_validate_scores_refuses_out_of_range_and_does_not_append(tmp_path: Path) -> None:
    card = _registered_card(tmp_path)
    _add_numeric_toxicity()
    scores_file = tmp_path / "scores.jsonl"
    result = runner.invoke(
        app, _score_add_args(card, scores_file, "1.5", "--validate-scores")
    )
    assert result.exit_code == 1
    assert not scores_file.exists()


def test_validation_defaults_off_out_of_range_still_appends(tmp_path: Path) -> None:
    card = _registered_card(tmp_path)
    _add_numeric_toxicity()
    scores_file = tmp_path / "scores.jsonl"
    result = runner.invoke(app, _score_add_args(card, scores_file, "1.5"))
    assert result.exit_code == 0, result.output
    assert len(scores_file.read_text().splitlines()) == 1


def test_validate_scores_with_no_config_appends_free_score(tmp_path: Path) -> None:
    card = _registered_card(tmp_path)
    scores_file = tmp_path / "scores.jsonl"
    result = runner.invoke(
        app, _score_add_args(card, scores_file, "0.3", "--validate-scores")
    )
    assert result.exit_code == 0, result.output
    assert len(scores_file.read_text().splitlines()) == 1
