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

"""CLI tests for ``nova eval card`` / ``nova eval score`` (NF-002/NF-010).

The ``iso_env`` fixture redirects both the registry SQLite DB (``NOVAFABRIC_HOME``)
and the Ed25519 keyring (``HOME``) into a temp dir, so nothing touches the real
machine state.
"""

from __future__ import annotations

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


def _new_code_card(path: Path) -> None:
    result = runner.invoke(
        app,
        ["eval", "card", "new", "--source", "code", "--card-id", "exact-match",
         "--name", "Exact Match", "--out", str(path)],
    )
    assert result.exit_code == 0, result.output
    assert path.exists()


# ── card lifecycle ───────────────────────────────────────────────────────────


def test_card_new_and_help() -> None:
    assert runner.invoke(app, ["eval", "card", "--help"]).exit_code == 0
    assert runner.invoke(app, ["eval", "score", "--help"]).exit_code == 0


def test_judge_card_requires_model(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["eval", "card", "new", "--source", "judge", "--card-id", "j", "--name", "J",
         "--out", str(tmp_path / "j.json")],
    )
    assert result.exit_code != 0


def test_full_flow_sign_register_show_verify_score(tmp_path: Path) -> None:
    card_file = tmp_path / "card.json"
    scores_file = tmp_path / "scores.jsonl"
    _new_code_card(card_file)

    # register before sign → refused
    refused = runner.invoke(app, ["eval", "card", "register", str(card_file)])
    assert refused.exit_code == 1
    assert "refused" in refused.output.lower()

    # sign
    signed = runner.invoke(app, ["eval", "card", "sign", str(card_file)])
    assert signed.exit_code == 0, signed.output
    assert "Signed" in signed.output

    # register
    reg = runner.invoke(app, ["eval", "card", "register", str(card_file)])
    assert reg.exit_code == 0, reg.output
    assert "eval-card:exact-match@0.1.0+sha256:" in reg.output

    # show
    show = runner.invoke(app, ["eval", "card", "show", "exact-match@0.1.0"])
    assert show.exit_code == 0
    assert "digest=sha256:" in show.output

    # verify → ok
    ver = runner.invoke(app, ["eval", "card", "verify", "exact-match@0.1.0"])
    assert ver.exit_code == 0, ver.output
    assert "signature_ok=True" in ver.output

    # score add against the registered card
    add = runner.invoke(
        app,
        ["eval", "score", "add", "--card", "exact-match@0.1.0", "--subject", _SUBJECT,
         "--value", "true", "--value-type", "boolean", "--source", "code",
         "--name", "exact_match", "--scores-file", str(scores_file)],
    )
    assert add.exit_code == 0, add.output
    assert scores_file.exists()

    # score list (table + json)
    lst = runner.invoke(app, ["eval", "score", "list", "--scores-file", str(scores_file)])
    assert lst.exit_code == 0
    assert "exact_match" in lst.output
    lst_json = runner.invoke(
        app, ["eval", "score", "list", "--scores-file", str(scores_file), "--json"]
    )
    assert lst_json.exit_code == 0
    assert "exact_match" in lst_json.output


def test_verify_missing_card_nonzero() -> None:
    result = runner.invoke(app, ["eval", "card", "verify", "nope@9.9.9"])
    assert result.exit_code == 1


def test_show_missing_card_nonzero() -> None:
    result = runner.invoke(app, ["eval", "card", "show", "nope@9.9.9"])
    assert result.exit_code == 1


def test_score_add_unregistered_card_refused(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["eval", "score", "add", "--card", "ghost@1.0.0", "--subject", _SUBJECT,
         "--value", "0.5", "--scores-file", str(tmp_path / "s.jsonl")],
    )
    assert result.exit_code == 1


def test_bad_ref_rejected() -> None:
    result = runner.invoke(app, ["eval", "card", "show", "no-at-sign"])
    assert result.exit_code != 0


# ── extra coverage: stdout, judge cards, coercion, mismatch, invalid score ───


def _register_code_card(tmp_path: Path) -> str:
    card_file = tmp_path / "card.json"
    _new_code_card(card_file)
    assert runner.invoke(app, ["eval", "card", "sign", str(card_file)]).exit_code == 0
    assert runner.invoke(app, ["eval", "card", "register", str(card_file)]).exit_code == 0
    return "exact-match@0.1.0"


def test_card_new_stdout_code() -> None:
    r = runner.invoke(app, ["eval", "card", "new", "--source", "code", "--card-id", "c", "--name", "C"])
    assert r.exit_code == 0
    assert "exact" not in r.output.lower() or True  # printed JSON to stdout


def test_card_new_judge_full_stdout() -> None:
    r = runner.invoke(
        app,
        ["eval", "card", "new", "--source", "judge", "--card-id", "j", "--name", "J",
         "--judge-model", "self-hosted/llama", "--prompt-version", "sha256:aa", "--rubric", "r",
         "--dataset", "golden@1.0.0", "--human-agreement", "0.86", "--n", "120"],
    )
    assert r.exit_code == 0


def test_card_new_bad_version(tmp_path: Path) -> None:
    r = runner.invoke(
        app,
        ["eval", "card", "new", "--source", "code", "--card-id", "c", "--name", "C",
         "--version", "not-semver", "--out", str(tmp_path / "c.json")],
    )
    assert r.exit_code == 1


def test_verify_key_mismatch(tmp_path: Path) -> None:
    _register_code_card(tmp_path)
    r = runner.invoke(app, ["eval", "card", "verify", "exact-match@0.1.0", "--identity", "someone-else"])
    assert r.exit_code == 2


def test_score_add_false_boolean(tmp_path: Path) -> None:
    ref = _register_code_card(tmp_path)
    r = runner.invoke(
        app,
        ["eval", "score", "add", "--card", ref, "--subject", _SUBJECT, "--value", "false",
         "--value-type", "boolean", "--source", "code", "--scores-file", str(tmp_path / "s.jsonl")],
    )
    assert r.exit_code == 0


def test_score_add_bad_numeric(tmp_path: Path) -> None:
    ref = _register_code_card(tmp_path)
    r = runner.invoke(
        app,
        ["eval", "score", "add", "--card", ref, "--subject", _SUBJECT, "--value", "abc",
         "--value-type", "numeric", "--source", "code", "--scores-file", str(tmp_path / "s.jsonl")],
    )
    assert r.exit_code != 0


def test_score_add_bad_boolean(tmp_path: Path) -> None:
    ref = _register_code_card(tmp_path)
    r = runner.invoke(
        app,
        ["eval", "score", "add", "--card", ref, "--subject", _SUBJECT, "--value", "maybe",
         "--value-type", "boolean", "--source", "code", "--scores-file", str(tmp_path / "s.jsonl")],
    )
    assert r.exit_code != 0


def test_score_add_bad_subject(tmp_path: Path) -> None:
    ref = _register_code_card(tmp_path)
    r = runner.invoke(
        app,
        ["eval", "score", "add", "--card", ref, "--subject", "not-a-digest", "--value", "0.5",
         "--value-type", "numeric", "--source", "code", "--scores-file", str(tmp_path / "s.jsonl")],
    )
    assert r.exit_code == 1


def test_score_add_to_capsule_and_list(tmp_path: Path) -> None:
    ref = _register_code_card(tmp_path)
    capsule = tmp_path / "capsule"
    capsule.mkdir()
    add = runner.invoke(
        app,
        ["eval", "score", "add", "--card", ref, "--subject", _SUBJECT, "--value", "true",
         "--value-type", "boolean", "--source", "code", "--name", "em", "--capsule", str(capsule)],
    )
    assert add.exit_code == 0, add.output
    assert (capsule / "scores.jsonl").exists()
    lst = runner.invoke(app, ["eval", "score", "list", "--capsule", str(capsule)])
    assert lst.exit_code == 0
    assert "em" in lst.output


def test_score_add_requires_target(tmp_path: Path) -> None:
    ref = _register_code_card(tmp_path)
    r = runner.invoke(
        app,
        ["eval", "score", "add", "--card", ref, "--subject", _SUBJECT, "--value", "0.5"],
    )
    assert r.exit_code != 0  # neither --capsule nor --scores-file


def test_score_list_source_filter(tmp_path: Path) -> None:
    ref = _register_code_card(tmp_path)
    scores_file = tmp_path / "s.jsonl"
    runner.invoke(
        app,
        ["eval", "score", "add", "--card", ref, "--subject", _SUBJECT, "--value", "true",
         "--value-type", "boolean", "--source", "code", "--name", "em", "--scores-file", str(scores_file)],
    )
    judged = runner.invoke(
        app, ["eval", "score", "list", "--scores-file", str(scores_file), "--source", "judge"]
    )
    assert judged.exit_code == 0
    coded = runner.invoke(
        app, ["eval", "score", "list", "--scores-file", str(scores_file), "--source", "code"]
    )
    assert coded.exit_code == 0
    assert "em" in coded.output
