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

"""CLI smoke + failure paths for ADR-0150 P3: ``nova hitl handoff list``,
``nova hitl acted-as`` and ``nova consent record|show|verify``."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from rich.console import Console
from typer.testing import CliRunner

from novafabric.cli import consent as consent_cli
from novafabric.cli import hitl as hitl_cli
from novafabric.cli.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "conversation"
SCHEMA = REPO_ROOT / "schemas" / "run-capsule.schema.json"
OK_DOC = FIXTURES / "nf084-delegation-established.json"
BROKEN_DOC = FIXTURES / "nf084-delegation-broken.json"
runner = CliRunner()


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hitl_cli, "console", Console(width=250))
    monkeypatch.setattr(consent_cli, "console", Console(width=250))


def _capsule_dir(tmp_path: Path, fixture: str, mutate: Any = None) -> Path:
    data = json.loads((FIXTURES / fixture).read_text())
    if mutate is not None:
        mutate(data)
    capsule = tmp_path / "cap"
    capsule.mkdir(parents=True)
    (capsule / "capsule.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    return capsule


def _run(*args: str) -> Any:
    return runner.invoke(app, list(args))


def _squash(text: str) -> str:
    return " ".join(text.split())


def _json(result: Any) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(result.stdout)
    return data


@pytest.mark.parametrize(
    "path",
    [
        ["hitl", "handoff"],
        ["hitl", "handoff", "list"],
        ["hitl", "acted-as"],
        ["consent"],
        ["consent", "record"],
        ["consent", "show"],
        ["consent", "verify"],
    ],
)
def test_help(path: list[str]) -> None:
    result = _run(*path, "--help")
    assert result.exit_code == 0
    assert "Usage" in result.stdout


# ── nova hitl handoff list ────────────────────────────────────────────────


def test_handoff_list_ok_json(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, "p3-accountability-capsule.json")
    result = _run("hitl", "handoff", "list", "--capsule", str(cap), "--json")
    assert result.exit_code == 0, result.output
    data = _json(result)
    assert [h["signature_check"]["signature"] for h in data["handoffs"]] == [
        "valid",
        "reference_only",
    ]
    assert "Record-only" in data["notice"]


def test_handoff_list_text(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, "p3-accountability-capsule.json")
    result = _run("hitl", "handoff", "list", "--capsule", str(cap))
    assert result.exit_code == 0
    out = _squash(result.stdout)
    assert "Handoffs: 2" in out and "fingerprint_match" in out and "reference_only" in out


def test_handoff_list_invalid_fixture_exits_1(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, "invalid-handoff-capsule.json")
    result = _run("hitl", "handoff", "list", "--capsule", str(cap))
    assert result.exit_code == 1
    out = _squash(result.stdout)
    assert "invalid" in out and "SelfHandoffError" in out


def test_handoff_list_dangling_exits_1(tmp_path: Path) -> None:
    def mutate(d: dict[str, Any]) -> None:
        d["facets"]["conversation"]["handoff"][1]["turn_ref"] = "t404"

    cap = _capsule_dir(tmp_path, "p3-accountability-capsule.json", mutate)
    result = _run("hitl", "handoff", "list", "--capsule", str(cap))
    assert result.exit_code == 1
    assert "DANGLING" in result.stdout


def test_handoff_list_empty_ok(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, "threaded-capsule.json")
    result = _run("hitl", "handoff", "list", "--capsule", str(cap), "--json")
    assert result.exit_code == 0
    assert _json(result)["handoffs"] == []


# ── nova hitl acted-as ────────────────────────────────────────────────────


def test_acted_as_established(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, "p3-accountability-capsule.json")
    result = _run(
        "hitl", "acted-as", "--capsule", str(cap), "--turn", "t1", "--delegation", str(OK_DOC)
    )
    assert result.exit_code == 0, result.output
    out = _squash(result.stdout)
    assert "established" in out and "not re-verified or re-derived" in out
    assert "as recorded by document, not re-verified" in out
    assert "principal_match=chain_root" in out


def test_acted_as_broken_hop_surfaced_nonzero(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, "p3-accountability-capsule.json")
    result = _run(
        "hitl",
        "acted-as",
        "--capsule",
        str(cap),
        "--turn",
        "t1",
        "--delegation",
        str(BROKEN_DOC),
        "--json",
    )
    assert result.exit_code == 1
    data = _json(result)
    state = data["bindings"][0]["nf084_hop_state"]
    assert state["state"] == "broken" and state["broken_hop"] == 1
    assert data["ok"] is False


def test_acted_as_principal_mismatch_nonzero(tmp_path: Path) -> None:
    """Reviewer PoC: alice bound to a grant whose granter is bob must not exit 0."""
    doc = json.loads(OK_DOC.read_text())
    doc["chain"][0]["granter"] = "user:did:example:bob"
    path = tmp_path / "bob.json"
    path.write_text(json.dumps(doc))
    cap = _capsule_dir(tmp_path, "p3-accountability-capsule.json")
    args = ["hitl", "acted-as", "--capsule", str(cap), "--turn", "t1", "--delegation", str(path)]
    result = _run(*args)
    assert result.exit_code == 1
    assert "principal_mismatch" in _squash(result.stdout)
    data = _json(_run(*args, "--json"))
    state = data["bindings"][0]["nf084_hop_state"]
    assert state["state"] == "principal_mismatch" and state["principal_match"] == "none"
    assert state["walk_ok"] is True and state["reverified"] is False
    assert data["ok"] is False
    assert "not re-verified" in data["nf084_state_source"]


def test_acted_as_without_document_is_absent(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, "p3-accountability-capsule.json")
    result = _run("hitl", "acted-as", "--capsule", str(cap), "--turn", "t1", "--json")
    assert result.exit_code == 1
    data = _json(result)
    assert data["nf084_document"] is False
    assert data["bindings"][0]["nf084_hop_state"]["state"] == "absent"


def test_acted_as_reads_capsule_delegation_facet(tmp_path: Path) -> None:
    doc = json.loads(OK_DOC.read_text())

    def mutate(d: dict[str, Any]) -> None:
        d["facets"]["delegation"] = doc

    cap = _capsule_dir(tmp_path, "p3-accountability-capsule.json", mutate)
    result = _run("hitl", "acted-as", "--capsule", str(cap), "--turn", "t1")
    assert result.exit_code == 0, result.output


def test_acted_as_wrapped_document(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, "p3-accountability-capsule.json")
    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(json.dumps({"facets": {"delegation": json.loads(OK_DOC.read_text())}}))
    result = _run(
        "hitl", "acted-as", "--capsule", str(cap), "--turn", "t1", "--delegation", str(wrapped)
    )
    assert result.exit_code == 0, result.output


def test_acted_as_dangling_turn_and_no_binding(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, "p3-accountability-capsule.json")
    result = _run("hitl", "acted-as", "--capsule", str(cap), "--turn", "t9")
    assert result.exit_code == 1
    out = _squash(result.stdout)
    assert "Dangling turn_ref" in out and "No acted-on-behalf binding" in out


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, "not a regular file"),
        ("[1, 2]", "not a JSON object"),
        ("{not json", "cannot read --delegation"),
        ("OVERSIZE", "byte limit"),
    ],
)
def test_acted_as_bad_delegation_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str | None, message: str
) -> None:
    cap = _capsule_dir(tmp_path, "p3-accountability-capsule.json")
    path = tmp_path / "d.json"
    if content == "OVERSIZE":
        monkeypatch.setattr(hitl_cli, "MAX_DELEGATION_BYTES", 8)
        path.write_text(OK_DOC.read_text())
    elif content is not None:
        path.write_text(content)
    result = _run(
        "hitl", "acted-as", "--capsule", str(cap), "--turn", "t1", "--delegation", str(path)
    )
    assert result.exit_code == 1
    assert message in _squash(result.output)


# ── nova consent ──────────────────────────────────────────────────────────


def _record(cap: Path, *extra: str) -> Any:
    return _run(
        "consent",
        "record",
        "--capsule",
        str(cap),
        "--subject",
        "human:did:example:bob",
        "--purpose",
        "dpv:ServiceProvision",
        "--scope",
        "dpv:Store",
        "--scope",
        "dpv:Analyse",
        "--given-at",
        "2026-07-16T00:00:00Z",
        *extra,
    )


def test_consent_record_writes_schema_valid_capsule(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, "valid-text-only-capsule.json")
    result = _record(cap, "--expiry", "2027-01-01T00:00:00Z")
    assert result.exit_code == 0, result.output
    assert "re-issue any seal" in _squash(result.stdout)
    data = yaml.safe_load((cap / "capsule.yaml").read_text())
    jsonschema.validate(data, json.loads(SCHEMA.read_text()))
    (receipt,) = data["facets"]["conversation"]["consent"]
    assert receipt["action"] == ["dpv:Store", "dpv:Analyse"]
    assert receipt["consent_id"].startswith("consent-")
    assert not list(cap.glob(".capsule.yaml.*"))
    verify = _run("consent", "verify", "--capsule", str(cap), "--json")
    assert verify.exit_code == 0 and _json(verify)["status"] == "ok"


def test_consent_record_is_deterministic_and_refuses_duplicate(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, "threaded-capsule.json")
    assert _record(cap).exit_code == 0
    again = _record(cap)
    assert again.exit_code == 1
    assert "consent_id already recorded" in _squash(again.output)


def test_consent_record_dry_run_json_does_not_write(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, "threaded-capsule.json")
    before = (cap / "capsule.yaml").read_bytes()
    result = _record(cap, "--dry-run", "--json", "--consent-id", "c-7", "--turn", "t0")
    assert result.exit_code == 0, result.output
    data = _json(result)
    assert data["written"] is False and data["receipt"]["consent_id"] == "c-7"
    assert "legally valid" in data["notice"]
    assert (cap / "capsule.yaml").read_bytes() == before
    text = _record(cap, "--dry-run")
    assert "Would record" in text.stdout


def test_consent_record_not_withdrawable(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, "threaded-capsule.json")
    result = _record(cap, "--not-withdrawable", "--json")
    assert result.exit_code == 0
    assert _json(result)["receipt"]["withdrawable"] is False


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (("--turn", "t404"), "turn_ref does not resolve"),
        (("--expiry", "2020-01-01T00:00:00Z"), "expiry must be later"),
        (("--consent-id", "has space"), "consent_id must be"),
        (("--given-at", "yesterday"), "ISO-8601"),
    ],
)
def test_consent_record_refused(tmp_path: Path, extra: tuple[str, ...], message: str) -> None:
    cap = _capsule_dir(tmp_path, "threaded-capsule.json")
    result = _record(cap, *extra)
    assert result.exit_code == 1
    assert message in _squash(result.output)


def test_consent_record_refuses_email_subject(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, "threaded-capsule.json")
    result = _run(
        "consent",
        "record",
        "--capsule",
        str(cap),
        "--subject",
        "human:bob@example.com",
        "--purpose",
        "dpv:X",
        "--scope",
        "dpv:Y",
    )
    assert result.exit_code == 1
    assert "email" in _squash(result.output)


def test_consent_record_pydantic_error_not_echoed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = _capsule_dir(tmp_path, "threaded-capsule.json")

    def boom(**_: Any) -> None:
        raise ValueError("secret-looking detail")

    monkeypatch.setattr(consent_cli, "build_consent_receipt", boom)
    result = _record(cap)
    assert result.exit_code == 1
    assert "secret-looking" not in result.output
    assert "ValueError" in result.output


def test_consent_record_refuses_symlinked_manifest(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, "threaded-capsule.json")
    real = tmp_path / "elsewhere.yaml"
    (cap / "capsule.yaml").rename(real)
    (cap / "capsule.yaml").symlink_to(real)
    result = _record(cap)
    assert result.exit_code == 1
    assert "symlink" in result.output


def test_consent_record_write_failure_cleans_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = _capsule_dir(tmp_path, "threaded-capsule.json")

    def fail(*_: Any) -> None:
        raise OSError("disk full")

    before = (cap / "capsule.yaml").read_bytes()
    monkeypatch.setattr(os, "replace", fail)
    result = _record(cap)
    assert result.exit_code == 1
    assert "cannot write capsule.yaml" in result.output
    assert not list(cap.glob(".capsule.yaml.*"))
    assert (cap / "capsule.yaml").read_bytes() == before


def test_consent_record_refuses_sealed_capsule(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, "threaded-capsule.json")
    (cap / ".seal").mkdir()
    before = (cap / "capsule.yaml").read_bytes()
    result = _record(cap)
    assert result.exit_code == 1
    assert "--force-unseal" in _squash(result.output)
    assert (cap / "capsule.yaml").read_bytes() == before
    forced = _record(cap, "--force-unseal")
    assert forced.exit_code == 0, forced.output
    assert "no longer matches" in _squash(forced.output)
    assert (cap / "capsule.yaml").read_bytes() != before


def test_consent_missing_capsule(tmp_path: Path) -> None:
    result = _run("consent", "show", "--capsule", str(tmp_path / "nope"))
    assert result.exit_code == 1


def test_consent_show(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, "p3-accountability-capsule.json")
    result = _run("consent", "show", "--capsule", str(cap))
    assert result.exit_code == 0
    out = _squash(result.stdout)
    assert "consent-0001" in out and "does not assert" in out
    data = _json(_run("consent", "show", "--capsule", str(cap), "--json"))
    assert data["consents"][0]["purpose"] == "dpv:ServiceProvision"


def test_consent_show_withdrawn_and_defect(tmp_path: Path) -> None:
    def mutate(d: dict[str, Any]) -> None:
        block = d["facets"]["conversation"]["consent"]
        block[0]["withdrawn_at"] = "2026-08-01T00:00:00Z"
        block.append({"consent_id": 1})

    cap = _capsule_dir(tmp_path, "p3-accountability-capsule.json", mutate)
    result = _run("consent", "show", "--capsule", str(cap))
    assert result.exit_code == 1
    out = _squash(result.stdout)
    assert "withdrawn_at=2026-08-01T00:00:00Z" in out and "DEFECT" in out


def test_consent_verify_tampered(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, "invalid-tampered-consent-capsule.json")
    result = _run("consent", "verify", "--capsule", str(cap))
    assert result.exit_code == 1
    out = _squash(result.stdout)
    assert "DEFECTIVE" in out and "digest_matches=False" in out and "legally valid" in out


def test_consent_verify_json_reports_manifest_digest(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, "p3-accountability-capsule.json")
    data = _json(_run("consent", "verify", "--capsule", str(cap), "--json"))
    assert data["status"] == "ok"
    assert data["manifest_digest"].startswith("sha256:")
    assert "transitive" in data["binding"]


def test_consent_verify_empty_and_defect(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, "threaded-capsule.json")
    result = _run("consent", "verify", "--capsule", str(cap))
    assert result.exit_code == 2 and "Nothing to check" in result.stdout

    def mutate(d: dict[str, Any]) -> None:
        d["facets"]["conversation"]["consent"] = [{"consent_id": 1}]

    cap2 = _capsule_dir(tmp_path / "b", "threaded-capsule.json", mutate)
    result = _run("consent", "verify", "--capsule", str(cap2))
    assert result.exit_code == 1 and "DEFECT" in result.stdout


def test_manifest_size_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _capsule_dir(tmp_path, "p3-accountability-capsule.json")
    monkeypatch.setattr(hitl_cli, "MAX_MANIFEST_BYTES", 16)
    result = _run("consent", "show", "--capsule", str(cap))
    assert result.exit_code == 1
    assert "limit" in _squash(result.output)
