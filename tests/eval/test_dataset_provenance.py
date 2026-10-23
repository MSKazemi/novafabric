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
from collections.abc import Iterator
from pathlib import Path

import jsonschema  # type: ignore[import-untyped]
import pytest
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
    assert reg.resolve(_facet(dataset_hash="sha256:dd44", split_hash="sha256:bb22")) == "superseded"


def test_registry_contaminated_beats_superseded() -> None:
    reg = ContaminationRegistry(contaminated=["sha256:aa11"], superseded=["sha256:aa11"])
    assert reg.resolve(_facet(dataset_hash="sha256:aa11")) == "contaminated"


def test_registry_unknown_when_no_hashes() -> None:
    reg = ContaminationRegistry(contaminated=["sha256:eeee"])
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


# ── fail closed on a malformed facet ─────────────────────────────────────────
#
# Regression: a facet that could not be parsed or validated was silently
# skipped, so a capsule whose only facet was malformed read as "No
# dataset_provenance facets" and exited 0 — a CI contamination gate passed on
# input it could not read. It is now an ``invalid`` verdict with a reason,
# flagged like contamination (exit 4), and never a crash.

FACET_SUBDIR = Path("extensions") / "dev.novafabric.dataset-provenance"


def _write_raw_facet(cap: Path, name: str, data: bytes) -> Path:
    fdir = cap / FACET_SUBDIR
    fdir.mkdir(parents=True, exist_ok=True)
    path = fdir / name
    path.write_bytes(data)
    return path


_MALFORMED = {
    "not-json": b"{not json",
    "schema-bad-status": json.dumps({"name": "swe-bench", "status": "clean"}).encode(),
    "missing-name": json.dumps({"dataset_hash": "sha256:aa11", "status": "current"}).encode(),
    "empty-name": json.dumps({"name": "", "status": "current"}).encode(),
    "json-array": b"[]",
    "json-null": b"null",
    "empty-file": b"",
    "not-utf8": b"\xff\xfe\x00{",
}


@pytest.mark.parametrize("label", sorted(_MALFORMED))
def test_malformed_facet_is_an_invalid_verdict(tmp_path: Path, label: str) -> None:
    cap = _capsule(tmp_path)
    _write_raw_facet(cap, f"{label}.json", _MALFORMED[label])
    results = check_contamination(cap, None)
    assert len(results) == 1
    assert results[0].status == "invalid"
    assert results[0].name == label
    assert results[0].reason and f"{label}.json" in results[0].reason
    assert is_flagged(results)


def test_malformed_facet_beside_a_current_one_is_still_flagged(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    write_facet(cap, _facet(status="current"))
    _write_raw_facet(cap, "broken.json", b"{not json")
    results = check_contamination(cap, None)
    assert sorted(r.status for r in results) == ["current", "invalid"]
    assert is_flagged(results)


def test_registry_cannot_clear_an_invalid_facet(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    _write_raw_facet(cap, "broken.json", b"{not json")
    reg = ContaminationRegistry(contaminated=["sha256:eeee"])
    results = check_contamination(cap, reg)
    assert [r.status for r in results] == ["invalid"]


def test_facet_namespace_that_is_a_file_is_invalid(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    (cap / "extensions").mkdir()
    (cap / FACET_SUBDIR).write_text("{}")
    results = check_contamination(cap, None)
    assert [r.status for r in results] == ["invalid"]
    assert results[0].reason and "not a directory" in results[0].reason
    assert is_flagged(results)


def test_unreadable_facet_is_invalid_not_a_crash(tmp_path: Path) -> None:
    import os

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root reads a 0o000 file")
    cap = _capsule(tmp_path)
    path = _write_raw_facet(cap, "locked.json", _facet().model_dump_json().encode())
    path.chmod(0)
    try:
        results = check_contamination(cap, None)
    finally:
        path.chmod(0o600)
    assert [r.status for r in results] == ["invalid"]
    assert results[0].reason and "could not read" in results[0].reason


def test_valid_facets_carry_no_reason(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    write_facet(cap, _facet(status="current"))
    assert check_contamination(cap, None)[0].reason is None


def test_read_facets_still_returns_only_valid_facets(tmp_path: Path) -> None:
    """``read_facets`` keeps its contract (valid facets only); the verdict is check_contamination's."""
    cap = _capsule(tmp_path)
    write_facet(cap, _facet())
    _write_raw_facet(cap, "broken.json", b"{not json")
    assert [f.name for f in read_facets(cap)] == ["swe-bench"]


def test_cli_malformed_facet_exits_4_with_reason(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    _write_raw_facet(cap, "broken.json", b"{not json")
    result = runner.invoke(app, ["eval", "contamination-check", str(cap)])
    assert result.exit_code == 4, result.output
    assert isinstance(result.exception, SystemExit)
    assert "invalid" in result.output
    assert "broken.json" in result.output
    assert "No dataset_provenance facets" not in result.output


def test_cli_malformed_facet_json_output(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    _write_raw_facet(cap, "broken.json", b"{not json")
    result = runner.invoke(app, ["eval", "contamination-check", str(cap), "--json"])
    assert result.exit_code == 4, result.output
    data = json.loads(result.output)
    assert data[0]["status"] == "invalid"
    assert "broken.json" in data[0]["reason"]


# ── fail-closed against shaped facets (second adversarial review) ───────────
#
# Each of these exited 0 (or hung, or crashed with exit 1) on a contaminated
# capsule before: the hash compare was an exact string match, the facet parser
# kept the last of two duplicate keys, only lower-case ``*.json`` directly in the
# namespace was read, and a FIFO or ``/dev/zero`` facet was opened like a file.

_H = "sha256:" + "a" * 64


@pytest.fixture
def memory_cap() -> Iterator[None]:
    """Cap the address space so an unbounded read fails with MemoryError, not an OOM kill."""
    import os
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    pages = int(Path("/proc/self/statm").read_text().split()[0])
    cap = pages * os.sysconf("SC_PAGE_SIZE") + 1_500_000_000
    if hard != resource.RLIM_INFINITY:
        cap = min(cap, hard)
    resource.setrlimit(resource.RLIMIT_AS, (cap, hard))
    try:
        yield
    finally:
        resource.setrlimit(resource.RLIMIT_AS, (soft, hard))


def _registry_file(tmp_path: Path) -> Path:
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps({"contaminated": [_H]}))
    return reg


def _cli_exit(cap: Path, reg: Path) -> int:
    result = runner.invoke(
        app, ["eval", "contamination-check", str(cap), "--registry", str(reg), "--json"]
    )
    assert result.exception is None or isinstance(result.exception, SystemExit), result.output
    return result.exit_code


@pytest.mark.parametrize(
    "spelling",
    [_H, _H.upper(), _H.removeprefix("sha256:"), f" {_H}", f"{_H}\n", "SHA256: " + "A" * 64],
    ids=["canonical", "upper", "no-prefix", "leading-space", "trailing-newline", "spaced-prefix"],
)
def test_any_spelling_of_a_contaminated_hash_is_flagged(tmp_path: Path, spelling: str) -> None:
    cap = _capsule(tmp_path)
    _write_raw_facet(cap, "m.json", json.dumps({"name": "mmlu", "dataset_hash": spelling}).encode())
    assert _cli_exit(cap, _registry_file(tmp_path)) == 4
    [result] = check_contamination(cap, ContaminationRegistry.from_path(_registry_file(tmp_path)))
    assert result.status == "contaminated"
    assert result.dataset_hash == _H


def test_a_registry_hash_in_another_spelling_still_matches(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    write_facet(cap, _facet(dataset_hash=_H))
    reg = ContaminationRegistry(contaminated=[" " + _H.upper().removeprefix("SHA256:")])
    assert [r.status for r in check_contamination(cap, reg)] == ["contaminated"]


@pytest.mark.parametrize("bad", ["sha256:zz", "md5:" + "a" * 32, "sha256:" + "a" * 65, "abc"])
def test_a_hash_that_cannot_be_normalised_is_invalid(tmp_path: Path, bad: str) -> None:
    cap = _capsule(tmp_path)
    _write_raw_facet(cap, "m.json", json.dumps({"name": "mmlu", "dataset_hash": bad}).encode())
    [result] = check_contamination(cap, None)
    assert result.status == "invalid"
    assert result.reason and "sha256" in result.reason


def test_a_registry_entry_that_cannot_be_normalised_is_a_usage_error(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps({"contaminated": ["not-a-hash"]}))
    assert _cli_exit(cap, reg) == 2


def test_a_duplicate_key_makes_the_facet_invalid(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    raw = f'{{"name": "mmlu", "dataset_hash": "{_H}", "dataset_hash": ""}}'.encode()
    _write_raw_facet(cap, "m.json", raw)
    [result] = check_contamination(cap, None)
    assert result.status == "invalid"
    assert result.reason and "duplicate key" in result.reason
    assert _cli_exit(cap, _registry_file(tmp_path)) == 4


def test_a_duplicate_key_in_the_registry_is_a_usage_error(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    reg = tmp_path / "reg.json"
    reg.write_text(f'{{"contaminated": ["{_H}"], "contaminated": []}}')
    assert _cli_exit(cap, reg) == 2


@pytest.mark.parametrize("name", ["m.JSON", "m.Json"])
def test_facet_file_names_match_case_insensitively(tmp_path: Path, name: str) -> None:
    cap = _capsule(tmp_path)
    _write_raw_facet(cap, name, json.dumps({"name": "mmlu", "dataset_hash": _H}).encode())
    assert _cli_exit(cap, _registry_file(tmp_path)) == 4


def test_a_facet_in_a_subdirectory_fails_closed(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    sub = cap / FACET_SUBDIR / "sub"
    sub.mkdir(parents=True)
    (sub / "m.json").write_text(json.dumps({"name": "mmlu", "dataset_hash": _H}))
    [result] = check_contamination(cap, None)
    assert result.status == "invalid"
    assert result.reason and "subdirectory" in result.reason
    assert _cli_exit(cap, _registry_file(tmp_path)) == 4


@pytest.mark.timeout(30)
def test_a_fifo_facet_is_invalid_and_never_blocks(tmp_path: Path) -> None:
    import os

    cap = _capsule(tmp_path)
    (cap / FACET_SUBDIR).mkdir(parents=True)
    os.mkfifo(cap / FACET_SUBDIR / "m.json")
    [result] = check_contamination(cap, None)
    assert result.status == "invalid"
    assert _cli_exit(cap, _registry_file(tmp_path)) == 4


@pytest.mark.timeout(60)
@pytest.mark.parametrize("target", ["/dev/zero", "outside"])
def test_a_linked_facet_is_invalid_and_never_read(
    tmp_path: Path, target: str, memory_cap: None
) -> None:
    cap = _capsule(tmp_path)
    (cap / FACET_SUBDIR).mkdir(parents=True)
    if target == "outside":
        outside = tmp_path / "outside.json"
        outside.write_text(json.dumps({"name": "mmlu", "dataset_hash": "sha256:" + "b" * 64}))
        target = str(outside)
    (cap / FACET_SUBDIR / "m.json").symlink_to(target)
    [result] = check_contamination(cap, None)
    assert result.status == "invalid"
    assert result.reason and "symlink" in result.reason
    assert _cli_exit(cap, _registry_file(tmp_path)) == 4


@pytest.mark.parametrize("linked", ["namespace", "extensions"])
def test_a_linked_namespace_is_invalid(tmp_path: Path, linked: str) -> None:
    cap = _capsule(tmp_path)
    clean = tmp_path / "clean"
    clean.mkdir()
    if linked == "namespace":
        (cap / "extensions").mkdir()
        (cap / FACET_SUBDIR).symlink_to(clean, target_is_directory=True)
    else:
        (cap / "extensions").symlink_to(clean, target_is_directory=True)
        (clean / Path(FACET_SUBDIR).name).mkdir()
    [result] = check_contamination(cap, None)
    assert result.status == "invalid"
    assert result.reason and "symlink" in result.reason


def test_an_oversized_facet_is_invalid(tmp_path: Path) -> None:
    from novafabric.eval.dataset_provenance import MAX_FACET_BYTES

    cap = _capsule(tmp_path)
    body = json.dumps({"name": "mmlu", "dataset_hash": _H}) + " " * (MAX_FACET_BYTES + 1)
    _write_raw_facet(cap, "m.json", body.encode())
    [result] = check_contamination(cap, None)
    assert result.status == "invalid"
    assert result.reason and "larger than" in result.reason
