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

"""``nova provenance`` — the NF-161/162/163 CLI surface (ADR-0148 D1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from trust._provenance_fixtures import (
    IMAGE_BYTES,
    IMAGE_HASH,
    OTHER_HASH,
    a_capsule,
    a_manifest,
)
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.trust.provenance._honesty import HONESTY_LINE
from novafabric.trust.provenance.watermark import SOFT_BINDING_LABEL

runner = CliRunner()


def run(*args: str) -> Any:
    return runner.invoke(app, ["provenance", *args])


@pytest.fixture
def bound_capsule(tmp_path: Path) -> Path:
    """A capsule with one media blob and a matching sidecar manifest."""
    return a_capsule(tmp_path, blob=IMAGE_BYTES, sidecar=a_manifest())


@pytest.fixture
def bare_capsule(tmp_path: Path) -> Path:
    """A capsule with media but no manifest anywhere."""
    return a_capsule(tmp_path, blob=IMAGE_BYTES)


# --- help smoke --------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ("--help",),
        ("bind", "--help"),
        ("show", "--help"),
        ("verify", "--help"),
        ("output", "--help"),
        ("watermark", "--help"),
        ("watermark", "show", "--help"),
    ],
)
def test_help_is_reachable_for_every_subcommand(args: tuple[str, ...]) -> None:
    result = run(*args)
    assert result.exit_code == 0, result.output


# --- bind --------------------------------------------------------------------


def test_bind_reports_the_binding_and_the_honesty_line(bound_capsule: Path) -> None:
    result = run("bind", "--capsule", str(bound_capsule))
    assert result.exit_code == 0, result.output
    assert "Bound 1 manifest(s)" in result.output
    assert "hard_binding_ok=True" in result.output
    assert HONESTY_LINE.split(".")[0] in result.output


def test_bind_does_not_write_unless_asked(bound_capsule: Path) -> None:
    before = (bound_capsule / "capsule.yaml").read_text()
    result = run("bind", "--capsule", str(bound_capsule))
    assert result.exit_code == 0
    assert (bound_capsule / "capsule.yaml").read_text() == before
    assert "Pass --write to persist" in result.output


def test_bind_write_persists_the_facet(bound_capsule: Path) -> None:
    result = run("bind", "--capsule", str(bound_capsule), "--write")
    assert result.exit_code == 0, result.output
    manifest = yaml.safe_load((bound_capsule / "capsule.yaml").read_text())
    assert "media_provenance" in manifest["facets"]
    assert manifest["facets"]["media_provenance"]["entries"][0][
        "bound_content_hash"
    ] == IMAGE_HASH


def test_bind_with_no_manifest_exits_zero_and_says_nothing_was_found(
    bare_capsule: Path,
) -> None:
    """AC7 / I-3 — fail-open. Absent material is not an error, and the message must
    not read as a finding that the media carries no provenance."""
    result = run("bind", "--capsule", str(bare_capsule))
    assert result.exit_code == 0, result.output
    assert "Nothing was found to bind" in result.output
    assert "this is not" in result.output


def test_bind_json_is_machine_readable(bound_capsule: Path) -> None:
    result = run("bind", "--capsule", str(bound_capsule), "--json")
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["entries"][0]["bound_against"] == "blob_bytes"
    assert "cert_chain_ok" not in body["entries"][0]["verified"], (
        "cert_chain_ok is None and excluded; the reason field carries the meaning"
    )
    assert body["entries"][0]["verified"]["cert_chain_reason"]


def test_bind_json_on_an_empty_result_is_still_json(bare_capsule: Path) -> None:
    result = run("bind", "--capsule", str(bare_capsule), "--json")
    assert result.exit_code == 0
    assert json.loads(result.output) == {"media_provenance": None, "bound": 0}


def test_bind_marks_declared_output_hashes_with_nf163_fields(
    bound_capsule: Path,
) -> None:
    result = run(
        "bind",
        "--capsule",
        str(bound_capsule),
        "--output-hash",
        IMAGE_HASH,
        "--producing-model",
        "img-gen-v3",
        "--producing-run-id",
        "run_1",
        "--art50-marking-claimed",
        "--nf094-receipt-digest",
        f"sha256:{'ee' * 32}",
        "--json",
    )
    assert result.exit_code == 0, result.output
    entry = json.loads(result.output)["entries"][0]
    assert entry["direction"] == "output"
    assert entry["producing_model"] == "img-gen-v3"
    assert entry["art50_marking_claimed"] is True


def test_bind_accepts_an_explicit_manifest_path(tmp_path: Path) -> None:
    capsule = a_capsule(tmp_path, blob=IMAGE_BYTES)
    manifest_path = tmp_path / f"{IMAGE_HASH.split(':')[1]}.c2pa.json"
    manifest_path.write_text(json.dumps(a_manifest()))
    result = run(
        "bind", "--capsule", str(capsule), "--manifest", str(manifest_path), "--json"
    )
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.output)["entries"]) == 1


def test_an_explicit_manifest_cannot_be_bound_to_media_it_is_not_named_for(
    tmp_path: Path,
) -> None:
    """The filename is the binding. A manifest named for other content must not
    silently attach to whatever media the capsule happens to hold."""
    capsule = a_capsule(tmp_path, blob=IMAGE_BYTES)
    wrong = tmp_path / f"{OTHER_HASH.split(':')[1]}.c2pa.json"
    wrong.write_text(json.dumps(a_manifest()))
    result = run("bind", "--capsule", str(capsule), "--manifest", str(wrong), "--json")
    assert result.exit_code == 0
    assert json.loads(result.output) == {"media_provenance": None, "bound": 0}


def test_bind_rejects_a_misnamed_manifest_file(tmp_path: Path) -> None:
    capsule = a_capsule(tmp_path, blob=IMAGE_BYTES)
    bad = tmp_path / "manifest.json"
    bad.write_text("{}")
    result = run("bind", "--capsule", str(capsule), "--manifest", str(bad))
    assert result.exit_code == 2
    assert "must be named" in result.output


def test_bind_rejects_a_manifest_whose_name_is_not_a_digest(tmp_path: Path) -> None:
    capsule = a_capsule(tmp_path, blob=IMAGE_BYTES)
    bad = tmp_path / "not-a-digest.c2pa.json"
    bad.write_text("{}")
    result = run("bind", "--capsule", str(capsule), "--manifest", str(bad))
    assert result.exit_code == 2
    assert "not a sha256 hex digest" in result.output


def test_bind_rejects_a_missing_manifest(tmp_path: Path) -> None:
    capsule = a_capsule(tmp_path, blob=IMAGE_BYTES)
    missing = tmp_path / f"{IMAGE_HASH.split(':')[1]}.c2pa.json"
    result = run("bind", "--capsule", str(capsule), "--manifest", str(missing))
    assert result.exit_code == 2
    assert "Manifest not found" in result.output


def test_bind_rejects_unparseable_manifest_json(tmp_path: Path) -> None:
    capsule = a_capsule(tmp_path, blob=IMAGE_BYTES)
    broken = tmp_path / f"{IMAGE_HASH.split(':')[1]}.c2pa.json"
    broken.write_text("{not json")
    result = run("bind", "--capsule", str(capsule), "--manifest", str(broken))
    assert result.exit_code == 2
    assert "Could not read manifest" in result.output


# --- usage errors ------------------------------------------------------------


@pytest.mark.parametrize("cmd", ["bind", "show", "verify", "output"])
def test_a_missing_capsule_directory_is_a_usage_error(tmp_path: Path, cmd: str) -> None:
    result = run(cmd, "--capsule", str(tmp_path / "nope"))
    assert result.exit_code == 2
    assert "Capsule directory not found" in result.output


def test_a_capsule_without_a_manifest_file_is_a_usage_error(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = run("show", "--capsule", str(empty))
    assert result.exit_code == 2
    assert "capsule.yaml not found" in result.output


def test_an_unreadable_capsule_manifest_is_a_usage_error(tmp_path: Path) -> None:
    capsule = tmp_path / "bad"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text("[1, 2, 3]\n")
    result = run("show", "--capsule", str(capsule))
    assert result.exit_code == 2
    assert "not a mapping" in result.output


# --- show / verify / output --------------------------------------------------


def test_show_prints_a_persisted_facet(bound_capsule: Path) -> None:
    assert run("bind", "--capsule", str(bound_capsule), "--write").exit_code == 0
    result = run("show", "--capsule", str(bound_capsule))
    assert result.exit_code == 0, result.output
    assert "media_provenance v0.1.0" in result.output
    assert "CN=Camera Co" not in result.output, "this manifest is unsigned"


def test_show_on_a_capsule_without_the_facet_exits_zero(bound_capsule: Path) -> None:
    result = run("show", "--capsule", str(bound_capsule))
    assert result.exit_code == 0
    assert "No media_provenance facet" in result.output


def test_verify_reports_ok_and_never_claims_a_verified_cert_chain(
    bound_capsule: Path,
) -> None:
    """AC3 at the CLI boundary — the surface a user reads must not imply a chain
    verification that never happened."""
    assert run("bind", "--capsule", str(bound_capsule), "--write").exit_code == 0
    result = run("verify", "--capsule", str(bound_capsule))
    assert result.exit_code == 0, result.output
    assert "hard_binding=ok" in result.output.replace("\n", "")
    assert "cert_chain=unknown" in result.output
    assert "no_offline_cert_chain_verifier" in result.output


def test_verify_reports_a_broken_binding_but_still_exits_zero(tmp_path: Path) -> None:
    """The exit-code contract: reporting a broken binding IS the job succeeding.
    Exiting non-zero by default would make every reader of this command a gate."""
    capsule = a_capsule(
        tmp_path, blob=IMAGE_BYTES, sidecar=a_manifest(claimed_hash=OTHER_HASH)
    )
    assert run("bind", "--capsule", str(capsule), "--write").exit_code == 0
    result = run("verify", "--capsule", str(capsule))
    assert result.exit_code == 0, result.output
    assert "FAILED" in result.output
    assert "1 with no established binding" in result.output


def test_verify_strict_is_the_opt_in_gate(tmp_path: Path) -> None:
    capsule = a_capsule(
        tmp_path, blob=IMAGE_BYTES, sidecar=a_manifest(claimed_hash=OTHER_HASH)
    )
    assert run("bind", "--capsule", str(capsule), "--write").exit_code == 0
    assert run("verify", "--capsule", str(capsule), "--strict").exit_code == 1


def test_verify_strict_stays_zero_when_every_binding_holds(bound_capsule: Path) -> None:
    assert run("bind", "--capsule", str(bound_capsule), "--write").exit_code == 0
    assert run("verify", "--capsule", str(bound_capsule), "--strict").exit_code == 0


def test_verify_json_carries_the_honesty_line(bound_capsule: Path) -> None:
    """A machine consumer must receive the caveat too, not only a terminal reader."""
    assert run("bind", "--capsule", str(bound_capsule), "--write").exit_code == 0
    result = run("verify", "--capsule", str(bound_capsule), "--json")
    assert result.exit_code == 0
    body = json.loads(result.output)
    assert body["honesty"] == HONESTY_LINE
    assert body["verified"][0]["cert_chain_ok"] is None


def test_verify_json_strict_still_exits_one(tmp_path: Path) -> None:
    capsule = a_capsule(
        tmp_path, blob=IMAGE_BYTES, sidecar=a_manifest(claimed_hash=OTHER_HASH)
    )
    assert run("bind", "--capsule", str(capsule), "--write").exit_code == 0
    result = run("verify", "--capsule", str(capsule), "--strict", "--json")
    assert result.exit_code == 1
    assert json.loads(result.output)["unestablished"] == 1


def test_verify_without_a_facet_exits_zero(bound_capsule: Path) -> None:
    result = run("verify", "--capsule", str(bound_capsule))
    assert result.exit_code == 0
    assert "No media_provenance facet to verify" in result.output


def test_output_prints_the_nf163_receipts(bound_capsule: Path) -> None:
    assert (
        run(
            "bind",
            "--capsule",
            str(bound_capsule),
            "--output-hash",
            IMAGE_HASH,
            "--producing-model",
            "img-gen-v3",
            "--producing-run-id",
            "run_1",
            "--art50-marking-claimed",
            "--nf094-receipt-digest",
            f"sha256:{'ee' * 32}",
            "--write",
        ).exit_code
        == 0
    )
    result = run("output", "--capsule", str(bound_capsule))
    assert result.exit_code == 0, result.output
    assert "img-gen-v3" in result.output
    assert "art50_marking_claimed=true" in result.output


def test_output_on_an_input_only_capsule_reports_none(bound_capsule: Path) -> None:
    assert run("bind", "--capsule", str(bound_capsule), "--write").exit_code == 0
    result = run("output", "--capsule", str(bound_capsule))
    assert result.exit_code == 0
    assert "No output-media provenance receipts" in result.output


def test_output_json_is_a_list_even_when_empty(bound_capsule: Path) -> None:
    result = run("output", "--capsule", str(bound_capsule), "--json")
    assert result.exit_code == 0
    assert json.loads(result.output)["output_media_provenance"] == []


# --- watermark ---------------------------------------------------------------


def with_soft_binding(present: Any) -> dict[str, Any]:
    doc = a_manifest()
    doc["manifests"]["urn:manifest:1"]["assertions"].append(
        {"label": SOFT_BINDING_LABEL, "data": {"present": present}}
    )
    return doc


def test_watermark_show_bind_reads_and_persists_claims(tmp_path: Path) -> None:
    capsule = a_capsule(tmp_path, blob=IMAGE_BYTES, sidecar=with_soft_binding(True))
    result = run("watermark", "show", "--capsule", str(capsule), "--bind", "--write")
    assert result.exit_code == 0, result.output
    assert "present=true" in result.output
    manifest = yaml.safe_load((capsule / "capsule.yaml").read_text())
    assert "watermark_presence" in manifest["facets"]


def test_watermark_show_renders_no_claim_as_unknown(tmp_path: Path) -> None:
    """AC4 at the CLI boundary — ``unknown`` must never render as ``false``."""
    capsule = a_capsule(tmp_path, blob=IMAGE_BYTES, sidecar=with_soft_binding("maybe"))
    result = run("watermark", "show", "--capsule", str(capsule), "--bind")
    assert result.exit_code == 0, result.output
    assert "present=unknown" in result.output
    assert "present=false" not in result.output


def test_watermark_show_with_no_claim_says_so_without_asserting_absence(
    bound_capsule: Path,
) -> None:
    result = run("watermark", "show", "--capsule", str(bound_capsule), "--bind")
    assert result.exit_code == 0
    assert "No claim is not a claim of absence" in result.output


def test_watermark_show_json_keeps_null_for_unknown(tmp_path: Path) -> None:
    capsule = a_capsule(tmp_path, blob=IMAGE_BYTES, sidecar=with_soft_binding("maybe"))
    result = run(
        "watermark", "show", "--capsule", str(capsule), "--bind", "--json"
    )
    assert result.exit_code == 0
    assert json.loads(result.output)["entries"][0]["present"] is None


def test_watermark_show_reads_a_persisted_facet_without_bind(tmp_path: Path) -> None:
    capsule = a_capsule(tmp_path, blob=IMAGE_BYTES, sidecar=with_soft_binding(False))
    assert (
        run("watermark", "show", "--capsule", str(capsule), "--bind", "--write").exit_code
        == 0
    )
    result = run("watermark", "show", "--capsule", str(capsule))
    assert result.exit_code == 0, result.output
    assert "present=false" in result.output


def test_a_written_capsule_still_validates(bound_capsule: Path) -> None:
    """The end-to-end span: --write produces a capsule the validator accepts.

    ``attach_facet`` returning the right shape and ``nova validate`` accepting it are
    different questions, because ``facets`` is a closed registry (ADR-0196 D2). Both
    facets are written onto one real capsule base and validated together.
    """
    assert run("bind", "--capsule", str(bound_capsule), "--write").exit_code == 0
    assert (
        run(
            "watermark", "show", "--capsule", str(bound_capsule), "--bind", "--write"
        ).exit_code
        == 0
    )
    written = yaml.safe_load((bound_capsule / "capsule.yaml").read_text())
    assert "media_provenance" in written.get("facets", {})

    repo = Path(__file__).resolve().parents[2]
    schema = json.loads(
        (repo / "src" / "novafabric" / "schemas" / "run-capsule.schema.json").read_text(
            encoding="utf-8"
        )
    )
    base = json.loads(
        (repo / "tests" / "trust" / "_capsule_base.json").read_text(encoding="utf-8")
    )
    base["facets"] = written["facets"]
    jsonschema.Draft202012Validator(schema).validate(base)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("show",), {"media_provenance": None}),
        (("verify",), {"media_provenance": None, "verified": []}),
    ],
)
def test_json_output_stays_json_when_there_is_no_facet(
    bound_capsule: Path, args: tuple[str, ...], expected: dict[str, Any]
) -> None:
    """A machine consumer must get parseable JSON on the empty path too — a prose
    line where JSON was promised breaks the caller, not the human."""
    result = run(*args, "--capsule", str(bound_capsule), "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == expected


def test_watermark_json_stays_json_when_there_is_no_facet(bound_capsule: Path) -> None:
    result = run("watermark", "show", "--capsule", str(bound_capsule), "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"watermark_presence": None}


def test_an_unparseable_capsule_manifest_is_a_usage_error(tmp_path: Path) -> None:
    capsule = tmp_path / "broken"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text("run_id: [unclosed\n")
    result = run("show", "--capsule", str(capsule))
    assert result.exit_code == 2
    assert "Could not read capsule.yaml" in result.output
