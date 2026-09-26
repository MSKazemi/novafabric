"""CLI: ``nova science receipt build|verify`` and ``nova export-rocrate-science``.

Covers every subcommand, the exit-code contract (0 ok / 1 refused-or-failed /
2 usage), the in-mission-boundary line on every output, and ``--help`` smoke.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.science import digest_node
from novafabric.science.provenance import FACET_NAME
from novafabric.science.reproducibility import RECEIPT_KEY

from .conftest import FULL_RECEIPT, RUN_ID, SEALED_ROOT

runner = CliRunner()
BOUNDARY = "never runs experiments"
ENV = digest_node("env")
CODE = digest_node("code")
DATA = digest_node("data")


def _invoke(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, list(args))
    return result.exit_code, result.output


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.parametrize(
    "cmd",
    [
        ["science", "--help"],
        ["science", "receipt", "--help"],
        ["science", "receipt", "build", "--help"],
        ["science", "receipt", "verify", "--help"],
        ["export-rocrate-science", "--help"],
    ],
)
def test_help_smoke(cmd: list[str]) -> None:
    code, out = _invoke(*cmd)
    assert code == 0 and "Usage" in out


# ── receipt build ──────────────────────────────────────────────────────────


def test_build_without_write_does_not_touch_manifest(
    capsule_factory: Callable[..., Path],
) -> None:
    capsule = capsule_factory()
    before = (capsule / "capsule.yaml").read_bytes()
    code, out = _invoke("science", "receipt", "build", "--capsule", str(capsule), "--env", ENV)
    assert code == 0
    assert "Not written" in out and BOUNDARY in _flat(out)
    assert "seeds" in out  # receipt_incomplete surfaced
    assert (capsule / "capsule.yaml").read_bytes() == before


def test_build_write_then_verify_round_trip(capsule_factory: Callable[..., Path]) -> None:
    capsule = capsule_factory()
    code, out = _invoke(
        "science", "receipt", "build", "--capsule", str(capsule),
        "--env", ENV, "--data", DATA, "--code", CODE,
        "--seed", "1337", "--seed", "42", "--determinism", "statistical", "--write",
    )  # fmt: skip
    # The fixture capsule carries .seal/: --write refuses without --force-unseal.
    assert code == 2, out
    assert "--force-unseal" in _flat(out)
    code, out = _invoke(
        "science", "receipt", "build", "--capsule", str(capsule),
        "--env", ENV, "--data", DATA, "--code", CODE,
        "--seed", "1337", "--seed", "42", "--determinism", "statistical", "--write",
        "--force-unseal",
    )  # fmt: skip
    assert code == 0, out
    assert "Wrote" in out and "no longer matches" in _flat(out)
    manifest = yaml.safe_load((capsule / "capsule.yaml").read_text())
    receipt = manifest["facets"][FACET_NAME][RECEIPT_KEY]
    assert receipt["seeds"] == [1337, 42]
    assert receipt["capsule_root"] == SEALED_ROOT  # bound to the sealed science root
    # The P1 DAG survives the write.
    assert manifest["facets"][FACET_NAME]["hypothesis_experiment_result"]

    code, out = _invoke("science", "receipt", "verify", "--capsule", str(capsule))
    assert code == 0, out
    assert "reproducible_in_fact: null" in out and BOUNDARY in _flat(out)

    code, out = _invoke("science", "receipt", "verify", "--capsule", str(capsule), "--strict")
    assert code == 0


def test_build_json_output(capsule_factory: Callable[..., Path]) -> None:
    capsule = capsule_factory(with_facet=False)
    code, out = _invoke(
        "science", "receipt", "build", "--capsule", str(capsule), "--code", CODE, "--json"
    )
    assert code == 0
    body = json.loads(out)
    assert body["code_digest"] == CODE
    assert "capsule_root" not in body
    assert body["receipt_incomplete"] == ["environment_digest", "seeds", "data_digest"]


@pytest.mark.parametrize(
    "extra",
    [["--env", "sha256:nothex"], ["--determinism", "mostly"], ["--code", "x" * 600]],
)
def test_build_usage_errors_exit_2(capsule_factory: Callable[..., Path], extra: list[str]) -> None:
    capsule = capsule_factory()
    code, _ = _invoke("science", "receipt", "build", "--capsule", str(capsule), *extra)
    assert code == 2


def test_unknown_capsule_exits_2(tmp_path: Path) -> None:
    code, _ = _invoke("science", "receipt", "verify", "--capsule", str(tmp_path / "nope" / "x"))
    assert code == 2


@pytest.mark.parametrize("content", ["- a list\n", ": : bad : ["])
def test_unreadable_manifest_exits_2(tmp_path: Path, content: str) -> None:
    capsule = tmp_path / "c"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text(content)
    code, _ = _invoke("science", "receipt", "verify", "--capsule", str(capsule))
    assert code == 2


# ── receipt verify ─────────────────────────────────────────────────────────


def test_verify_without_receipt_exits_1(capsule_factory: Callable[..., Path]) -> None:
    capsule = capsule_factory()
    code, out = _invoke("science", "receipt", "verify", "--capsule", str(capsule))
    assert code == 1 and "No reproducibility_receipt" in out and BOUNDARY in _flat(out)
    code, out = _invoke("science", "receipt", "verify", "--capsule", str(capsule), "--json")
    assert code == 1 and json.loads(out) == {"reproducibility_receipt": None}


def test_verify_tampered_exits_1(capsule_factory: Callable[..., Path]) -> None:
    capsule = capsule_factory(receipt_kwargs=FULL_RECEIPT)
    path = capsule / "capsule.yaml"
    manifest = yaml.safe_load(path.read_text())
    manifest["facets"][FACET_NAME][RECEIPT_KEY]["data_digest"] = digest_node("swap")
    path.write_text(yaml.safe_dump(manifest))
    code, out = _invoke("science", "receipt", "verify", "--capsule", str(capsule))
    assert code == 1 and "MISMATCH" in out
    code, out = _invoke("science", "receipt", "verify", "--capsule", str(capsule), "--json")
    body = json.loads(out)
    assert code == 1 and body["sealed_into_root"] is False
    assert body["re_executed"] is False and body["reproducible_in_fact"] is None


def test_verify_malformed_exits_1(capsule_factory: Callable[..., Path]) -> None:
    capsule = capsule_factory(receipt_kwargs=FULL_RECEIPT)
    path = capsule / "capsule.yaml"
    manifest = yaml.safe_load(path.read_text())
    manifest["facets"][FACET_NAME][RECEIPT_KEY]["bound_root"] = "nope"
    path.write_text(yaml.safe_dump(manifest))
    code, out = _invoke("science", "receipt", "verify", "--capsule", str(capsule))
    assert code == 1 and "Malformed" in out


def test_verify_strict_fails_incomplete(capsule_factory: Callable[..., Path]) -> None:
    capsule = capsule_factory(receipt_kwargs={"environment_digest": ENV})
    code, _ = _invoke("science", "receipt", "verify", "--capsule", str(capsule))
    assert code == 0  # incompleteness is reported, not a failure, by default
    code, out = _invoke(
        "science", "receipt", "verify", "--capsule", str(capsule), "--strict", "--json"
    )
    assert code == 1 and json.loads(out)["complete"] is False


# ── export-rocrate-science ─────────────────────────────────────────────────


def test_export_writes_crate_and_binding(
    capsule_factory: Callable[..., Path], tmp_path: Path
) -> None:
    capsule = capsule_factory(receipt_kwargs=FULL_RECEIPT)
    out_dir = tmp_path / "crates"
    code, out = _invoke(
        "export-rocrate-science", "--capsule", str(capsule), "--out", str(out_dir),
        "--orcid", "0000-0002-1825-0097", "--ror", "03yrm5c26", "--doi", "10.5281/zenodo.1",
    )  # fmt: skip
    assert code == 0, out
    assert "verdict: null" in out and "w3c-prov" in out and BOUNDARY in _flat(out)
    crate = out_dir / f"{RUN_ID}.science.rocrate.zip"
    assert zipfile.is_zipfile(crate)
    assert (out_dir / f"{RUN_ID}.fair-binding.json").is_file()


def test_export_defaults_to_capsule_parent_and_json(
    capsule_factory: Callable[..., Path],
) -> None:
    capsule = capsule_factory(bound_root=None)
    code, out = _invoke("export-rocrate-science", "--capsule", str(capsule), "--json")
    assert code == 0, out
    body = json.loads(out)
    assert body["prov_alignment"] == "w3c-prov" and body["verdict"] is None
    assert body["unbound"] == ["sealed_root", "reproducibility_receipt"]
    assert (capsule.parent / f"{RUN_ID}.science.rocrate.zip").is_file()


def test_export_non_science_capsule_exits_1(capsule_factory: Callable[..., Path]) -> None:
    capsule = capsule_factory(with_facet=False)
    code, out = _invoke("export-rocrate-science", "--capsule", str(capsule))
    assert code == 1 and BOUNDARY in _flat(out)


def test_export_workflow_profile_without_workflow_exits_1(
    capsule_factory: Callable[..., Path],
) -> None:
    capsule = capsule_factory(receipt_kwargs=FULL_RECEIPT)
    code, _ = _invoke(
        "export-rocrate-science", "--capsule", str(capsule), "--profile", "workflow-run-crate"
    )
    assert code == 1


@pytest.mark.parametrize(
    "extra", [["--profile", "provenance-run-crate"], ["--orcid", "1234"], ["--doi", "nope"]]
)
def test_export_usage_errors_exit_2(capsule_factory: Callable[..., Path], extra: list[str]) -> None:
    capsule = capsule_factory(receipt_kwargs=FULL_RECEIPT)
    code, _ = _invoke("export-rocrate-science", "--capsule", str(capsule), *extra)
    assert code == 2


def test_export_unknown_capsule_exits_2(tmp_path: Path) -> None:
    code, _ = _invoke("export-rocrate-science", "--capsule", str(tmp_path / "a" / "b"))
    assert code == 2


def test_export_human_output_names_unbound(capsule_factory: Callable[..., Path]) -> None:
    capsule = capsule_factory(bound_root=None)
    code, out = _invoke("export-rocrate-science", "--capsule", str(capsule))
    assert code == 0, out
    assert "sealed_root: unbound" in out
    assert "unbound: sealed_root, reproducibility_receipt" in out
