"""An Evidence Bundle's runs are the ones its signed statements name — nothing else.

Adversarial review, 2026-10-23 (attack A2). ``manifest.json`` is unsigned and its
``manifest_hash`` is a self-hash anyone can recompute, yet a bundle signed by a
*trusted* key stayed ``valid`` / ``anchored`` after its manifest was edited:

* a fabricated ``subject`` entry naming another run was accepted;
* in a capsule-set bundle, run-b's files, statements and signatures could be
  stripped while its ``subject`` label stayed;
* ``POST /api/evidence/{id}/verify`` reported the unsigned top-level ``run_id`` of a
  set bundle (``BundleCheck.run_id`` fell back to it whenever more than one run
  statement verified).

Every bundle here is built by the real builders from real captured capsules and
signed with a real Ed25519 key that the verifier trusts.
"""

from __future__ import annotations

import json
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.evidence.bundle import (
    CapsuleSetBundleBuilder,
    EvidenceBundleBuilder,
    _canonical_manifest_hash,
)
from novafabric.evidence.bundle_verify import (
    BundleVerification,
    load_trusted_public_keys,
    verify_bundle_archive,
)
from novafabric.evidence.signing import LocalSigner, generate_keypair
from novafabric.serve.evidence_verify import BundleCheck

TOKEN = "bundle-runs-token"
HOST = {"host": "127.0.0.1:4321"}
VICTIM = "victim-run"

Edit = Callable[[dict[str, Any]], None]


def _capsule(root: Path, name: str) -> tuple[Path, str]:
    capsule = (
        CaptureOrchestrator(base_dir=root / f"runs-{name}")
        .run(command=[sys.executable, "-c", "print('hi')"])
        .capsule_dir
    )
    run_id = yaml.safe_load((capsule / "capsule.yaml").read_text())["run_id"]
    return capsule, run_id


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("bundle-runs")
    mp = pytest.MonkeyPatch()
    mp.setenv("NOVAFABRIC_HOME", str(root / "home"))
    mp.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(root / "home" / "audit.jsonl"))
    for name in ("NOVAFABRIC_SEAL_CONFIG", "NOVAFABRIC_SEAL_DB_PATH", "NOVAFABRIC_CAPSULE_DIR"):
        mp.delenv(name, raising=False)
    try:
        cap_a, run_a = _capsule(root, "a")
        cap_b, run_b = _capsule(root, "b")
        priv, pub = generate_keypair(root / "keys")
        signer = LocalSigner(priv)
        single = EvidenceBundleBuilder(
            cap_a, signer, root / "single.zip", audit_log_path=root / "audit.jsonl"
        ).build()
        capsule_set = CapsuleSetBundleBuilder([cap_a, cap_b], signer, root / "set.zip").build()
    finally:
        mp.undo()
    return {
        "root": root,
        "single": single,
        "set": capsule_set,
        "run_a": run_a,
        "run_b": run_b,
        "pub": pub,
        "trusted": load_trusted_public_keys(pub),
    }


def _rewrite(
    src: Path, dst: Path, edit: Edit | None = None, drop: Callable[[str], bool] | None = None
) -> Path:
    """Copy bundle *src* to *dst*, editing the unsigned manifest and recomputing its self-hash."""
    with zipfile.ZipFile(src) as zf:
        members = {n: zf.read(n) for n in zf.namelist()}
    manifest = json.loads(members.pop("manifest.json"))
    if edit is not None:
        edit(manifest)
    if drop is not None:
        members = {n: b for n, b in members.items() if not drop(n)}
    manifest["manifest_hash"] = _canonical_manifest_hash(manifest)
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
        for name, data in members.items():
            zf.writestr(name, data)
    return dst


def _verify(path: Path, trusted: list[bytes]) -> BundleVerification:
    with zipfile.ZipFile(path) as zf:
        return verify_bundle_archive(zf, trusted_public_pems=trusted)


def _subject_problems(outcome: BundleVerification) -> str:
    return "\n".join(outcome.signatures.bad_subjects)


# ── genuine bundles ─────────────────────────────────────────────────────────


def test_genuine_bundles_verify_and_report_only_signed_runs(built: dict[str, Any]) -> None:
    single = _verify(built["single"], built["trusted"])
    assert single.ok, single.problems()
    assert single.signer_trust == "anchored"
    assert BundleCheck(single).run_id == built["run_a"]
    assert BundleCheck(single).run_ids == [built["run_a"]]

    capsule_set = _verify(built["set"], built["trusted"])
    assert capsule_set.ok, capsule_set.problems()
    check = BundleCheck(capsule_set)
    assert check.run_id is None  # two runs: no single run id to report
    assert sorted(check.run_ids) == sorted([built["run_a"], built["run_b"]])
    assert sorted(check.fields()["run_ids"]) == sorted(check.run_ids)


# ── the attacks ─────────────────────────────────────────────────────────────


def test_an_unsigned_top_level_run_id_is_rejected_and_never_reported(
    built: dict[str, Any], tmp_path: Path
) -> None:
    forged = _rewrite(built["set"], tmp_path / "s.zip", lambda m: m.__setitem__("run_id", VICTIM))
    outcome = _verify(forged, built["trusted"])
    assert not outcome.ok
    assert VICTIM in _subject_problems(outcome)
    check = BundleCheck(outcome)
    assert check.run_id != VICTIM
    assert VICTIM not in check.run_ids


def test_a_fabricated_subject_in_a_set_bundle_is_rejected(
    built: dict[str, Any], tmp_path: Path
) -> None:
    def add(manifest: dict[str, Any]) -> None:
        manifest["subject"].append({
            "kind": "run-capsule",
            "run_id": VICTIM,
            "capsule_hash": "sha256:" + "0" * 64,
            "capsule_path_in_bundle": f"run-capsule/{VICTIM}/",
        })

    outcome = _verify(_rewrite(built["set"], tmp_path / "s.zip", add), built["trusted"])
    assert not outcome.ok
    assert VICTIM in _subject_problems(outcome)


def test_a_fabricated_subject_in_a_single_bundle_is_rejected(
    built: dict[str, Any], tmp_path: Path
) -> None:
    def add(manifest: dict[str, Any]) -> None:
        manifest["subject"] = [
            manifest["subject"],
            {"kind": "run-capsule", "run_id": VICTIM, "capsule_hash": "sha256:" + "1" * 64},
        ]

    outcome = _verify(_rewrite(built["single"], tmp_path / "s.zip", add), built["trusted"])
    assert not outcome.ok
    assert VICTIM in _subject_problems(outcome)
    assert BundleCheck(outcome).run_ids == [built["run_a"]]


def test_a_removed_run_whose_label_stays_is_rejected(
    built: dict[str, Any], tmp_path: Path
) -> None:
    run_b = built["run_b"]

    def strip(manifest: dict[str, Any]) -> None:
        for key, field in (
            ("attestations", "path"), ("signatures", "envelope_path"), ("artifacts", "path")
        ):
            manifest[key] = [e for e in manifest[key] if run_b not in e.get(field, "")]

    forged = _rewrite(built["set"], tmp_path / "s.zip", strip, drop=lambda n: run_b in n)
    outcome = _verify(forged, built["trusted"])
    assert not outcome.ok
    assert run_b in _subject_problems(outcome)
    assert BundleCheck(outcome).run_ids == [built["run_a"]]


def test_a_signed_run_the_manifest_omits_is_rejected(
    built: dict[str, Any], tmp_path: Path
) -> None:
    run_b = built["run_b"]

    def omit(manifest: dict[str, Any]) -> None:
        manifest["subject"] = [s for s in manifest["subject"] if s["run_id"] != run_b]

    outcome = _verify(_rewrite(built["set"], tmp_path / "s.zip", omit), built["trusted"])
    assert not outcome.ok
    assert run_b in _subject_problems(outcome)


def test_a_subject_pointing_at_another_runs_copy_is_rejected(
    built: dict[str, Any], tmp_path: Path
) -> None:
    run_a, run_b = built["run_a"], built["run_b"]

    def repoint(manifest: dict[str, Any]) -> None:
        for subject in manifest["subject"]:
            if subject["run_id"] == run_a:
                subject["capsule_path_in_bundle"] = f"run-capsule/{run_b}/"

    outcome = _verify(_rewrite(built["set"], tmp_path / "s.zip", repoint), built["trusted"])
    assert not outcome.ok
    assert "capsule_path_in_bundle" in _subject_problems(outcome)


def test_a_duplicated_subject_is_rejected(built: dict[str, Any], tmp_path: Path) -> None:
    def duplicate(manifest: dict[str, Any]) -> None:
        manifest["subject"].append(dict(manifest["subject"][0]))

    outcome = _verify(_rewrite(built["set"], tmp_path / "s.zip", duplicate), built["trusted"])
    assert not outcome.ok
    assert "more than once" in _subject_problems(outcome)


# ── the serve route reports signed runs only ───────────────────────────────


def test_the_verify_route_reports_signed_run_ids_only(
    built: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.serve.app import create_app

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_DIR", str(evidence))
    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_TRUSTED_KEYS", str(built["pub"]))
    monkeypatch.delenv("NOVAFABRIC_SEAL_CONFIG", raising=False)
    _rewrite(built["set"], evidence / "relabel.zip", lambda m: m.__setitem__("run_id", VICTIM))
    _rewrite(built["set"], evidence / "genuine.zip")
    runs = tmp_path / "runs"
    runs.mkdir()
    client = TestClient(
        create_app(token=TOKEN, capsule_dir=runs, db_path=tmp_path / "r.db"),
        raise_server_exceptions=False,
    )

    forged = client.post("/api/evidence/relabel/verify", params={"token": TOKEN}, headers=HOST)
    body = forged.json()
    assert body["valid"] is False
    assert body["run_id"] != VICTIM
    assert VICTIM not in body["run_ids"]

    genuine = client.post(
        "/api/evidence/genuine/verify", params={"token": TOKEN}, headers=HOST
    ).json()
    assert genuine["valid"] is True, genuine["errors"]
    assert genuine["run_id"] is None
    assert sorted(genuine["run_ids"]) == sorted([built["run_a"], built["run_b"]])
