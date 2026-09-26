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

"""``nova memstore access ledger | derive | provenance`` (ADR-0171 P2).

Exit-code contract (``novafabric.cli.memstore`` docstring): 0 = reported
(out-of-scope rows are evidence, not failure), 1 = defective evidence or an
unbindable root read, 2 = nothing could be checked. Every output carries the
in-mission-boundary line. Commands are read-only over the capsules supplied.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.cli import memstore as cli_mod
from novafabric.cli.main import app
from novafabric.memstore import IN_MISSION_BOUNDARY

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "memstore"
STORE = "org-kb/support-playbooks"
runner = CliRunner()


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Any]:
    doc = json.loads((FIXTURES / "valid-cross-run-derivation.json").read_text())
    dirs = []
    for manifest in doc["capsules"]:
        d = tmp_path / manifest["run_id"]
        d.mkdir()
        (d / "capsule.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
        dirs.append(d)
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"ledger": doc["ledger"]}))
    return {"doc": doc, "dirs": dirs, "ledger": ledger, "tmp": tmp_path}


def _caps(world: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for d in world["dirs"]:
        out += ["--capsule", str(d)]
    return out


def _digest_tree(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(str(p).encode() + p.read_bytes())
    return h.hexdigest()


def _run(*args: str) -> Any:
    return runner.invoke(app, ["memstore", *args])


# ── help smoke ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", [[], ["access"], ["access", "ledger"], ["derive"], ["provenance"]])
def test_help(path: list[str]) -> None:
    result = _run(*path, "--help")
    assert result.exit_code == 0
    assert "Usage" in result.output


# ── access ledger ─────────────────────────────────────────────────────────


def test_access_ledger_json_lists_uncontained_as_evidence(world: dict[str, Any]) -> None:
    result = _run("access", "ledger", *_caps(world), "--store", STORE, "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True and payload["boundary"] == IN_MISSION_BOUNDARY
    assert len(payload["accesses"]) == 5 and payload["uncontained"] == 1
    only = _run("access", "ledger", *_caps(world), "--store", STORE, "--uncontained", "--json")
    rows = json.loads(only.output)["accesses"]
    assert only.exit_code == 0
    assert [(r["run"], r["namespace"], r["contained"]) for r in rows] == [
        ("run_B", "billing", False)
    ]


def test_access_ledger_table_and_agent_filter(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from rich.console import Console

    monkeypatch.setattr(cli_mod, "console", Console(width=200))
    result = _run("access", "ledger", *_caps(world), "--store", STORE, "--agent", "triage-agent")
    assert result.exit_code == 0
    flat = _flat(result.output)
    assert "NO (evidence)" in flat and "support-agent" not in flat
    assert _flat(IN_MISSION_BOUNDARY) in flat


def test_access_ledger_none_recorded(world: dict[str, Any]) -> None:
    result = _run("access", "ledger", *_caps(world), "--store", "other-store")
    assert result.exit_code == 0
    assert "No access rows recorded" in _flat(result.output)


def test_access_ledger_defective_block_exits_1(world: dict[str, Any]) -> None:
    forged = json.loads((FIXTURES / "invalid-access-forged-contained.json").read_text())
    d = world["tmp"] / "forged"
    d.mkdir()
    manifest = {
        "run_id": "run_8f3bd21e",
        "facets": {"memstore_mutation": {"store_id": STORE, "access": forged["block"]}},
    }
    (d / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    result = _run("access", "ledger", "--capsule", str(d), "--store", STORE, "--json")
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False and payload["accesses"] == []


def test_commands_are_read_only(world: dict[str, Any]) -> None:
    before = _digest_tree(world["tmp"])
    _run("access", "ledger", *_caps(world), "--store", STORE)
    _run("derive", "--entry", "bl-7", "--run", "run_C", *_caps(world), "--store", STORE)
    _run("provenance", "--entry", "pb-4471", *_caps(world), "--store", STORE)
    assert _digest_tree(world["tmp"]) == before


# ── derive ────────────────────────────────────────────────────────────────


def test_derive_json_resolves_two_hops(world: dict[str, Any]) -> None:
    expect = world["doc"]["expect"]["derive_bl7_in_run_C"]
    result = _run(
        "derive", "--entry", "bl-7", "--run", "run_C", *_caps(world), "--store", STORE, "--json"
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    hops = payload["hops"]
    assert hops[0]["link"]["origin_mutation_ref"] == expect["origin_mutation_ref"]
    assert hops[0]["link"]["origin_run"] == "run_B"
    assert hops[1]["link"]["origin_run"] == "run_A"
    assert payload["ok"] and payload["ledger_chain_ok"] and payload["boundary"]


def test_derive_text_with_sidecar_ledger(world: dict[str, Any]) -> None:
    result = _run(
        "derive",
        "--entry",
        "bl-7",
        "--run",
        "run_C",
        *_caps(world),
        "--store",
        STORE,
        "--ledger",
        str(world["ledger"]),
    )
    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "[resolved] billing/bl-7 read by run_C" in flat
    assert "run run_A" in flat and _flat(IN_MISSION_BOUNDARY) in flat


def test_derive_depth_cap_reports_truncation(world: dict[str, Any]) -> None:
    result = _run(
        "derive",
        "--entry",
        "bl-7",
        "--run",
        "run_C",
        *_caps(world),
        "--store",
        STORE,
        "--max-depth",
        "0",
    )
    assert result.exit_code == 0
    assert "truncated" in _flat(result.output)


def test_derive_partial_capsule_set_warns_and_fails_unbound_root(world: dict[str, Any]) -> None:
    """Without run_B's capsule, bl-7's write is unknown: unresolved → exit 1."""
    caps = ["--capsule", str(world["dirs"][0]), "--capsule", str(world["dirs"][2])]
    result = _run("derive", "--entry", "bl-7", "--run", "run_C", *caps, "--store", STORE)
    assert result.exit_code == 1
    assert "[unresolved]" in _flat(result.output)


def test_derive_nothing_to_trace_exits_2(world: dict[str, Any]) -> None:
    result = _run("derive", "--entry", "nope", "--run", "run_C", *_caps(world), "--store", STORE)
    assert result.exit_code == 2


def test_derive_bad_bounds_exit_2(world: dict[str, Any]) -> None:
    result = _run(
        "derive",
        "--entry",
        "bl-7",
        "--run",
        "run_C",
        *_caps(world),
        "--store",
        STORE,
        "--max-nodes",
        "0",
    )
    assert result.exit_code == 2


def test_derive_broken_sidecar_exits_1(world: dict[str, Any]) -> None:
    doc = json.loads(world["ledger"].read_text())
    doc["ledger"].reverse()
    world["ledger"].write_text(json.dumps(doc))
    result = _run(
        "derive",
        "--entry",
        "bl-7",
        "--run",
        "run_C",
        *_caps(world),
        "--store",
        STORE,
        "--ledger",
        str(world["ledger"]),
        "--json",
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout)["ledger_chain_ok"] is False


@pytest.mark.parametrize(
    ("content", "code"),
    [
        ("not json", 2),
        ('{"ledger": 5}', 2),
        ('[{"store_id": ""}]', 1),
    ],
)
def test_derive_malformed_sidecar(world: dict[str, Any], content: str, code: int) -> None:
    world["ledger"].write_text(content)
    result = _run(
        "derive",
        "--entry",
        "bl-7",
        "--run",
        "run_C",
        *_caps(world),
        "--store",
        STORE,
        "--ledger",
        str(world["ledger"]),
    )
    assert result.exit_code == code


# ── provenance ────────────────────────────────────────────────────────────


def test_provenance_json(world: dict[str, Any]) -> None:
    expect = world["doc"]["expect"]["provenance_pb4471"]
    result = _run("provenance", "--entry", "pb-4471", *_caps(world), "--store", STORE, "--json")
    assert result.exit_code == 0, result.output
    node = json.loads(result.output)["writes"][0]
    assert node["read_by_runs"] == expect["read_by_runs"]
    assert node["source_ref"] == expect["source_ref"]


def test_provenance_text(world: dict[str, Any]) -> None:
    result = _run(
        "provenance",
        "--entry",
        "pb-4471",
        *_caps(world),
        "--store",
        STORE,
        "--namespace",
        "playbooks",
        "--ledger",
        str(world["ledger"]),
    )
    assert result.exit_code == 0
    flat = _flat(result.output)
    assert "read by 2 run(s): run_B, run_C" in flat and "source sha256:" in flat


def test_provenance_unresolved_reads_are_counted(world: dict[str, Any]) -> None:
    caps = ["--capsule", str(world["dirs"][1]), "--capsule", str(world["dirs"][2])]
    result = _run("provenance", "--entry", "bl-7", *caps, "--store", STORE)
    assert result.exit_code == 0
    assert "read by 1 run(s): run_C" in _flat(result.output)


def test_provenance_no_write_exits_2(world: dict[str, Any]) -> None:
    result = _run("provenance", "--entry", "nope", *_caps(world), "--store", STORE)
    assert result.exit_code == 2


# ── input handling ────────────────────────────────────────────────────────


def test_missing_capsule_exits_2(tmp_path: Path) -> None:
    result = _run("access", "ledger", "--capsule", str(tmp_path / "nope"), "--store", STORE)
    assert result.exit_code == 2


def test_no_capsule_exits_2() -> None:
    assert _run("access", "ledger", "--store", STORE).exit_code == 2


@pytest.mark.parametrize("content", ["- a\n- b\n", "key: [unclosed\n", b"\xff\xfe"])
def test_unreadable_manifest_exits_2(tmp_path: Path, content: str | bytes) -> None:
    d = tmp_path / "cap"
    d.mkdir()
    path = d / "capsule.yaml"
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)
    assert _run("access", "ledger", "--capsule", str(d), "--store", STORE).exit_code == 2


def test_oversize_manifest_exits_2(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_mod, "MAX_INPUT_BYTES", 10)
    assert _run("access", "ledger", *_caps(world), "--store", STORE).exit_code == 2


def test_input_bounds_exit_2(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_mod, "MAX_CAPSULES", 1)
    assert _run("access", "ledger", *_caps(world), "--store", STORE).exit_code == 2
    monkeypatch.setattr(cli_mod, "MAX_CAPSULES", 512)
    monkeypatch.setattr(cli_mod, "MAX_LEDGER_RECORDS", 1)
    result = _run(
        "provenance",
        "--entry",
        "pb-4471",
        *_caps(world),
        "--store",
        STORE,
        "--ledger",
        str(world["ledger"]),
    )
    assert result.exit_code == 2


def test_bad_store_id_exits_2(world: dict[str, Any]) -> None:
    assert _run("access", "ledger", *_caps(world), "--store", "").exit_code == 2


def test_oversized_assembly_exits_2(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from novafabric.memstore import derivation

    monkeypatch.setattr(derivation, "MAX_LEDGER_RECORDS", 1)
    result = _run("provenance", "--entry", "pb-4471", *_caps(world), "--store", STORE)
    assert result.exit_code == 2
