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
"""End-to-end crypto-shredding: capture encrypts, `nova pii erase` shreds (ADR-0314).

The defect this pins: `nova pii erase` destroyed a DEK that no capture path had ever
used, so captured PII stayed readable after "erasure". Each test drives a real
``nova capture`` workload whose argv and stdout carry the subject's PII.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import jsonschema
import pytest
from typer.testing import CliRunner

from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.cli.main import app
from novafabric.pii.capture_stage import SubjectEnvelopeStage
from novafabric.pii.dek import DEKStore
from novafabric.pii.envelope import ERASED, TOKEN_RE, EnvelopeKeyring, read_capsule_file

PEPPER = "e2e-test-pepper"
S = "alice.smith@example.com"
S_PHONE = "555-123-4567"
T = "bob.jones@example.org"

runner = CliRunner()


def _workload(subject: str, phone: str) -> list[str]:
    code = f"print('contact {subject} or call {phone}')"
    return [sys.executable, "-c", code]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("NOVAFABRIC_HOME", str(h))
    monkeypatch.setenv("NOVA_PII_PEPPER", PEPPER)
    return h


def _capture(home: Path, out: Path, subject: str, phone: str, factory=None):
    stage = SubjectEnvelopeStage(
        subject,
        pepper=PEPPER.encode(),
        dek_store_factory=factory or (lambda: DEKStore(home / "dek.db")),
    )
    orch = CaptureOrchestrator(base_dir=out, masking_pipeline=stage)
    return orch.run(command=_workload(subject, phone))


def _all_bytes(capsule_dir: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(capsule_dir.rglob("*")) if p.is_file())


def _stdout(capsule_dir: Path, run_id: str, home: Path) -> str:
    keyring = EnvelopeKeyring.from_home(home, PEPPER.encode())
    try:
        return str(read_capsule_file(capsule_dir, "outputs/stdout.txt", keyring=keyring,
                                     run_id=run_id))
    finally:
        keyring.close()


def _erase(home: Path, subject: str):
    return runner.invoke(
        app, ["pii", "erase", subject, "--retention-months", "0"],
        env={"NOVAFABRIC_HOME": str(home)},
    )


# -- (a) no plaintext anywhere in the capsule directory ------------------------


def test_a_retained_pii_is_not_in_plaintext_anywhere_in_the_capsule(
    home: Path, tmp_path: Path
) -> None:
    result = _capture(home, tmp_path / "runs", S, S_PHONE)
    assert result.exit_code == 0
    blob = _all_bytes(result.capsule_dir)
    assert S.encode() not in blob
    assert S_PHONE.encode() not in blob
    stdout = (result.capsule_dir / "outputs" / "stdout.txt").read_text()
    assert len(TOKEN_RE.findall(stdout)) == 2
    manifest = json.loads((result.capsule_dir / "redaction_manifest.json").read_text())
    assert manifest["capsule_id"] == result.run_id
    assert {e["protection"] for e in manifest["entries"]} == {"envelope"}


# -- (b) before erasure an authorised reader decrypts --------------------------


def test_b_authorised_reader_decrypts_before_erasure(home: Path, tmp_path: Path) -> None:
    result = _capture(home, tmp_path / "runs", S, S_PHONE)
    assert _stdout(result.capsule_dir, result.run_id, home) == (
        f"contact {S} or call {S_PHONE}\n"
    )
    trace = read_capsule_file(
        result.capsule_dir, "trace.jsonl",
        keyring=EnvelopeKeyring.from_home(home, PEPPER.encode()), run_id=result.run_id,
    )
    assert any(S in json.dumps(rec) for rec in trace)


# -- (c) after `nova pii erase S` it is unrecoverable and reads as [erased] -----


def test_c_after_erase_the_pii_is_unrecoverable_and_renders_erased(
    home: Path, tmp_path: Path
) -> None:
    result = _capture(home, tmp_path / "runs", S, S_PHONE)
    store = DEKStore(home / "dek.db")
    dek = store.get_dek(S)
    store.close()
    assert dek is not None

    erased = _erase(home, S)
    assert erased.exit_code == 0, erased.output
    assert '"method": "aes-256-gcm-dek-destruction"' in erased.output

    assert _stdout(result.capsule_dir, result.run_id, home) == (
        f"contact {ERASED} or call {ERASED}\n"
    )
    # The ciphertext is still there (WORM-friendly), the plaintext is not ...
    assert S.encode() not in _all_bytes(result.capsule_dir)
    # ... and the key bytes are gone from every file of the DEK store.
    key_store_bytes = b"".join(p.read_bytes() for p in home.glob("dek.db*"))
    assert dek.dek_hex.encode() not in key_store_bytes


def test_c_pii_status_reports_the_subject_erased(home: Path, tmp_path: Path) -> None:
    result = _capture(home, tmp_path / "runs", S, S_PHONE)
    before = runner.invoke(app, ["pii", "status", str(result.capsule_dir), "--json"],
                           env={"NOVAFABRIC_HOME": str(home), "NOVA_PII_PEPPER": PEPPER})
    assert before.exit_code == 0, before.output
    assert json.loads(before.output)["subjects"][0]["dek_state"] == "active"
    assert _erase(home, S).exit_code == 0
    after = runner.invoke(app, ["pii", "status", str(result.capsule_dir), "--json"],
                          env={"NOVAFABRIC_HOME": str(home), "NOVA_PII_PEPPER": PEPPER})
    assert json.loads(after.output)["subjects"][0]["dek_state"] == "erased"


# -- (d) erasing S leaves subject T readable ------------------------------------


def test_d_erasing_one_subject_leaves_another_readable(home: Path, tmp_path: Path) -> None:
    rs = _capture(home, tmp_path / "runs-s", S, S_PHONE)
    rt = _capture(home, tmp_path / "runs-t", T, "555-987-6543")
    assert _erase(home, S).exit_code == 0
    assert ERASED in _stdout(rs.capsule_dir, rs.run_id, home)
    assert _stdout(rt.capsule_dir, rt.run_id, home) == f"contact {T} or call 555-987-6543\n"


# -- (f) DEK store failure falls back to masking, never plaintext ---------------


def test_f_dek_store_failure_masks_and_never_stores_plaintext(
    home: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def broken() -> DEKStore:
        raise OSError("dek.db is on a read-only volume")

    caplog.set_level(logging.DEBUG)
    result = _capture(home, tmp_path / "runs", S, S_PHONE, factory=broken)
    assert result.exit_code == 0, "a DEK failure must not block the workload"
    blob = _all_bytes(result.capsule_dir)
    assert S.encode() not in blob and S_PHONE.encode() not in blob
    stdout = (result.capsule_dir / "outputs" / "stdout.txt").read_text()
    assert "[REDACTED:sha256:" in stdout and not TOKEN_RE.search(stdout)

    proof = json.loads((result.capsule_dir / "redaction-proof.json").read_text())
    errs = [e for e in proof["masker_errors"] if e["masker_id"] == "novafabric.pii.envelope"]
    assert errs and {e["action_taken"] for e in errs} == {"redact"}
    schema = json.loads(
        (Path(__file__).resolve().parents[2] / "schemas" / "masker-error.schema.json").read_text()
    )
    for err in errs:
        jsonschema.validate(err, schema)
    manifest = json.loads((result.capsule_dir / "redaction_manifest.json").read_text())
    assert {e["protection"] for e in manifest["entries"]} == {"mask"}

    warnings = [r for r in caplog.records if getattr(r, "event", "") ==
                "pii_envelope_dek_unavailable"]
    assert len(warnings) == 1
    for rec in caplog.records:
        rendered = rec.getMessage() + json.dumps(rec.__dict__, default=str)
        assert S not in rendered and S_PHONE not in rendered


# -- the CLI surface -------------------------------------------------------------


def test_cli_pii_subject_encrypts_and_requires_a_pepper(home: Path, tmp_path: Path) -> None:
    out = tmp_path / "cli-runs"
    argv = ["capture", "--pii-subject", S, "-o", str(out), "--", *_workload(S, S_PHONE)]
    refused = runner.invoke(app, argv, env={"NOVAFABRIC_HOME": str(home),
                                            "NOVA_PII_PEPPER": ""})
    assert refused.exit_code == 2
    assert "NOVA_PII_PEPPER" in refused.output
    assert not out.exists() or not any(out.rglob("capsule.yaml"))

    ok = runner.invoke(app, argv, env={"NOVAFABRIC_HOME": str(home),
                                       "NOVA_PII_PEPPER": PEPPER})
    assert ok.exit_code == 0, ok.output
    (capsule,) = [p.parent for p in out.rglob("capsule.yaml")]
    assert S.encode() not in _all_bytes(capsule)
    assert (home / "dek.db").is_file()
