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

"""Endpoint tests for POST /api/otlp/v1/traces (NF-034, ADR-0098)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

pytest.importorskip("fastapi")
pytest.importorskip("starlette")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

VALID_TOKEN = "test-token-1234567890abcdef"
HEADERS = {"host": "127.0.0.1:4321"}
FIXTURES = Path(__file__).parents[1] / "fixtures" / "otlp"


def _valid_payload() -> dict:
    return json.loads((FIXTURES / "genai-traces-valid.json").read_text())


@pytest.fixture
def capsule_base(tmp_path: Path) -> Path:
    base = tmp_path / "runs"
    base.mkdir()
    return base


@pytest.fixture
def client(capsule_base: Path, tmp_path: Path) -> TestClient:
    app = create_app(
        token=VALID_TOKEN,
        capsule_dir=capsule_base,
        db_path=tmp_path / "registry.db",
    )
    return TestClient(app)


def test_otlp_ingest_requires_token(client: TestClient) -> None:
    resp = client.post("/api/otlp/v1/traces", json=_valid_payload(), headers=HEADERS)
    assert resp.status_code == 401


def test_otlp_ingest_rejects_bad_token(client: TestClient) -> None:
    resp = client.post(
        "/api/otlp/v1/traces?token=wrong-token", json=_valid_payload(), headers=HEADERS
    )
    assert resp.status_code == 401


def test_otlp_ingest_malformed_payload_is_400(client: TestClient) -> None:
    resp = client.post(
        f"/api/otlp/v1/traces?token={VALID_TOKEN}",
        json={"resourceSpans": "not-a-list"},
        headers=HEADERS,
    )
    assert resp.status_code == 400
    assert "resourceSpans" in resp.json()["detail"]


def test_otlp_ingest_missing_resource_spans_is_400(client: TestClient) -> None:
    resp = client.post(
        f"/api/otlp/v1/traces?token={VALID_TOKEN}", json={}, headers=HEADERS
    )
    assert resp.status_code == 400


def test_otlp_ingest_writes_valid_capsule(
    client: TestClient, capsule_base: Path
) -> None:
    resp = client.post(
        f"/api/otlp/v1/traces?token={VALID_TOKEN}",
        json=_valid_payload(),
        headers=HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["spans_ingested"] == 3
    assert body["spans_skipped"] == 1
    assert body["model_call_count"] == 1
    assert body["tool_call_count"] == 1
    assert body["capture_level"] == "ingested-otlp"
    assert body["unmapped_attribute_keys"] == ["gen_ai.future.attribute"]

    cdir = capsule_base / body["capsule_id"]
    assert cdir.is_dir()
    manifest = yaml.safe_load((cdir / "capsule.yaml").read_text())
    assert manifest["capture_mode"] == "otel-import"
    assert manifest["metadata"]["capture_level"] == "ingested-otlp"

    # The sealed capsule passes the same checks as `nova validate`.
    from typer.testing import CliRunner

    from novafabric.cli.main import app as cli_app

    run = CliRunner().invoke(cli_app, ["validate", str(cdir)])
    assert run.exit_code == 0, run.output


def test_otlp_ingest_no_genai_spans_writes_nothing(
    client: TestClient, capsule_base: Path
) -> None:
    payload = {"resourceSpans": [{"scopeSpans": [{"spans": [{
        "name": "GET /healthz",
        "attributes": [{"key": "http.request.method", "value": {"stringValue": "GET"}}],
    }]}]}]}
    resp = client.post(
        f"/api/otlp/v1/traces?token={VALID_TOKEN}", json=payload, headers=HEADERS
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["capsule_id"] is None
    assert body["spans_ingested"] == 0
    assert body["spans_skipped"] == 1
    assert list(capsule_base.iterdir()) == []


def _protobuf_body() -> bytes:
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

    req = ExportTraceServiceRequest()
    sp = req.resource_spans.add().scope_spans.add().spans.add()
    sp.name = "chat gpt-4o"
    sp.trace_id = bytes.fromhex("0123456789abcdef0123456789abcdef")
    sp.span_id = bytes.fromhex("0123456789abcdef")
    sp.start_time_unix_nano = 1_700_000_000_000_000_000
    sp.end_time_unix_nano = 1_700_000_001_000_000_000
    for key, val in (
        ("gen_ai.system", "openai"),
        ("gen_ai.request.model", "gpt-4o"),
        ("gen_ai.operation.name", "chat"),
    ):
        kv = sp.attributes.add()
        kv.key = key
        kv.value.string_value = val
    return req.SerializeToString()


def test_otlp_ingest_protobuf_body(client: TestClient, capsule_base: Path) -> None:
    # OTLP/protobuf ingest (ADR-0177): Content-Type dispatch → same events as JSON.
    pytest.importorskip("opentelemetry.proto.collector.trace.v1.trace_service_pb2")
    resp = client.post(
        f"/api/otlp/v1/traces?token={VALID_TOKEN}",
        content=_protobuf_body(),
        headers={**HEADERS, "content-type": "application/x-protobuf"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["spans_ingested"] == 1
    assert body["model_call_count"] == 1
    assert body["capture_level"] == "ingested-otlp"


# ── finalization: the path `nova capture` uses (ADR-0009/ADR-0251 amendments) ──

# Assembled at runtime: no contiguous provider-shaped token in the source bytes.
_GH_TOKEN = "ghp" + "_" + "Ab3dEf6hIj" * 3 + "Kl3mNp"


def _leaky_agent_payload() -> dict:
    """The agent name becomes manifest metadata; the message lands in model-calls."""
    payload = _valid_payload()
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    for attr in spans[0]["attributes"]:
        if attr["key"] == "gen_ai.agent.name":
            attr["value"] = {"stringValue": f"agent-{_GH_TOKEN}"}
    spans[1]["attributes"].append({
        "key": "gen_ai.input.messages",
        "value": {"stringValue": f"use key {_GH_TOKEN}"},
    })
    return payload


def _assert_no_token(cdir: Path) -> None:
    for path in cdir.rglob("*"):
        if path.is_file():
            assert _GH_TOKEN[4:20].encode() not in path.read_bytes(), path


@pytest.fixture
def seal_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    key_path = tmp_path / "seal.key"
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "OTLP-Ingest-Seal-Test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "seal.crt"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    config = tmp_path / "novaseal.yaml"
    config.write_text(
        f"profile: local\nkey_path: {key_path}\ncert_path: {cert_path}\n"
        f"tsa_url: \nmerkle_db: {tmp_path / 'merkle.db'}\n"
    )
    monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(config))
    return config


def _post(client: TestClient, payload: dict) -> dict:
    resp = client.post(
        f"/api/otlp/v1/traces?token={VALID_TOKEN}", json=payload, headers=HEADERS
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_otlp_ingest_redacts_and_binds_without_a_signing_profile(
    client: TestClient, capsule_base: Path
) -> None:
    body = _post(client, _leaky_agent_payload())
    assert body["sealed"] is False
    assert "finalization_error" not in body  # opt-in sealing is not a failure
    cdir = capsule_base / body["capsule_id"]
    _assert_no_token(cdir)
    manifest = yaml.safe_load((cdir / "capsule.yaml").read_text())
    assert manifest["metadata"]["capture_level"] == "ingested-otlp"
    assert "finalization_error" not in manifest["metadata"]
    assert {"model-calls.jsonl", "replay.yaml", "lineage.jsonl", "redaction-proof.json"} <= set(
        manifest["evidence_digests"]
    )
    assert not (cdir / ".seal").exists()


def test_otlp_ingest_is_sealed_and_verifies_with_a_signing_profile(
    client: TestClient, capsule_base: Path, seal_profile: Path
) -> None:
    from typer.testing import CliRunner

    from novafabric.cli.main import app as cli_app

    body = _post(client, _leaky_agent_payload())
    assert body["sealed"] is True
    cdir = capsule_base / body["capsule_id"]
    _assert_no_token(cdir)
    assert (cdir / ".seal" / "manifest.dsse").is_file()
    run = CliRunner().invoke(
        cli_app, ["verify", str(cdir), "--seal-config", str(seal_profile)]
    )
    assert run.exit_code == 0, run.output
    manifest = yaml.safe_load((cdir / "capsule.yaml").read_text())
    assert manifest["metadata"]["capture_level"] == "ingested-otlp"


def test_otlp_ingest_finalization_failure_keeps_the_data_and_says_why(
    client: TestClient,
    capsule_base: Path,
    seal_profile: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novafabric.capture import finalize as finalize_mod

    def _broken(capsule_dir: Path) -> dict:
        raise RuntimeError(f"disk on fire {_GH_TOKEN}")

    monkeypatch.setattr(finalize_mod, "evidence_digests", _broken)
    body = _post(client, _valid_payload())
    assert body["sealed"] is False
    assert "disk on fire" in body["finalization_error"]
    assert _GH_TOKEN[4:20] not in body["finalization_error"]  # redacted
    cdir = capsule_base / body["capsule_id"]
    assert not (cdir / ".seal").exists()
    manifest = yaml.safe_load((cdir / "capsule.yaml").read_text())
    assert "disk on fire" in manifest["metadata"]["finalization_error"]
    assert manifest["metadata"]["capture_level"] == "ingested-otlp"
    assert len((cdir / "model-calls.jsonl").read_text().splitlines()) == 1
    assert len((cdir / "tool-calls.jsonl").read_text().splitlines()) == 1
