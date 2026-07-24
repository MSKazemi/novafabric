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

"""Tests for NF-028 dataset-provenance facet + contamination-check (ADR-0108)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema  # type: ignore[import-untyped]
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.eval.dataset_provenance import (
    ContaminationRegistry,
    DatasetProvenanceFacet,
    check_contamination,
    is_flagged,
    read_facets,
    write_facet,
)

runner = CliRunner()

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "schemas" / "dataset-provenance-v1.schema.json").read_text()
)


def _capsule(tmp_path: Path) -> Path:
    d = tmp_path / "cap-028"
    d.mkdir()
    (d / "capsule.yaml").write_text("run_id: run-028\n")
    return d


def _facet(**kw: object) -> DatasetProvenanceFacet:
    base = {"name": "swe-bench", "version": "2024-06", "dataset_hash": "sha256:aa11", "status": "current"}
    base.update(kw)
    return DatasetProvenanceFacet(**base)  # type: ignore[arg-type]


# ── schema (R6) ──────────────────────────────────────────────────────────────


def test_schema_is_meta_valid() -> None:
    jsonschema.Draft202012Validator.check_schema(SCHEMA)


def test_facet_serialization_validates(tmp_path: Path) -> None:
    facet = _facet(split_hash="sha256:bb22")
    jsonschema.Draft202012Validator(SCHEMA).validate(json.loads(facet.model_dump_json()))


def test_schema_rejects_bad_status() -> None:
    v = jsonschema.Draft202012Validator(SCHEMA)
    assert not v.is_valid({"name": "x", "dataset_hash": "sha256:aa", "status": "clean"})


# ── write/read round-trip (additive namespace) ───────────────────────────────


def test_write_and_read_facet(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    write_facet(cap, _facet())
    facets = read_facets(cap)
    assert len(facets) == 1
    assert facets[0].name == "swe-bench"


def test_read_no_facets_is_empty(tmp_path: Path) -> None:
    assert read_facets(_capsule(tmp_path)) == []


def test_malformed_facet_skipped(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    write_facet(cap, _facet())
    fdir = cap / "extensions" / "dev.novafabric.dataset-provenance"
    (fdir / "broken.json").write_text("{not json")
    assert len(read_facets(cap)) == 1


# ── registry resolution + exit code (R8) ─────────────────────────────────────


def test_registry_flags_superseded_by_hash() -> None:
    reg = ContaminationRegistry(superseded=["sha256:bb22"])
    assert reg.resolve(_facet(dataset_hash="sha256:xx", split_hash="sha256:bb22")) == "superseded"


def test_registry_contaminated_beats_superseded() -> None:
    reg = ContaminationRegistry(contaminated=["sha256:aa11"], superseded=["sha256:aa11"])
    assert reg.resolve(_facet(dataset_hash="sha256:aa11")) == "contaminated"


def test_registry_unknown_when_no_hashes() -> None:
    reg = ContaminationRegistry(contaminated=["sha256:zz"])
    assert reg.resolve(DatasetProvenanceFacet(name="d")) == "unknown"


def test_check_uses_recorded_status_without_registry(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    write_facet(cap, _facet(status="contaminated"))
    results = check_contamination(cap, None)
    assert results[0].status == "contaminated"
    assert is_flagged(results)


def test_registry_upgrades_severity(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    write_facet(cap, _facet(status="current", dataset_hash="sha256:aa11"))
    reg = ContaminationRegistry(contaminated=["sha256:aa11"])
    results = check_contamination(cap, reg)
    assert results[0].status == "contaminated"


def test_registry_does_not_downgrade(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    write_facet(cap, _facet(status="contaminated", dataset_hash="sha256:cc33"))
    reg = ContaminationRegistry()  # no entries → would resolve 'current'
    results = check_contamination(cap, reg)
    assert results[0].status == "contaminated"  # recorded severity preserved


def test_current_is_not_flagged(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    write_facet(cap, _facet(status="current"))
    assert not is_flagged(check_contamination(cap, None))


# ── CLI (R8, exit 4) ─────────────────────────────────────────────────────────


def test_cli_contamination_check_current_exit0(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    write_facet(cap, _facet(status="current"))
    result = runner.invoke(app, ["eval", "contamination-check", str(cap)])
    assert result.exit_code == 0, result.output
    assert "current" in result.output


def test_cli_contamination_check_flagged_exit4(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    write_facet(cap, _facet(status="current", dataset_hash="sha256:aa11"))
    reg = tmp_path / "known-bad.json"
    reg.write_text(json.dumps({"superseded": ["sha256:aa11"]}))
    result = runner.invoke(
        app, ["eval", "contamination-check", str(cap), "--registry", str(reg)]
    )
    assert result.exit_code == 4
    assert "superseded" in result.output


def test_cli_contamination_check_json(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    write_facet(cap, _facet(status="current"))
    result = runner.invoke(app, ["eval", "contamination-check", str(cap), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data[0]["name"] == "swe-bench"


def test_cli_no_facets_message(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    result = runner.invoke(app, ["eval", "contamination-check", str(cap)])
    assert result.exit_code == 0, result.output
    assert "No dataset_provenance facets" in result.output


def test_cli_bad_capsule_dir(tmp_path: Path) -> None:
    result = runner.invoke(app, ["eval", "contamination-check", str(tmp_path / "nope")])
    assert result.exit_code == 2


def test_cli_missing_registry_file(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    write_facet(cap, _facet())
    result = runner.invoke(
        app, ["eval", "contamination-check", str(cap), "--registry", str(tmp_path / "nope.json")]
    )
    assert result.exit_code == 2


def test_cli_malformed_registry(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    write_facet(cap, _facet())
    reg = tmp_path / "bad.json"
    reg.write_text("{not valid json")
    result = runner.invoke(
        app, ["eval", "contamination-check", str(cap), "--registry", str(reg)]
    )
    assert result.exit_code == 2
    assert "could not parse registry" in result.output


def test_cli_help_smoke() -> None:
    result = runner.invoke(app, ["eval", "contamination-check", "--help"])
    assert result.exit_code == 0
    assert "contamination" in result.output.lower()
