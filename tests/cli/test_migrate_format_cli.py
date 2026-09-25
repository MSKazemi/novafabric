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

"""CLI smoke + failure paths for ``nova migrate-format`` (ADR-0165 P2, NF-332)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.cli.migrate_format import BOUNDARY_LINE
from novafabric.preservation import (
    PreservationFacet,
    chain_from_facet,
    digest_artifact,
    verify_format_migration_chain,
)

runner = CliRunner()

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "preservation"
ANCHOR = FIXTURES / "valid-anchor.json"
VALID_CHAIN = FIXTURES / "valid-format-migration-chain.json"
INVALID_CHAIN = FIXTURES / "invalid-format-migration-chain-broken-parent.json"
TOOL = digest_artifact("migrator")
D03 = digest_artifact("migrated-0.3.0")


def _invoke(*args: str) -> Any:
    return runner.invoke(app, ["migrate-format", *args])


def _boundary_printed(result: Any) -> bool:
    # Rich may wrap the long line; compare whitespace-normalised.
    return " ".join(BOUNDARY_LINE.split()) in " ".join(result.stderr.split())


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    d = tmp_path / "01HXAY7M5JZ8R7K4P9DPBYK2WX"
    d.mkdir()
    manifest = {
        "schema_version": "0.2.0",
        "run_id": d.name,
        "facets": {"preservation": json.loads(ANCHOR.read_text())},
    }
    (d / "capsule.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    return d


def test_help_smoke() -> None:
    result = runner.invoke(app, ["migrate-format", "--help"])
    assert result.exit_code == 0
    assert "format-migration" in result.stdout


def test_append_first_hop_from_capsule_never_modifies_it(capsule_dir: Path, tmp_path: Path) -> None:
    before = (capsule_dir / "capsule.yaml").read_bytes()
    artifact = tmp_path / "migrated.yaml"
    artifact.write_bytes(b"migrated-capsule-bytes")
    out = tmp_path / "facet.json"
    result = _invoke(
        "--capsule",
        str(capsule_dir),
        "--to",
        "run-capsule@0.3.0",
        "--tool",
        TOOL,
        "--migrated-artifact",
        str(artifact),
        "--migrated-at",
        "2029-02-01T00:00:00Z",
        "-o",
        str(out),
    )
    assert result.exit_code == 0, result.output
    assert (capsule_dir / "capsule.yaml").read_bytes() == before
    facet = PreservationFacet.model_validate(json.loads(out.read_text()))
    (hop,) = chain_from_facet(facet)
    assert hop.from_version == "run-capsule@0.2.0"  # derived from schema_version
    assert hop.post_digest == digest_artifact(b"migrated-capsule-bytes")
    assert hop.parent is None and hop.pre_digest == facet.original_root
    assert _boundary_printed(result)


def test_append_by_run_id(capsule_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(capsule_dir.parent))
    result = _invoke(
        "--capsule",
        capsule_dir.name,
        "--to",
        "run-capsule@0.3.0",
        "--tool",
        TOOL,
        "--post-digest",
        D03,
    )
    assert result.exit_code == 0, result.output
    facet = PreservationFacet.model_validate(json.loads(result.stdout))
    (hop,) = chain_from_facet(facet)
    assert hop.migrated_at.endswith("Z")  # defaulted to now, UTC


def test_append_to_valid_chain_from_facet_file_to_stdout() -> None:
    result = _invoke(
        "--facet",
        str(VALID_CHAIN),
        "--to",
        "run-capsule@0.5.0",
        "--tool",
        "https://tools.example.org/m",
        "--post-digest",
        digest_artifact("v5"),
        "--migrated-at",
        "2040-01-01T00:00:00Z",
    )
    assert result.exit_code == 0, result.output
    facet = PreservationFacet.model_validate(json.loads(result.stdout))
    hops = chain_from_facet(facet)
    assert len(hops) == 3 and hops[-1].from_version == "run-capsule@0.4.0"
    assert verify_format_migration_chain(hops, facet.original_root).ok


def test_check_valid_chain_json() -> None:
    result = _invoke("--facet", str(VALID_CHAIN), "--check", "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True and payload["reaches_original_root"] is True
    assert _boundary_printed(result)


def test_check_broken_chain_exits_non_zero() -> None:
    result = _invoke("--facet", str(INVALID_CHAIN), "--check", "--json")
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["reaches_original_root"] is False
    assert "missing_parent" in result.stderr
    assert _boundary_printed(result)


def test_check_human_output() -> None:
    result = _invoke("--facet", str(INVALID_CHAIN), "--check")
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "BROKEN" in result.stderr


def test_append_to_broken_chain_is_refused() -> None:
    result = _invoke(
        "--facet",
        str(INVALID_CHAIN),
        "--to",
        "run-capsule@0.5.0",
        "--tool",
        TOOL,
        "--post-digest",
        digest_artifact("v5"),
        "--migrated-at",
        "2040-01-01T00:00:00Z",
    )
    assert result.exit_code == 1
    assert "Refused" in result.stderr and result.stdout == ""
    assert _boundary_printed(result)


def test_non_monotonic_hop_is_refused() -> None:
    result = _invoke(
        "--facet",
        str(VALID_CHAIN),
        "--to",
        "run-capsule@0.1.0",
        "--tool",
        TOOL,
        "--post-digest",
        digest_artifact("v1"),
        "--migrated-at",
        "2040-01-01T00:00:00Z",
    )
    assert result.exit_code == 1
    assert "version_not_increasing" in result.stderr


def test_output_never_overwrites(tmp_path: Path) -> None:
    out = tmp_path / "facet.json"
    out.write_text("earlier chain version")
    result = _invoke(
        "--facet",
        str(VALID_CHAIN),
        "--to",
        "run-capsule@0.5.0",
        "--tool",
        TOOL,
        "--post-digest",
        digest_artifact("v5"),
        "--migrated-at",
        "2040-01-01T00:00:00Z",
        "-o",
        str(out),
    )
    assert result.exit_code == 2
    assert out.read_text() == "earlier chain version"
    assert "refusing to overwrite" in result.stderr


def test_output_to_unwritable_location(tmp_path: Path) -> None:
    result = _invoke(
        "--facet",
        str(VALID_CHAIN),
        "--to",
        "run-capsule@0.5.0",
        "--tool",
        TOOL,
        "--post-digest",
        digest_artifact("v5"),
        "--migrated-at",
        "2040-01-01T00:00:00Z",
        "-o",
        str(tmp_path / "missing-dir" / "facet.json"),
    )
    assert result.exit_code == 2
    assert "cannot write" in result.stderr


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "exactly one of --capsule or --facet"),
        (["--facet", str(VALID_CHAIN)], "--to and --tool are required"),
        (
            ["--facet", str(VALID_CHAIN), "--to", "run-capsule@0.5.0", "--tool", TOOL],
            "exactly one of --post-digest or --migrated-artifact",
        ),
        (
            [
                "--facet",
                str(VALID_CHAIN),
                "--to",
                "run-capsule@0.5.0",
                "--tool",
                TOOL,
                "--migrated-artifact",
                "/nonexistent/file",
            ],
            "is not a file",
        ),
        (
            [
                "--facet",
                str(VALID_CHAIN),
                "--to",
                "run-capsule@0.5.0",
                "--tool",
                "https://u:pw@example.org/m",
                "--post-digest",
                D03,
            ],
            "credentials",
        ),
        (
            [
                "--facet",
                str(VALID_CHAIN),
                "--to",
                "not-a-version",
                "--tool",
                TOOL,
                "--post-digest",
                D03,
            ],
            "format version",
        ),
        (
            [
                "--facet",
                str(VALID_CHAIN),
                "--from",
                "run-capsule@0.2.0",
                "--to",
                "run-capsule@0.5.0",
                "--tool",
                TOOL,
                "--post-digest",
                D03,
            ],
            "contradicts",
        ),
        (["--facet", "/nonexistent/facet.json", "--check"], "cannot read facet file"),
        (["--capsule", "no-such-run-id-xyz", "--check"], "No capsule found"),
    ],
)
def test_input_errors_exit_2(args: list[str], message: str) -> None:
    result = _invoke(*args)
    assert result.exit_code == 2, result.output
    assert message in " ".join(result.stderr.split())
    assert _boundary_printed(result)


def test_first_hop_on_facet_file_needs_from(tmp_path: Path) -> None:
    result = _invoke(
        "--facet",
        str(ANCHOR),
        "--to",
        "run-capsule@0.3.0",
        "--tool",
        TOOL,
        "--post-digest",
        D03,
    )
    assert result.exit_code == 2
    assert "needs from_version" in result.stderr


def test_facet_file_must_be_object_and_valid(tmp_path: Path) -> None:
    arr = tmp_path / "arr.json"
    arr.write_text("[]")
    assert _invoke("--facet", str(arr), "--check").exit_code == 2
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"preservation_id": "x"}))
    result = _invoke("--facet", str(bad), "--check")
    assert result.exit_code == 2 and "facet is invalid" in result.stderr


@pytest.mark.parametrize(
    "stored", ["not-a-list", [{"from_version": "run-capsule@0.2.0"}]], ids=["shape", "hop"]
)
def test_malformed_stored_chain_is_broken_not_bad_input(tmp_path: Path, stored: object) -> None:
    """A tampered stored chain is broken evidence (exit 1), never a usage error (2)."""
    data = json.loads(ANCHOR.read_text())
    data["format_migration_chain"] = stored
    f = tmp_path / "f.json"
    f.write_text(json.dumps(data))
    result = _invoke("--facet", str(f), "--check")
    assert result.exit_code == 1, result.stderr
    assert "BROKEN" in result.stderr and "malformed" in result.stderr
    assert _boundary_printed(result)
    as_json = _invoke("--facet", str(f), "--check", "--json")
    assert as_json.exit_code == 1
    payload = json.loads(as_json.stdout)
    assert payload["ok"] is False and "malformed" in payload["malformed_record"]
    append = _invoke(
        "--facet", str(f), "--to", "run-capsule@0.3.0", "--tool", TOOL,
        "--post-digest", D03, "--from", "run-capsule@0.2.0",
    )  # fmt: skip
    assert append.exit_code == 1 and "Refused" in append.stderr and append.stdout == ""


def test_capsule_without_anchor_exits_2(tmp_path: Path) -> None:
    d = tmp_path / "cap"
    d.mkdir()
    (d / "capsule.yaml").write_text(yaml.safe_dump({"schema_version": "1.0.0"}))
    result = _invoke("--capsule", str(d), "--check")
    assert result.exit_code == 2 and "no facets.preservation anchor" in result.stderr


def test_capsule_manifest_not_a_mapping(tmp_path: Path) -> None:
    d = tmp_path / "cap"
    d.mkdir()
    (d / "capsule.yaml").write_text("- a\n- b\n")
    result = _invoke("--capsule", str(d), "--check")
    assert result.exit_code == 2 and "not a mapping" in result.stderr


def test_capsule_manifest_unparseable(tmp_path: Path) -> None:
    d = tmp_path / "cap"
    d.mkdir()
    (d / "capsule.yaml").write_text("key: [unclosed\n")
    result = _invoke("--capsule", str(d), "--check")
    assert result.exit_code == 2 and "cannot read capsule.yaml" in result.stderr


def test_capsule_check_reports_ok(capsule_dir: Path, tmp_path: Path) -> None:
    copy = tmp_path / "copy"
    shutil.copytree(capsule_dir, copy)
    result = _invoke("--capsule", str(copy), "--check")
    assert result.exit_code == 0 and "OK" in result.stderr


def test_rejected_credential_is_not_echoed() -> None:
    result = _invoke(
        "--facet",
        str(VALID_CHAIN),
        "--to",
        "run-capsule@0.5.0",
        "--tool",
        "https://u:hunter2@example.org/m",
        "--post-digest",
        D03,
    )
    assert result.exit_code == 2
    assert "hunter2" not in result.output
