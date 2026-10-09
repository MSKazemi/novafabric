"""Adapter and ``@agent`` capsules finalize through the same path as ``nova capture``.

Before 2026-10-09 a capsule written inside the user's process (the framework adapters
and the ``@agent`` decorator) ran one secret scan, wrote ``replay.yaml`` after it, and
stopped: no ADR-0009 residual pass, no manifest redaction, no ``lineage.jsonl``, no
ADR-0251 ``evidence_digests``, and never a seal -- even with a signing profile
configured. These tests plant secrets where only the later stages can catch them, then
search the finished capsule for any fragment, and run the real ``nova verify``.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from typer.testing import CliRunner

from novafabric.adapters._capsule import begin_capture
from novafabric.cli.main import app
from novafabric.sdk.agent import agent

# Assembled at runtime: no contiguous provider-shaped token in the source bytes.
GITHUB_TOKEN = "ghp" + "_" + "Ab3dEf6hIj" * 3 + "Kl3mNp"
ANTHROPIC_KEY = "sk-ant-api03-" + "Ab3dEf6hIj" * 9 + "xyzAA"
LATE_TOKEN = "ghp" + "_" + "Zy9xWv8uTs" * 3 + "Rq7pOn"

#: Where an in-process capsule records why it is not sealed.
FINALIZATION_KEY = "finalization_error"


def _assert_absent(capsule_dir: Path, secret: str) -> None:
    """No file anywhere in the capsule holds the secret -- or any 16-char piece of it."""
    needles = {secret.encode()} | {
        secret[i : i + 16].encode() for i in range(0, max(1, len(secret) - 16), 4)
    }
    for path in capsule_dir.rglob("*"):
        if path.is_file() and not path.is_symlink():
            data = path.read_bytes()
            for needle in needles:
                assert needle not in data, f"{secret[:6]}… fragment survives in {path}"


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _manifest(capsule: Path) -> dict[str, Any]:
    return yaml.safe_load((capsule / "capsule.yaml").read_text())


def _proof(capsule: Path) -> dict[str, Any]:
    return json.loads((capsule / "redaction-proof.json").read_text())


def _assert_bound(capsule: Path) -> dict[str, Any]:
    """Every evidence file is in evidence_digests with its on-disk hash, and vice versa."""
    manifest = _manifest(capsule)
    digests = manifest["evidence_digests"]
    on_disk = {
        p.relative_to(capsule).as_posix()
        for p in capsule.rglob("*")
        if p.is_file() and p.relative_to(capsule).parts[0] not in {".seal", "capsule.yaml"}
    }
    assert set(digests) == on_disk
    for rel, entry in digests.items():
        assert entry["sha256"] == _sha((capsule / rel).read_bytes()), rel
    assert "lineage.jsonl" in digests
    assert "replay.yaml" in digests
    assert "redaction-proof.json" in digests
    return manifest


@pytest.fixture()
def seal_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    key = ec.generate_private_key(ec.SECP256R1())
    key_path = tmp_path / "seal.key"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "In-Process-Seal-Test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "seal.crt"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    config_path = tmp_path / "novaseal.yaml"
    config_path.write_text(
        f"profile: local\nkey_path: {key_path}\ncert_path: {cert_path}\n"
        f"tsa_url: \nmerkle_db: {tmp_path / 'merkle.db'}\n"
    )
    monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(config_path))
    return config_path


def _verify(capsule: Path, seal_config: Path) -> Any:
    return CliRunner().invoke(app, ["verify", str(capsule), "--seal-config", str(seal_config)])


@pytest.fixture()
def late_secret(monkeypatch: pytest.MonkeyPatch) -> str:
    """Write a secret into the capsule AFTER the main scan, alongside lineage.jsonl.

    Only the ADR-0009 residual pass runs after lineage emission, so only it can catch
    this file. A capsule finalized without the shared path never even reaches here.
    """
    from novafabric.lineage._writer import LineageWriter

    real_write = LineageWriter.write

    def _write(self: LineageWriter, edges: Any) -> Path:
        out = real_write(self, edges)
        (self._capsule_dir / "outputs" / "late.txt").write_text(f"token={LATE_TOKEN}\n")
        return out

    monkeypatch.setattr(LineageWriter, "write", _write)
    return "outputs/late.txt"


@pytest.fixture()
def leaky_replay_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    from novafabric.capture import replay as replay_mod

    real = replay_mod.minimal_replay_policy

    def _leaky() -> dict[str, Any]:
        return {**real(), "note": f"token {ANTHROPIC_KEY}"}

    monkeypatch.setattr(replay_mod, "minimal_replay_policy", _leaky)


def _adapter_capsule(tmp_path: Path, *, run_name: str = "demo") -> Path:
    cap = begin_capture(framework="testfw", run_name=run_name, data_dir=tmp_path / "runs")
    (cap.cap_dir / "outputs" / "answer.txt").write_text(f"gh={GITHUB_TOKEN}\n")
    cap.finish()
    return cap.cap_dir


# ── AdapterCapture (LlamaIndex, Pydantic AI, Haystack) ───────────────────────


def test_adapter_capsule_runs_the_residual_pass_over_late_files(
    tmp_path: Path, late_secret: str, leaky_replay_policy: None
) -> None:
    capsule = _adapter_capsule(tmp_path)

    for secret in (GITHUB_TOKEN, ANTHROPIC_KEY, LATE_TOKEN):
        _assert_absent(capsule, secret)
    proof = _proof(capsule)
    residual = proof["residual_check"]
    # replay.yaml is not a main-scan target and late.txt did not exist yet: both
    # are caught by the residual pass, exactly as for `nova capture`.
    assert set(residual["residual_refs"]) == {"replay.yaml", late_secret}
    refs = {f["target_ref"] for f in proof["findings"]}
    assert {"outputs/answer.txt", "replay.yaml", late_secret} <= refs
    manifest = _assert_bound(capsule)
    assert FINALIZATION_KEY not in manifest.get("metadata", {})


def test_adapter_manifest_is_redacted_before_it_is_written(tmp_path: Path) -> None:
    capsule = _adapter_capsule(tmp_path, run_name=f"run-{GITHUB_TOKEN}")
    _assert_absent(capsule, GITHUB_TOKEN)
    targets = {t["ref"]: t for t in _proof(capsule)["targets"]}
    assert targets["capsule.yaml"]["findings_count"] >= 1


def test_adapter_capsule_is_unsealed_without_a_signing_profile(tmp_path: Path) -> None:
    capsule = _adapter_capsule(tmp_path)
    assert not (capsule / ".seal").exists()
    manifest = _assert_bound(capsule)
    # Opt-in sealing (ADR-0301) is not a failure: nothing is recorded.
    assert FINALIZATION_KEY not in manifest.get("metadata", {})
    assert (capsule / "lineage.jsonl").is_file()


def test_adapter_capsule_is_sealed_and_verifies_with_a_signing_profile(
    tmp_path: Path, seal_config: Path
) -> None:
    capsule = _adapter_capsule(tmp_path)
    assert (capsule / ".seal" / "manifest.dsse").is_file()
    _assert_bound(capsule)
    verified = _verify(capsule, seal_config)
    assert verified.exit_code == 0, verified.output

    # The bytes are bound: an edit to a recorded file is caught.
    (capsule / "outputs" / "answer.txt").write_text("edited\n")
    tampered = _verify(capsule, seal_config)
    assert tampered.exit_code == 1, tampered.output


# ── an adapter with its own writer (LangGraph) ───────────────────────────────


def test_own_writer_adapter_is_sealed_and_verifies(tmp_path: Path, seal_config: Path) -> None:
    graph = MagicMock()
    graph.invoke.return_value = {"answer": 42}
    with patch.dict(sys.modules, {"langgraph": MagicMock()}):
        from novafabric.adapters.langgraph import wrap

        wrapped = wrap(graph, run_name=f"lg-{GITHUB_TOKEN}", data_dir=tmp_path / "runs")
        assert wrapped.invoke({"q": 1}) == {"answer": 42}

    (capsule,) = [p.parent for p in (tmp_path / "runs").glob("*/capsule.yaml")]
    _assert_absent(capsule, GITHUB_TOKEN)
    assert (capsule / ".seal" / "manifest.dsse").is_file()
    _assert_bound(capsule)
    verified = _verify(capsule, seal_config)
    assert verified.exit_code == 0, verified.output


# ── @agent decorator ─────────────────────────────────────────────────────────


def test_sdk_agent_capsule_is_finalized_sealed_and_verifies(
    tmp_path: Path, seal_config: Path, late_secret: str, leaky_replay_policy: None
) -> None:
    cap_dir = tmp_path / "capsule"

    @agent(name="sdk-agent", version="1.0", capsule_dir=cap_dir)
    def work() -> str:
        (cap_dir / "outputs" / "answer.txt").write_text(f"gh={GITHUB_TOKEN}\n")
        return "done"

    assert work() == "done"
    for secret in (GITHUB_TOKEN, ANTHROPIC_KEY, LATE_TOKEN):
        _assert_absent(cap_dir, secret)
    assert set(_proof(cap_dir)["residual_check"]["residual_refs"]) == {
        "replay.yaml", late_secret
    }
    manifest = _assert_bound(cap_dir)
    assert (cap_dir / ".seal" / "manifest.dsse").is_file()
    verified = _verify(cap_dir, seal_config)
    assert verified.exit_code == 0, verified.output
    # The run is indexed under its run id, not the caller-chosen directory name.
    lineage_runs = {
        json.loads(line).get("capsule_run_id")
        for line in (cap_dir / "lineage.jsonl").read_text().splitlines()
        if line.strip()
    }
    assert lineage_runs <= {manifest["run_id"]}


def test_sdk_agent_capsule_without_profile_is_unsealed_but_bound(tmp_path: Path) -> None:
    cap_dir = tmp_path / "capsule"

    @agent(name="sdk-agent", version="1.0", capsule_dir=cap_dir)
    def work() -> int:
        return 7

    assert work() == 7
    assert not (cap_dir / ".seal").exists()
    manifest = _assert_bound(cap_dir)
    assert "metadata" not in manifest  # nothing to report: opt-in, not a failure


# ── degrade, never block ─────────────────────────────────────────────────────


def _broken_digests(capsule_dir: Path) -> dict[str, Any]:
    raise RuntimeError("disk on fire")


def test_finalization_failure_leaves_sdk_capsule_unsealed_and_result_unchanged(
    tmp_path: Path, seal_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.capture import finalize as finalize_mod

    monkeypatch.setattr(finalize_mod, "evidence_digests", _broken_digests)
    cap_dir = tmp_path / "capsule"

    @agent(name="sdk-agent", version="1.0", capsule_dir=cap_dir)
    def work() -> dict[str, int]:
        return {"answer": 42}

    assert work() == {"answer": 42}
    assert not (cap_dir / ".seal").exists()
    reason = _manifest(cap_dir)["metadata"][FINALIZATION_KEY]
    assert "RuntimeError" in reason and "disk on fire" in reason


def test_finalization_failure_never_replaces_the_callers_exception(
    tmp_path: Path, seal_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.capture import finalize as finalize_mod

    monkeypatch.setattr(finalize_mod, "evidence_digests", _broken_digests)
    cap_dir = tmp_path / "capsule"

    @agent(name="sdk-agent", version="1.0", capsule_dir=cap_dir)
    def explode() -> None:
        raise ValueError("the user's own error")

    with pytest.raises(ValueError, match="the user's own error"):
        explode()
    manifest = _manifest(cap_dir)
    assert manifest["status"] == "failure"
    assert "RuntimeError" in manifest["metadata"][FINALIZATION_KEY]
    assert not (cap_dir / ".seal").exists()


def test_adapter_finalization_failure_is_recorded_and_unsealed(
    tmp_path: Path, seal_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.capture import finalize as finalize_mod

    monkeypatch.setattr(finalize_mod, "evidence_digests", _broken_digests)
    capsule = _adapter_capsule(tmp_path)
    assert not (capsule / ".seal").exists()
    manifest = _manifest(capsule)
    assert "disk on fire" in manifest["metadata"][FINALIZATION_KEY]
    assert manifest["metadata"]["framework"] == "testfw"  # the caller's labels survive
    _assert_absent(capsule, GITHUB_TOKEN)  # the main scan had already run


def test_failure_reason_is_redacted(
    tmp_path: Path, seal_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.capture import finalize as finalize_mod

    def _leaky_failure(capsule_dir: Path) -> dict[str, Any]:
        raise RuntimeError(f"bad token {LATE_TOKEN}")

    monkeypatch.setattr(finalize_mod, "evidence_digests", _leaky_failure)
    capsule = _adapter_capsule(tmp_path)
    _assert_absent(capsule, LATE_TOKEN)
    assert "RuntimeError" in _manifest(capsule)["metadata"][FINALIZATION_KEY]


def test_failed_seal_is_recorded_and_leaves_no_partial_seal(
    tmp_path: Path, seal_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.capture import finalize as finalize_mod

    def _half_seal(capsule_dir: Path, manifest: dict[str, Any]) -> str | None:
        (capsule_dir / ".seal").mkdir()
        (capsule_dir / ".seal" / "manifest.dsse").write_bytes(b"partial")
        return "NovaSeal sealing failed: TimeoutError: tsa unreachable"

    monkeypatch.setattr(finalize_mod, "seal_capsule", _half_seal)
    capsule = _adapter_capsule(tmp_path)
    # A failed seal that still left a DSSE file is not "sealed".
    assert not (capsule / ".seal").exists()
    assert "tsa unreachable" in _manifest(capsule)["metadata"][FINALIZATION_KEY]


def test_manifest_gate_refusal_is_recorded_and_unsealed(
    tmp_path: Path, seal_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.capture import finalize as finalize_mod

    real = finalize_mod.evidence_digests

    def _leaky_digests(capsule_dir: Path) -> dict[str, Any]:
        digests = real(capsule_dir)
        digests[f"outputs/{GITHUB_TOKEN}.txt"] = {"sha256": _sha(b""), "size_bytes": 0}
        return digests

    monkeypatch.setattr(finalize_mod, "evidence_digests", _leaky_digests)
    capsule = _adapter_capsule(tmp_path)
    assert not (capsule / ".seal").exists()
    _assert_absent(capsule, GITHUB_TOKEN)
    assert "manifest gate" in _manifest(capsule)["metadata"][FINALIZATION_KEY]


# ── every in-process writer, not just the two exercised above ────────────────

_SRC = Path(__file__).resolve().parents[2] / "src" / "novafabric"
_IN_PROCESS_WRITERS = sorted(
    [p for p in (_SRC / "adapters").glob("*.py") if p.name != "__init__.py"]
    + [_SRC / "sdk" / "agent.py"]
)


def _writes_a_manifest(source: str) -> bool:
    return '"schema_version"' in source and '"run_id"' in source


def test_the_writer_list_is_not_empty() -> None:
    """Guard the guard: the eight own-writer adapters, AdapterCapture and @agent."""
    assert sum(_writes_a_manifest(p.read_text()) for p in _IN_PROCESS_WRITERS) >= 10


@pytest.mark.parametrize("path", _IN_PROCESS_WRITERS, ids=lambda p: p.name)
def test_every_in_process_writer_finalizes_through_the_shared_path(path: Path) -> None:
    """A capsule writer that scans or writes capsule.yaml on its own skips the
    residual pass, the digests and the seal -- the gap this module closed."""
    source = path.read_text()
    if not _writes_a_manifest(source):
        return
    assert "finalize_in_process_capsule(" in source, path.name
    assert "SecretScannerV0" not in source, f"{path.name} runs its own scan"
    assert '"capsule.yaml", yaml.dump' not in source, f"{path.name} writes capsule.yaml"
