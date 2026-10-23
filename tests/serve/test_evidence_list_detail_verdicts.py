"""``GET /api/evidence`` and ``GET /api/evidence/{id}``: real verdicts, bounded reads.

Adversarial review, 2026-10-23 (attack A3):

* the list route's ``verified`` flag compared a bare SHA-256 with the
  ``sha256:``-prefixed ``manifest_hash``, so it was ``false`` for every bundle — and
  even a fixed comparison would only have re-checked a self-hash anyone can recompute;
* list and detail read ``manifest.json`` whole: a ~300 KB ZIP inflating to 300 MB of
  whitespace was read into memory on every list call;
* both reported the manifest's unsigned top-level ``run_id``.

``verified`` is now the verdict of the shared verifier (the one behind ``nova verify``
and ``POST /api/evidence/{id}/verify``) under the operator trust list, cached per
file identity; ``run_id`` is the signed run when exactly one verifies.
"""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from novafabric.evidence.bundle import EvidenceBundleBuilder, _canonical_manifest_hash
from novafabric.evidence.signing import LocalSigner, generate_keypair

TOKEN = "evidence-list-token"
HOST = {"host": "127.0.0.1:4321"}
AUTH = {"token": TOKEN}
RUN_ID = "01HEVIDENCELISTDETAIL00000"
VICTIM = "victim-run"


def _capsule(root: Path) -> Path:
    capsule = root / RUN_ID
    (capsule / "outputs").mkdir(parents=True)
    (capsule / "inputs").mkdir()
    (capsule / "outputs" / "stdout.txt").write_text("hello\n")
    (capsule / "env.lock").write_text("python=3.13\n")
    (capsule / "capsule.yaml").write_text(
        yaml.safe_dump({"run_id": RUN_ID, "status": "success", "command": ["true"]})
    )
    (capsule / "redaction-proof.json").write_text(
        json.dumps({
            "findings": [], "unsafe_skips": [], "proof_id": "p1",
            "chain_hash": "sha256:" + "a" * 64, "findings_count": {"total": 0},
            "scanner": {"name": "test"},
        })
    )
    for name in ("trace.jsonl", "model-calls.jsonl", "tool-calls.jsonl", "assets.jsonl"):
        (capsule / name).write_text("")
    return capsule


def _relabel(src: Path, dst: Path) -> None:
    with zipfile.ZipFile(src) as zf:
        members = {n: zf.read(n) for n in zf.namelist()}
    manifest = json.loads(members.pop("manifest.json"))
    manifest["run_id"] = VICTIM
    manifest["manifest_hash"] = _canonical_manifest_hash(manifest)
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
        for name, data in members.items():
            zf.writestr(name, data)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    home = tmp_path / "home"
    home.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(home / "audit.jsonl"))
    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_DIR", str(evidence))
    for name in ("NOVAFABRIC_SEAL_CONFIG", "NOVAFABRIC_SEAL_DB_PATH", "NOVAFABRIC_CAPSULE_DIR"):
        monkeypatch.delenv(name, raising=False)
    priv, pub = generate_keypair(tmp_path / "keys")
    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_TRUSTED_KEYS", str(pub))
    src = tmp_path / "src"
    genuine = EvidenceBundleBuilder(
        _capsule(src), LocalSigner(priv), evidence / "genuine.zip",
        audit_log_path=tmp_path / "audit.jsonl",
    ).build()
    _relabel(genuine, evidence / "relabel.zip")
    runs = tmp_path / "runs"
    runs.mkdir()
    client = TestClient(
        create_app_for(runs, tmp_path), raise_server_exceptions=False
    )
    return {"client": client, "evidence": evidence, "tmp": tmp_path}


def create_app_for(runs: Path, tmp: Path) -> Any:
    from novafabric.serve.app import create_app

    return create_app(token=TOKEN, capsule_dir=runs, db_path=tmp / "r.db")


def _listed(client: TestClient) -> dict[str, dict[str, Any]]:
    r = client.get("/api/evidence", params=AUTH, headers=HOST)
    assert r.status_code == 200, r.text
    return {b["bundle_id"]: b for b in r.json()["bundles"]}


def test_list_verified_is_the_real_verifier_verdict(env: dict[str, Any]) -> None:
    listed = _listed(env["client"])
    assert listed["genuine"]["verified"] is True
    assert listed["genuine"]["run_id"] == RUN_ID
    assert listed["genuine"]["run_ids"] == [RUN_ID]
    assert listed["genuine"]["signer_trust"] == "anchored"
    assert listed["relabel"]["verified"] is False
    assert listed["relabel"]["run_id"] != VICTIM


def test_detail_reports_the_signed_run_and_the_verdict(env: dict[str, Any]) -> None:
    client = env["client"]
    genuine = client.get("/api/evidence/genuine", params=AUTH, headers=HOST).json()
    assert genuine["run_id"] == RUN_ID
    assert genuine["verified"] is True
    relabel = client.get("/api/evidence/relabel", params=AUTH, headers=HOST).json()
    assert relabel["run_id"] != VICTIM
    assert relabel["verified"] is False
    assert relabel["manifest"]["run_id"] == VICTIM  # shown as data, never as the run


def test_a_bundle_edited_in_place_with_its_mtime_restored_is_reverified(
    env: dict[str, Any],
) -> None:
    client = env["client"]
    assert _listed(client)["genuine"]["verified"] is True
    path = env["evidence"] / "genuine.zip"
    st = path.stat()
    # Same inode, same size, same mtime: only ctime tells the cache it changed.
    data = bytearray(path.read_bytes())
    data[len(data) // 3] ^= 0xFF
    with open(path, "r+b") as handle:
        handle.write(bytes(data))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    after = path.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        st.st_ino, st.st_size, st.st_mtime_ns
    )
    assert _listed(client)["genuine"]["verified"] is False


def test_an_oversized_manifest_is_never_read_whole(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.evidence import bundle_verify

    monkeypatch.setattr(bundle_verify, "MAX_MANIFEST_BYTES", 4096)
    bomb = env["evidence"] / "bomb.zip"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", "{" + " " * 65536 + '"run_id": "bomb"}')
    client = env["client"]
    listed = _listed(client)
    assert listed["bomb"]["verified"] is False
    assert listed["bomb"]["run_id"] == "bomb"  # the bundle id, not the manifest's label
    detail = client.get("/api/evidence/bomb", params=AUTH, headers=HOST)
    assert detail.status_code == 413, detail.text
    verify = client.post("/api/evidence/bomb/verify", params=AUTH, headers=HOST)
    assert verify.status_code == 413, verify.text

