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
