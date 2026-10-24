"""Bad user input is a one-line error, never a traceback (review cli-b-16).

Each of these exited 1 with a raw Python traceback: ``ValueError`` from an
``--expires-in 2w``, ``FileNotFoundError`` from a missing ``--public-key``,
``URLError`` from an unreachable OpenLineage endpoint, ``AttributeError`` from a
non-object ``manifest.json`` (which also aborted the whole ``migrate-schema`` batch)
or redaction proof, and an unreadable envelope or key for ``verify-envelope``.
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def _clean(result: object, code: int) -> None:
    assert result.exit_code == code, result.output  # type: ignore[attr-defined]
    assert isinstance(result.exception, SystemExit), result.exception  # type: ignore[attr-defined]
    assert "Traceback" not in result.output  # type: ignore[attr-defined]


# ── --expires-in ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["2w", "12h", "0", "-5d", "d", ""])
def test_parse_days_refuses_anything_but_positive_days(value: str) -> None:
    import typer

    from novafabric.cli.server import _parse_days

    with pytest.raises(typer.BadParameter, match="days"):
        _parse_days(value)


@pytest.mark.parametrize(("value", "days"), [("90d", 90), ("30", 30), (" 7d ", 7)])
def test_parse_days_accepts_day_counts(value: str, days: int) -> None:
    from novafabric.cli.server import _parse_days

    assert _parse_days(value) == days


def test_issue_token_with_an_hours_expiry_is_a_usage_error(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["server", "issue-token", "--subject", "a", "--expires-in", "12h",
         "--key-path", str(tmp_path / "k.pem")],
    )
    _clean(result, 2)


# ── nova verify <bundle.zip> --public-key ─────────────────────────────────────


def test_verify_bundle_with_a_missing_public_key_is_clean(tmp_path: Path) -> None:
    bundle = tmp_path / "b.zip"
    with zipfile.ZipFile(bundle, "w") as zf:
        zf.writestr("manifest.json", "{}")
    result = runner.invoke(app, ["verify", str(bundle), "--public-key", str(tmp_path / "no.pem")])
    _clean(result, 1)
    assert "--public-key" in result.output


# ── nova lineage emit-openlineage --output http://unreachable ────────────────


def test_emit_openlineage_to_an_unreachable_endpoint_is_clean(tmp_path: Path) -> None:
    from novafabric.capture.orchestrator import CaptureOrchestrator

    cap = CaptureOrchestrator(base_dir=tmp_path / "runs").run(
        command=[sys.executable, "-c", "pass"]
    ).capsule_dir
    result = runner.invoke(
        app, ["lineage", "emit-openlineage", str(cap), "--output", "http://127.0.0.1:9/"]
    )
    _clean(result, 1)
    assert cap.name in result.output and "0 capsule(s) delivered" in result.output


# ── nova migrate-schema: a non-object manifest.json ──────────────────────────


def test_migrate_schema_skips_a_non_object_manifest_and_continues(tmp_path: Path) -> None:
    (tmp_path / "a-broken").mkdir()
    (tmp_path / "a-broken" / "manifest.json").write_text("[]")
    (tmp_path / "b-good").mkdir()
    (tmp_path / "b-good" / "manifest.json").write_text(json.dumps({"run_id": "r"}))
    result = runner.invoke(app, ["migrate-schema", "--capsule-dir", str(tmp_path)])
    _clean(result, 1)
    assert "Failed: 1" in result.output
    assert json.loads((tmp_path / "b-good" / "manifest.json").read_text())["schema_version"]


# ── nova redaction-xray --capsule with a non-object proof ────────────────────


def test_redaction_xray_on_a_non_object_proof_is_clean(tmp_path: Path) -> None:
    cap = tmp_path / "cap"
    cap.mkdir()
    (cap / "redaction-proof.json").write_text("[1, 2]")
    result = runner.invoke(app, ["redaction-xray", "--capsule", str(cap)])
    _clean(result, 2)


# ── nova verify-envelope ─────────────────────────────────────────────────────


@pytest.mark.parametrize("problem", ["missing-envelope", "not-json", "missing-key", "not-a-key"])
def test_verify_envelope_bad_inputs_are_clean(tmp_path: Path, problem: str) -> None:
    envelope = tmp_path / "env.json"
    key = tmp_path / "key.pem"
    if problem != "missing-envelope":
        envelope.write_text("{not json" if problem == "not-json" else "{}")
    if problem == "not-a-key":
        key.write_text("not a key")
    elif problem != "missing-key":
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key.write_bytes(Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ))
    result = runner.invoke(app, ["verify-envelope", str(envelope), "--key", str(key)])
    assert result.exit_code != 0, result.output
    assert isinstance(result.exception, SystemExit), result.exception
