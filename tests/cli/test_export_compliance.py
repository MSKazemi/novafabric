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
"""CLI smoke tests for the ``nova export-compliance`` cohort.

Thin wrappers over the shipped compliance exporters (ADR-0107). Each subcommand reads
its input (a capsule directory or a JSON file) and writes a report JSON.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from typer.testing import CliRunner

from novafabric.capture.finalize import evidence_digests
from novafabric.cli.export_compliance import export_compliance_app
from novafabric.compliance.export.provenance import build_capsule_ref
from novafabric.trust.novaseal import KeyConfig, NovaSeal
from novafabric.trust.novaseal.capsule_verify import capsule_seal_status

runner = CliRunner()


def _capsule(tmp_path: Path) -> Path:
    cap = tmp_path / "cap"
    cap.mkdir()
    (cap / "capsule.yaml").write_text("run_id: r1\nstatus: completed\n", encoding="utf-8")
    return cap


def test_genai_profile_from_capsule(tmp_path: Path) -> None:
    out = tmp_path / "profile.json"
    result = runner.invoke(
        export_compliance_app,
        ["genai-profile", str(_capsule(tmp_path)), "--evidence", "tool_permissions", "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    doc = json.loads(out.read_text())
    assert doc["base_run_id"] == "r1"
    assert len(doc["mappings"]) == 10


def _catalog(tmp_path: Path, *ids: str) -> Path:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps([{"control_id": i, "evidence_kind": "eval_gate"} for i in ids]),
        encoding="utf-8",
    )
    return catalog


def _iso(tmp_path: Path, *args: str) -> tuple[Any, Path]:
    out = tmp_path / "iso.json"
    result = runner.invoke(export_compliance_app, ["iso42001", *args, "--out", str(out)])
    return result, out


def test_iso42001_binds_evidenced_controls_to_the_real_capsule_digest(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    result, out = _iso(
        tmp_path, "--catalog", str(_catalog(tmp_path, "ISO42-8.4-AI-SYSTEM-IMPACT")),
        "--evidence", "eval_gate", "--capsule", str(cap),
    )
    assert result.exit_code == 0, result.output
    ctrl = json.loads(out.read_text())["controls"][0]
    assert ctrl["status"] == "evidenced"
    expected = build_capsule_ref(cap, "unused")
    assert expected is not None
    assert ctrl["evidence_ref"]["content_digest"] == expected.content_digest
    assert ctrl["evidence_ref"]["content_digest"] != "sha256:" + "0" * 64
    assert ctrl["evidence_ref"]["capsule_id"] == "r1"


def test_iso42001_unsealed_capsule_carries_no_seal_path(tmp_path: Path) -> None:
    result, out = _iso(
        tmp_path, "--catalog", str(_catalog(tmp_path, "C-1")),
        "--evidence", "eval_gate", "--capsule", str(_capsule(tmp_path)),
    )
    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text())["controls"][0]["evidence_ref"]["seal_envelope_path"] is None


def test_iso42001_capsule_id_alone_fails_closed(tmp_path: Path) -> None:
    # The pre-fix call shape: it attached an all-zero digest to a capsule nobody read.
    result, out = _iso(
        tmp_path, "--catalog", str(_catalog(tmp_path, "C-1")),
        "--evidence", "eval_gate", "--capsule-id", "run-123",
    )
    assert result.exit_code == 2
    assert isinstance(result.exception, SystemExit)
    assert "--capsule" in result.output
    assert not out.exists()


def test_iso42001_capsule_id_must_match_the_manifest(tmp_path: Path) -> None:
    result, out = _iso(
        tmp_path, "--catalog", str(_catalog(tmp_path, "C-1")), "--evidence", "eval_gate",
        "--capsule", str(_capsule(tmp_path)), "--capsule-id", "someone-else",
    )
    assert result.exit_code == 2
    assert isinstance(result.exception, SystemExit)
    assert not out.exists()


def test_iso42001_directory_without_manifest_exit_2(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result, out = _iso(
        tmp_path, "--catalog", str(_catalog(tmp_path, "C-1")),
        "--evidence", "eval_gate", "--capsule", str(empty),
    )
    assert result.exit_code == 2
    assert isinstance(result.exception, SystemExit)
    assert not out.exists()


def test_iso42001_symlinked_manifest_refused(tmp_path: Path) -> None:
    real = _capsule(tmp_path)
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "capsule.yaml").symlink_to(real / "capsule.yaml")
    result, out = _iso(
        tmp_path, "--catalog", str(_catalog(tmp_path, "C-1")),
        "--evidence", "eval_gate", "--capsule", str(linked),
    )
    assert result.exit_code == 2
    assert isinstance(result.exception, SystemExit)
    assert not out.exists()


def test_iso42001_duplicate_control_id_exit_2(tmp_path: Path) -> None:
    result, out = _iso(
        tmp_path, "--catalog", str(_catalog(tmp_path, "C-1", "C-1")),
        "--evidence", "eval_gate", "--capsule", str(_capsule(tmp_path)),
    )
    assert result.exit_code == 2
    assert isinstance(result.exception, SystemExit)
    assert not out.exists()


def test_iso42001_entry_without_control_id_is_an_error_not_a_traceback(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps([{"evidence_kind": "eval_gate"}]), encoding="utf-8")
    result, out = _iso(tmp_path, "--catalog", str(catalog))
    assert result.exit_code == 2
    assert isinstance(result.exception, SystemExit)
    assert not out.exists()


@pytest.mark.parametrize("manifest", ["run_id: [unclosed\n", "- just\n- a list\n"])
def test_iso42001_malformed_manifest_exit_2(tmp_path: Path, manifest: str) -> None:
    cap = tmp_path / "bad"
    cap.mkdir()
    (cap / "capsule.yaml").write_text(manifest, encoding="utf-8")
    result, out = _iso(
        tmp_path, "--catalog", str(_catalog(tmp_path, "C-1")),
        "--evidence", "eval_gate", "--capsule", str(cap),
    )
    assert result.exit_code == 2, result.output
    assert isinstance(result.exception, SystemExit), result.exception
    assert not out.exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_iso42001_fifo_manifest_exit_2(tmp_path: Path) -> None:
    cap = tmp_path / "fifo"
    cap.mkdir()
    os.mkfifo(cap / "capsule.yaml")
    result, out = _iso(
        tmp_path, "--catalog", str(_catalog(tmp_path, "C-1")),
        "--evidence", "eval_gate", "--capsule", str(cap),
    )
    assert result.exit_code == 2, result.output
    assert isinstance(result.exception, SystemExit), result.exception
    assert not out.exists()


def test_iso42001_non_object_entry_names_its_index(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps([{"control_id": "C-1"}, "C-2"]), encoding="utf-8")
    result, out = _iso(tmp_path, "--catalog", str(catalog))
    assert result.exit_code == 2, result.output
    assert isinstance(result.exception, SystemExit), result.exception
    assert "[1]" in result.output
    assert not out.exists()


def test_iso42001_standard_label_with_markup_characters(tmp_path: Path) -> None:
    result, out = _iso(tmp_path, "--catalog", str(_catalog(tmp_path, "C-1")), "--standard", "[/x] A")
    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text())["standard"] == "[/x] A"


def test_iso42001_standard_label(tmp_path: Path) -> None:
    cat = _catalog(tmp_path, "C-1")
    result, out = _iso(tmp_path, "--catalog", str(cat), "--standard", "AIUC-1 (user catalog)")
    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text())["standard"] == "AIUC-1 (user catalog)"
    result, out = _iso(tmp_path, "--catalog", str(cat))
    assert json.loads(out.read_text())["standard"] == "ISO/IEC 42001:2023"


def _sealed_capsule(tmp_path: Path) -> Path:
    """A capsule with a genuine NovaSeal ``.seal/`` (local key, no TSA)."""
    cap = tmp_path / "sealed"
    cap.mkdir()
    (cap / "model-calls.jsonl").write_text('{"model":"m"}\n', encoding="utf-8")
    manifest: dict[str, Any] = {"run_id": "r-sealed", "status": "success"}
    manifest["evidence_digests"] = evidence_digests(cap)
    (cap / "capsule.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "export-test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256())
    )
    key_path, cert_path = tmp_path / "seal.key", tmp_path / "seal.crt"
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    bundle = NovaSeal(
        config=KeyConfig(profile="local", key_path=str(key_path), cert_path=str(cert_path)),
        tsa_url="", db_path=str(tmp_path / "merkle.db"),
    ).seal(manifest)
    seal_dir = cap / ".seal"
    seal_dir.mkdir()
    (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
    (seal_dir / "manifest.dsse.tsr").write_bytes(bundle.tsr)
    (seal_dir / "log-entry.json").write_text(json.dumps(bundle.log_entry), encoding="utf-8")
    return cap


def _flat(output: str) -> str:
    """Rich wraps the error panel: drop borders and join lines before matching."""
    return " ".join(re.sub(r"[│╭╮╰╯─]", " ", output).split())


def _iso_capsule(tmp_path: Path, cap: Path) -> tuple[Any, Path]:
    return _iso(
        tmp_path, "--catalog", str(_catalog(tmp_path, "C-1")),
        "--evidence", "eval_gate", "--capsule", str(cap),
    )


def _assert_refused_for_seal(result: Any, out: Path, cap: Path) -> None:
    assert result.exit_code == 2, result.output
    assert isinstance(result.exception, SystemExit), result.exception
    assert "seal verification" in _flat(result.output)
    # rich wraps a long temp path mid-word (longer under xdist: popen-gwN), so compare
    # with all whitespace removed
    assert cap.name in "".join(_flat(result.output).split())
    assert not out.exists()


def test_iso42001_validly_sealed_capsule_carries_the_seal_path(tmp_path: Path) -> None:
    cap = _sealed_capsule(tmp_path)
    assert capsule_seal_status(cap) == "valid"
    result, out = _iso_capsule(tmp_path, cap)
    assert result.exit_code == 0, result.output
    ref = json.loads(out.read_text())["controls"][0]["evidence_ref"]
    assert ref["seal_envelope_path"] == ".seal/manifest.dsse"
    assert ref["capsule_id"] == "r-sealed"


def test_iso42001_sealed_then_tampered_capsule_is_refused(tmp_path: Path) -> None:
    # This used to export as "unsealed" with a digest of the edited bytes.
    cap = _sealed_capsule(tmp_path)
    with (cap / "model-calls.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"model":"edited"}\n')
    assert capsule_seal_status(cap) == "invalid"
    result, out = _iso_capsule(tmp_path, cap)
    _assert_refused_for_seal(result, out, cap)


def _independent_digest(cap: Path) -> str:
    """The reference digest, derived here from the bytes: file name then contents, in the
    documented order, for the files that exist (not read from the exporter)."""
    h = hashlib.sha256()
    for name in ("capsule.yaml", "tool-permission-events.jsonl", "model-calls.jsonl",
                 "tool-calls.jsonl"):  # fmt: skip
        if (cap / name).is_file():
            h.update(name.encode())
            h.update((cap / name).read_bytes())
    return "sha256:" + h.hexdigest()


def test_iso42001_valid_seal_binds_the_real_digest_of_the_sealed_bytes(tmp_path: Path) -> None:
    cap = _sealed_capsule(tmp_path)
    result, out = _iso_capsule(tmp_path, cap)
    assert result.exit_code == 0, result.output
    ref = json.loads(out.read_text())["controls"][0]["evidence_ref"]
    assert ref["content_digest"] == _independent_digest(cap)
    assert ref["seal_envelope_path"] == ".seal/manifest.dsse"
    # the seal says the bytes are intact; it says nothing about who signed them
    assert "signer" not in json.dumps(ref).lower()


def test_iso42001_unsealed_capsule_stays_distinctly_unsigned(tmp_path: Path) -> None:
    cap = _sealed_capsule(tmp_path)
    shutil.rmtree(cap / ".seal")
    assert capsule_seal_status(cap) == "absent"
    result, out = _iso_capsule(tmp_path, cap)
    assert result.exit_code == 0, result.output
    ref = json.loads(out.read_text())["controls"][0]["evidence_ref"]
    assert ref["seal_envelope_path"] is None
    assert ref["content_digest"] == _independent_digest(cap)


@pytest.mark.parametrize(
    "mutate",
    ["edit-model-calls", "edit-manifest", "delete-model-calls", "add-tool-calls",
     "add-tool-permission-events"],
)  # fmt: skip
def test_iso42001_any_change_to_a_digested_member_after_sealing_is_refused(
    tmp_path: Path, mutate: str
) -> None:
    """Every file the reference digest covers must be one the seal covers.

    Editing or deleting a sealed member breaks the binding. A *new* tool stream is not named
    by the seal, so the verifier reports it as unlisted (ADR-0251: derived files are not
    fatal), yet the exporter would still hash it into a digest that carries the seal path."""
    cap = _sealed_capsule(tmp_path)
    if mutate == "edit-model-calls":
        (cap / "model-calls.jsonl").write_text('{"model":"EDITED"}\n', encoding="utf-8")
    elif mutate == "edit-manifest":
        with (cap / "capsule.yaml").open("a", encoding="utf-8") as fh:
            fh.write("status_note: edited after sealing\n")
    elif mutate == "delete-model-calls":
        (cap / "model-calls.jsonl").unlink()
    elif mutate == "add-tool-calls":
        (cap / "tool-calls.jsonl").write_text('{"tool":"injected"}\n', encoding="utf-8")
    else:
        (cap / "tool-permission-events.jsonl").write_text('{"ev":"injected"}\n', encoding="utf-8")
    result, out = _iso_capsule(tmp_path, cap)
    _assert_refused_for_seal(result, out, cap)


def test_iso42001_derived_file_next_to_a_sealed_capsule_is_not_a_tamper(tmp_path: Path) -> None:
    """ADR-0251 §5: an unlisted derived artifact is reported, never fatal."""
    cap = _sealed_capsule(tmp_path)
    (cap / "report.html").write_text("<p>derived</p>", encoding="utf-8")
    result, out = _iso_capsule(tmp_path, cap)
    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text())["controls"][0]["evidence_ref"]["seal_envelope_path"]


def test_iso42001_fifo_seal_is_refused_without_blocking(tmp_path: Path) -> None:
    cap = _sealed_capsule(tmp_path)
    shutil.rmtree(cap / ".seal")
    os.mkfifo(cap / ".seal")
    assert capsule_seal_status(cap) == "invalid"
    result, out = _iso_capsule(tmp_path, cap)
    _assert_refused_for_seal(result, out, cap)


def test_iso42001_a_verifier_that_crashes_is_a_refusal_not_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seal that cannot be verified is not a valid seal (and not an absent one)."""
    import novafabric.trust.novaseal.capsule_verify as cv

    cap = _sealed_capsule(tmp_path)

    def boom(_dir: Path, **_kw: Any) -> None:
        raise RuntimeError("verifier exploded")

    monkeypatch.setattr(cv, "verify_capsule", boom)
    assert capsule_seal_status(cap) == "invalid"
    result, out = _iso_capsule(tmp_path, cap)
    _assert_refused_for_seal(result, out, cap)


def test_capsule_seal_verifies_keeps_its_boolean_contract(tmp_path: Path) -> None:
    from novafabric.trust.novaseal.capsule_verify import capsule_seal_verifies

    cap = _sealed_capsule(tmp_path)
    assert capsule_seal_verifies(cap) is True
    (cap / "model-calls.jsonl").write_text("{}\n", encoding="utf-8")
    assert capsule_seal_verifies(cap) is False  # invalid
    shutil.rmtree(cap / ".seal")
    assert capsule_seal_verifies(cap) is False  # absent


@pytest.mark.parametrize("garbage", [b"", b"not a dsse envelope"])
def test_iso42001_garbage_seal_is_refused(tmp_path: Path, garbage: bytes) -> None:
    cap = _capsule(tmp_path)
    (cap / ".seal").mkdir()
    (cap / ".seal" / "manifest.dsse").write_bytes(garbage)
    result, out = _iso_capsule(tmp_path, cap)
    _assert_refused_for_seal(result, out, cap)


def test_iso42001_empty_seal_directory_is_refused(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    (cap / ".seal").mkdir()
    result, out = _iso_capsule(tmp_path, cap)
    _assert_refused_for_seal(result, out, cap)


def test_iso42001_seal_that_is_a_file_is_refused(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    (cap / ".seal").write_text("{}", encoding="utf-8")
    result, out = _iso_capsule(tmp_path, cap)
    _assert_refused_for_seal(result, out, cap)


@pytest.mark.parametrize("target", ["valid", "dangling"])
def test_iso42001_symlinked_seal_is_refused_not_treated_as_absent(
    tmp_path: Path, target: str
) -> None:
    # A link is never followed: even one to a genuine seal is not this capsule's seal,
    # and a dangling one must not read as "no seal".
    cap = _capsule(tmp_path)
    if target == "valid":
        (cap / ".seal").symlink_to(_sealed_capsule(tmp_path) / ".seal")
    else:
        (cap / ".seal").symlink_to(tmp_path / "nowhere")
    assert capsule_seal_status(cap) == "invalid"
    result, out = _iso_capsule(tmp_path, cap)
    _assert_refused_for_seal(result, out, cap)


def test_capsule_seal_status_absent(tmp_path: Path) -> None:
    assert capsule_seal_status(_capsule(tmp_path)) == "absent"
    assert capsule_seal_status(tmp_path / "missing") == "absent"


def test_iso42001_without_capsule_degrades_to_not_evidenced(tmp_path: Path) -> None:
    result, out = _iso(
        tmp_path, "--catalog", str(_catalog(tmp_path, "ISO42-8.4-AI-SYSTEM-IMPACT")),
        "--evidence", "eval_gate",
    )
    assert result.exit_code == 0, result.output
    # No capsule → the exporter honestly refuses to claim "evidenced".
    ctrl = json.loads(out.read_text())["controls"][0]
    assert ctrl["status"] == "not_evidenced"
    assert ctrl["evidence_ref"] is None


def test_gpai53_from_fields(tmp_path: Path) -> None:
    fields = tmp_path / "fields.json"
    fields.write_text(json.dumps({"general_description": "A GPAI model."}), encoding="utf-8")
    out = tmp_path / "form.json"
    result = runner.invoke(
        export_compliance_app,
        ["gpai53", "--model", "gpai-x", "--fields", str(fields), "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    doc = json.loads(out.read_text())
    assert doc["model_name"] == "gpai-x"
    assert len(doc["revisions"]) == 1
    assert doc["revisions"][0]["content_digest"].startswith("sha256:")


def test_pmm_from_findings(tmp_path: Path) -> None:
    findings = tmp_path / "findings.json"
    findings.write_text(
        json.dumps(
            [
                {
                    "metric": "hallucination_rate",
                    "trend": "degrading",
                    "severity": "high",
                    "description": "rising",
                    "incident_classification": "widespread_infringement",
                }
            ]
        ),
        encoding="utf-8",
    )
    out = tmp_path / "pmm.json"
    result = runner.invoke(
        export_compliance_app,
        [
            "pmm",
            "--system", "triage-agent",
            "--findings", str(findings),
            "--period-start", "2026-06-01",
            "--period-end", "2026-07-01",
            "--occurred-at", "2026-07-01",
            "--out", str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    doc = json.loads(out.read_text())
    assert len(doc["referred_incidents"]) == 1


def test_pmm_serious_finding_without_classification_errors(tmp_path: Path) -> None:
    findings = tmp_path / "findings.json"
    findings.write_text(
        json.dumps([{"metric": "m", "trend": "degrading", "severity": "critical", "description": "d"}]),
        encoding="utf-8",
    )
    result = runner.invoke(
        export_compliance_app,
        ["pmm", "--system", "s", "--findings", str(findings), "--occurred-at", "2026-07-01"],
    )
    assert result.exit_code != 0


def test_stdout_when_no_out(tmp_path: Path) -> None:
    fields = tmp_path / "fields.json"
    fields.write_text(json.dumps({"general_description": "A GPAI model."}), encoding="utf-8")
    result = runner.invoke(
        export_compliance_app,
        ["gpai53", "--model", "gpai-x", "--fields", str(fields)],
    )
    assert result.exit_code == 0, result.output
    assert "revisions" in result.output
