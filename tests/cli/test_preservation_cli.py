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

"""CLI smoke + failure paths for ``nova preservation`` (ADR-0165 P3, NF-333/334)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.cli import preservation as cli_mod
from novafabric.cli.main import app
from novafabric.cli.preservation import BOUNDARY_LINE
from novafabric.preservation import (
    PreservationFacet,
    crypto_migrations_from_facet,
    ltv_chain_from_facet,
    verify_ltv_chain,
)

runner = CliRunner()

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "preservation"
ANCHOR = FIXTURES / "valid-anchor.json"
VALID = FIXTURES / "valid-reseal-ltv-chain.json"
DROPPED = FIXTURES / "invalid-reseal-original-sig-dropped.json"
DOWNGRADE = FIXTURES / "invalid-ltv-chain-downgrade.json"


def d256(s: str) -> str:
    return "sha256:" + hashlib.sha256(s.encode()).hexdigest()


def _invoke(*args: str) -> Any:
    return runner.invoke(app, ["preservation", *args])


def _squash(text: str) -> str:
    return " ".join(text.split())


def _boundary(result: Any) -> bool:
    return _squash(BOUNDARY_LINE) in _squash(result.stderr)


RESEAL_ARGS = (
    "--to-alg",
    "ml-dsa-65",
    "--from-alg",
    "ed25519",
    "--upgrade-ref",
    "NF-192:upgrade-signature#op-1",
    "--renewal-timestamp-ref",
    d256("tst"),
    "--resealed-at",
    "2029-06-01T00:00:00Z",
)


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
    for path in ([], ["reseal"], ["ltv"], ["reseal", "record"], ["ltv", "append"]):
        result = runner.invoke(app, ["preservation", *path, "--help"])
        assert result.exit_code == 0, path
    assert "re-seal" in _squash(runner.invoke(app, ["preservation", "--help"]).stdout)


def test_reseal_record_from_capsule_never_modifies_it(capsule_dir: Path, tmp_path: Path) -> None:
    before = (capsule_dir / "capsule.yaml").read_bytes()
    out = tmp_path / "facet.json"
    result = _invoke(
        "reseal",
        "record",
        "--capsule",
        str(capsule_dir),
        *RESEAL_ARGS,
        "--original-sig-preserved",
        "--agent",
        "https://agents.example.org/op",
        "-o",
        str(out),
    )
    assert result.exit_code == 0, result.stderr
    assert (capsule_dir / "capsule.yaml").read_bytes() == before
    facet = PreservationFacet.model_validate(json.loads(out.read_text()))
    (event,) = crypto_migrations_from_facet(facet)
    assert event.original_sig_preserved is True and event.to_alg == "ml-dsa-65"
    assert _boundary(result)


def test_reseal_record_requires_assertion_and_refuses_dropped() -> None:
    missing = _invoke("reseal", "record", "--facet", str(ANCHOR), *RESEAL_ARGS)
    assert missing.exit_code == 2 and "original-sig-preserved" in missing.stderr
    dropped = _invoke(
        "reseal", "record", "--facet", str(ANCHOR), *RESEAL_ARGS, "--original-sig-dropped"
    )
    assert dropped.exit_code == 1 and "Refused" in dropped.stderr
    assert dropped.stdout == ""
    assert _boundary(dropped)


def test_reseal_record_stdout_and_broken_append(tmp_path: Path) -> None:
    result = _invoke(
        "reseal", "record", "--facet", str(VALID), "--to-alg", "ml-dsa-87",
        "--upgrade-ref", "op-2", "--renewal-timestamp-ref", d256("fresh"),
        "--resealed-at", "2031-01-01T00:00:00Z", "--original-sig-preserved",
    )  # fmt: skip
    assert result.exit_code == 0, result.stderr
    facet = PreservationFacet.model_validate(json.loads(result.stdout))
    assert [e.from_alg for e in crypto_migrations_from_facet(facet)] == ["ed25519", "ml-dsa-65"]
    reused = _invoke(
        "reseal", "record", "--facet", str(VALID), "--to-alg", "ml-dsa-87",
        "--upgrade-ref", "op-2",
        "--renewal-timestamp-ref",
        "sha256:08864605c051336226500532ef6929f9b5d91b01ab3fac553fbe1e3293611356",
        "--resealed-at", "2031-01-01T00:00:00Z", "--original-sig-preserved",
    )  # fmt: skip
    assert reused.exit_code == 1 and "renewal_timestamp_reused" in reused.stderr


def test_reseal_record_bad_inputs(tmp_path: Path) -> None:
    secret = _invoke(
        "reseal", "record", "--facet", str(ANCHOR), "--from-alg", "ed25519",
        "--to-alg", "ml-dsa-65", "--upgrade-ref", "https://u:hunter2@x.example/op",
        "--renewal-timestamp-ref", d256("t"), "--original-sig-preserved",
    )  # fmt: skip
    assert secret.exit_code == 2 and "hunter2" not in secret.stderr
    first_no_from = _invoke(
        "reseal", "record", "--facet", str(ANCHOR), "--to-alg", "ml-dsa-65",
        "--upgrade-ref", "op", "--renewal-timestamp-ref", d256("t"), "--original-sig-preserved",
    )  # fmt: skip
    assert first_no_from.exit_code == 2 and "from_alg" in first_no_from.stderr


def test_reseal_verify(tmp_path: Path) -> None:
    ok = _invoke("reseal", "verify", "--facet", str(VALID), "--json")
    assert ok.exit_code == 0
    payload = json.loads(ok.stdout)
    assert payload["ok"] and payload["scheme_migrations"] == ["ed25519→ml-dsa-65"]
    dropped = _invoke("reseal", "verify", "--facet", str(DROPPED))
    assert dropped.exit_code == 1 and "original_sig_preserved" in _squash(dropped.stderr)
    data = json.loads(VALID.read_text())
    data["crypto_migration"][0]["to_alg"] = "ed25519"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(data))
    broken = _invoke("reseal", "verify", "--facet", str(bad))
    assert broken.exit_code == 1 and "no_alg_change" in broken.stderr


def test_ltv_append_chain_and_output_create_only(tmp_path: Path) -> None:
    out1 = tmp_path / "one.json"
    first = _invoke(
        "ltv", "append", "--facet", str(ANCHOR), "--type", "timestamp_renewal",
        "--covered-digest", d256("ev"), "--new-timestamp-ref", d256("tA"),
        "--new-hash-alg", "sha256", "--renewed-before", "2030-01-01",
        "--renewed-at", "2029-01-01T00:00:00Z", "--expires-at", "2035-01-01", "-o", str(out1),
    )  # fmt: skip
    assert first.exit_code == 0, first.stderr
    second = _invoke(
        "ltv", "append", "--facet", str(out1), "--type", "timestamp_renewal",
        "--new-timestamp-ref", d256("tB"), "--new-hash-alg", "sha256",
        "--renewed-before", "2034-01-01",
    )  # fmt: skip
    assert second.exit_code == 0, second.stderr
    chain = ltv_chain_from_facet(PreservationFacet.model_validate(json.loads(second.stdout)))
    assert chain[1].covered_digest == chain[1].parent == d256("tA")
    assert verify_ltv_chain(chain).ok
    again = _invoke(
        "ltv", "append", "--facet", str(ANCHOR), "--type", "timestamp_renewal",
        "--covered-digest", d256("ev"), "--new-timestamp-ref", d256("tA"),
        "--new-hash-alg", "sha256", "--renewed-before", "2030-01-01", "-o", str(out1),
    )  # fmt: skip
    assert again.exit_code == 2 and "already exists" in again.stderr


def test_ltv_append_refusals(tmp_path: Path) -> None:
    late = _invoke(
        "ltv", "append", "--facet", str(VALID), "--type", "hash_tree_renewal",
        "--covered-digest", "sha512:" + hashlib.sha512(b"s").hexdigest(),
        "--new-timestamp-ref", "sha512:" + hashlib.sha512(b"t").hexdigest(),
        "--new-hash-alg", "sha512", "--renewed-before", "2050-01-01",
    )  # fmt: skip
    assert late.exit_code == 1 and "renewed_after_prior_expiry" in late.stderr
    broken = _invoke(
        "ltv", "append", "--facet", str(DOWNGRADE), "--type", "timestamp_renewal",
        "--new-timestamp-ref", d256("z"), "--new-hash-alg", "sha256",
        "--renewed-before", "2040-01-01",
    )  # fmt: skip
    assert broken.exit_code == 1 and "already fails" in _squash(broken.stderr)
    no_cover = _invoke(
        "ltv", "append", "--facet", str(ANCHOR), "--type", "hash_tree_renewal",
        "--new-timestamp-ref", d256("z"), "--new-hash-alg", "sha256",
        "--renewed-before", "2040-01-01",
    )  # fmt: skip
    assert no_cover.exit_code == 2 and "covered_digest" in no_cover.stderr


def test_ltv_verify(capsule_dir: Path) -> None:
    ok = _invoke("ltv", "verify", "--facet", str(VALID), "--json")
    assert ok.exit_code == 0 and json.loads(ok.stdout)["ok"] is True
    bad = _invoke("ltv", "verify", "--facet", str(DOWNGRADE), "--json")
    assert bad.exit_code == 1
    codes = {f["code"] for f in json.loads(bad.stdout)["findings"]}
    assert {"hash_alg_downgrade", "missing_parent"} <= codes
    human = _invoke("ltv", "verify", "--facet", str(DOWNGRADE))
    assert human.exit_code == 1 and "BROKEN" in human.stderr and human.stdout == ""
    empty = _invoke("ltv", "verify", "--capsule", str(capsule_dir))
    assert empty.exit_code == 0 and _boundary(empty)


@pytest.mark.parametrize("which", ["both", "neither"])
def test_exactly_one_source(which: str, capsule_dir: Path) -> None:
    args = ["--facet", str(VALID), "--capsule", str(capsule_dir)] if which == "both" else []
    result = _invoke("ltv", "verify", *args)
    assert result.exit_code == 2 and "exactly one" in result.stderr


def test_bad_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = _invoke("ltv", "verify", "--facet", str(tmp_path / "nope.json"))
    assert missing.exit_code == 2
    notjson = tmp_path / "x.json"
    notjson.write_text("{not json")
    assert _invoke("ltv", "verify", "--facet", str(notjson)).exit_code == 2
    listy = tmp_path / "l.json"
    listy.write_text("[]")
    assert "mapping" in _invoke("ltv", "verify", "--facet", str(listy)).stderr
    invalid = tmp_path / "i.json"
    invalid.write_text(json.dumps({"preservation_id": "x"}))
    assert "invalid" in _invoke("ltv", "verify", "--facet", str(invalid)).stderr
    malformed = tmp_path / "m.json"
    data = json.loads(ANCHOR.read_text())
    data["ltv_renewal_chain"] = [{"renewal_type": "timestamp_renewal"}]
    malformed.write_text(json.dumps(data))
    # a malformed *stored* record is broken evidence, not bad CLI input
    assert _invoke("ltv", "verify", "--facet", str(malformed)).exit_code == 1
    monkeypatch.setattr(cli_mod, "MAX_INPUT_BYTES", 10)
    big = _invoke("ltv", "verify", "--facet", str(VALID))
    assert big.exit_code == 2 and "limit" in big.stderr


def test_capsule_errors(tmp_path: Path) -> None:
    unknown = _invoke("reseal", "verify", "--capsule", str(tmp_path / "missing-run"))
    assert unknown.exit_code == 2
    d = tmp_path / "cap"
    d.mkdir()
    (d / "capsule.yaml").write_text(yaml.safe_dump({"schema_version": "0.2.0"}))
    no_anchor = _invoke("reseal", "verify", "--capsule", str(d))
    assert no_anchor.exit_code == 2 and "no facets.preservation" in _squash(no_anchor.stderr)
    (d / "capsule.yaml").write_text("key: [unclosed")
    assert _invoke("reseal", "verify", "--capsule", str(d)).exit_code == 2


def test_output_unwritable(tmp_path: Path) -> None:
    result = _invoke(
        "reseal", "record", "--facet", str(ANCHOR), *RESEAL_ARGS, "--original-sig-preserved",
        "-o", str(tmp_path / "no-such-dir" / "out.json"),
    )  # fmt: skip
    assert result.exit_code == 2 and "cannot write" in result.stderr


@pytest.mark.parametrize(
    ("group", "field", "stored"),
    [
        ("reseal", "crypto_migration", "not-a-list"),
        ("reseal", "crypto_migration", [{"original_sig_preserved": True}]),
        ("ltv", "ltv_renewal_chain", "not-a-list"),
        ("ltv", "ltv_renewal_chain", [{"renewal_type": "timestamp_renewal"}]),
    ],
)
def test_malformed_stored_record_is_broken_not_bad_input(
    tmp_path: Path, group: str, field: str, stored: object
) -> None:
    """A tampered stored record is broken evidence (exit 1), never a usage error (2)."""
    data = json.loads(ANCHOR.read_text())
    data[field] = stored
    bad = tmp_path / "tampered.json"
    bad.write_text(json.dumps(data))
    human = _invoke(group, "verify", "--facet", str(bad))
    assert human.exit_code == 1, human.stderr
    assert "BROKEN" in human.stderr and "malformed" in human.stderr and _boundary(human)
    as_json = _invoke(group, "verify", "--facet", str(bad), "--json")
    assert as_json.exit_code == 1
    payload = json.loads(as_json.stdout)
    assert payload["ok"] is False and "malformed" in payload["malformed_record"]


def test_append_onto_malformed_stored_record_is_broken(tmp_path: Path) -> None:
    data = json.loads(ANCHOR.read_text())
    data["crypto_migration"] = "not-a-list"
    data["ltv_renewal_chain"] = "not-a-list"
    bad = tmp_path / "tampered.json"
    bad.write_text(json.dumps(data))
    reseal = _invoke(
        "reseal", "record", "--facet", str(bad), *RESEAL_ARGS, "--original-sig-preserved"
    )
    assert reseal.exit_code == 1 and "malformed" in reseal.stderr and reseal.stdout == ""
    ltv = _invoke(
        "ltv", "append", "--facet", str(bad), "--type", "hash_tree_renewal",
        "--covered-digest", d256("e"), "--new-timestamp-ref", d256("t"),
        "--new-hash-alg", "sha256", "--renewed-before", "2030-01-01",
    )  # fmt: skip
    assert ltv.exit_code == 1 and "malformed" in ltv.stderr and ltv.stdout == ""


def test_reseal_verify_flags_within_tier_signature_downgrade(tmp_path: Path) -> None:
    data = json.loads(VALID.read_text())
    data["crypto_migration"][0]["to_alg"] = "ml-dsa-44"
    data["crypto_migration"][0]["from_alg"] = "ml-dsa-87"
    bad = tmp_path / "weaker.json"
    bad.write_text(json.dumps(data))
    result = _invoke("reseal", "verify", "--facet", str(bad), "--json")
    assert result.exit_code == 1
    codes = {f["code"] for f in json.loads(result.stdout)["findings"]}
    assert codes == {"signature_alg_downgrade"}


def test_reseal_record_refuses_trailing_newline_ref() -> None:
    result = _invoke(
        "reseal", "record", "--facet", str(VALID), "--to-alg", "ml-dsa-87",
        "--upgrade-ref", "op-2", "--renewal-timestamp-ref", "ts-1\n",
        "--resealed-at", "2031-01-01T00:00:00Z", "--original-sig-preserved",
    )  # fmt: skip
    assert result.exit_code == 2 and result.stdout == ""
