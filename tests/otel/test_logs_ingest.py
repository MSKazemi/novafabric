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
"""ADR-0293: OTLP logs ingest into an append-only sidecar log store.

Properties:
- link precedence: record run id > resource run id > trace id > unlinked day;
- capsules are never written — a linked capsule's state is observed only;
- default storage is metadata only (no body text, no attribute values);
- opt-in body storage redacts with the ADR-0009 pack and truncates;
- ADR-0127 severity mapping (number authoritative, text fallback);
- request, attribute and stream-size bounds; symlink refusal; file modes;
- JSON and protobuf converge on identical stored records.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from novafabric.otel import logs_ingest
from novafabric.otel.logs_ingest import (
    LOG_RECORD_SCHEMA,
    OTLPLogsIngestError,
    default_log_store_dir,
    ingest_otlp_logs,
    ingest_otlp_logs_body,
    parse_otlp_logs_json,
    parse_otlp_logs_protobuf,
    read_log_records,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "otlp" / "logs-valid.json"
RUN_RES = "01RUNRESOURCE0000000000001"
RUN_REC = "01RUNRECORD00000000000002"
TRACE = "0af7651916cd43dd8448eb211c80319c"
SECRET = "sk-ant-" + "a1b2c3d4e5" * 3
NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)


def _payload() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def _record(**fields: Any) -> dict[str, Any]:
    lr: dict[str, Any] = {"timeUnixNano": "1790000000000000000"}
    lr.update(fields)
    return {"resourceLogs": [{"scopeLogs": [{"logRecords": [lr]}]}]}


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def _ingest(payload: dict[str, Any], store: Path, **kw: Any) -> logs_ingest.LogIngestResult:
    return ingest_otlp_logs(parse_otlp_logs_json(payload), store, now=NOW, **kw)


class TestParse:
    def test_flattens_and_decodes(self) -> None:
        recs = parse_otlp_logs_json(_payload())
        assert len(recs) == 4
        assert recs[0]["resource"]["service.name"] == "agent-svc"
        assert recs[0]["trace_id"] == "5b8efff798038103d269b633813fc60c"
        assert recs[2]["body"] == {"k": 1}

    @pytest.mark.parametrize("bad", [None, [], {}, {"resourceLogs": {}}])
    def test_rejects_non_export(self, bad: Any) -> None:
        with pytest.raises(OTLPLogsIngestError):
            parse_otlp_logs_json(bad)

    def test_tolerates_junk_entries(self) -> None:
        payload = {"resourceLogs": [1, {"scopeLogs": [2, {"logRecords": [3, {}]}]}]}
        assert len(parse_otlp_logs_json(payload)) == 1

    def test_zero_and_malformed_ids_dropped(self) -> None:
        recs = parse_otlp_logs_json(_record(traceId="0" * 32, spanId="xyz"))
        assert recs[0]["trace_id"] is None and recs[0]["span_id"] is None

    def test_record_count_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(logs_ingest, "MAX_RECORDS_PER_REQUEST", 2)
        payload = {"resourceLogs": [{"scopeLogs": [{"logRecords": [{}, {}, {}]}]}]}
        with pytest.raises(OTLPLogsIngestError, match="split the batch"):
            parse_otlp_logs_json(payload)


class TestLinking:
    def test_streams_by_link_key(self, tmp_path: Path) -> None:
        result = _ingest(_payload(), tmp_path)
        assert result.records_seen == result.records_stored == 4
        assert result.linked_runs == sorted([RUN_RES, RUN_REC])
        assert result.linked_traces == 1 and result.unlinked == 1
        assert (tmp_path / "runs" / f"{RUN_RES}.jsonl").is_file()
        # The record-level run id beats the resource one.
        assert _lines(tmp_path / "runs" / f"{RUN_REC}.jsonl")[0]["body"]["bytes"] == len(
            "model call failed"
        )
        assert _lines(tmp_path / "traces" / f"{TRACE}.jsonl")[0]["trace_id"] == TRACE
        assert (tmp_path / "unlinked" / "2026-09-21.jsonl").is_file()

    def test_invalid_run_id_falls_back_to_trace(self, tmp_path: Path) -> None:
        payload = _record(
            traceId=TRACE,
            attributes=[{"key": "novafabric.run_id", "value": {"stringValue": "../etc"}}],
        )
        _ingest(payload, tmp_path)
        assert not (tmp_path / "runs").exists()
        assert (tmp_path / "traces" / f"{TRACE}.jsonl").is_file()

    def test_appends_never_rewrite(self, tmp_path: Path) -> None:
        _ingest(_payload(), tmp_path)
        _ingest(_payload(), tmp_path)
        assert len(_lines(tmp_path / "traces" / f"{TRACE}.jsonl")) == 2


class TestCapsuleNeverAmended:
    def _capsule(self, root: Path, run_id: str, *, sealed: bool) -> Path:
        cdir = root / run_id
        cdir.mkdir(parents=True)
        (cdir / "capsule.yaml").write_text(f"run_id: {run_id}\n")
        if sealed:
            (cdir / ".seal").mkdir()
            (cdir / ".seal" / "seal.json").write_text("{}")
        return cdir

    def _snapshot(self, cdir: Path) -> dict[str, str]:
        return {
            str(p.relative_to(cdir)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(cdir.rglob("*"))
            if p.is_file()
        }

    def test_sealed_state_observed_and_bytes_unchanged(self, tmp_path: Path) -> None:
        caps = tmp_path / "capsules"
        sealed = self._capsule(caps, RUN_RES, sealed=True)
        open_ = self._capsule(caps, RUN_REC, sealed=False)
        before = (self._snapshot(sealed), self._snapshot(open_))
        result = _ingest(_payload(), tmp_path / "store", capsule_dir=caps)
        assert (self._snapshot(sealed), self._snapshot(open_)) == before
        assert result.sealed_runs == [RUN_RES]
        assert result.to_response()["capsule_amended"] is False
        store = tmp_path / "store" / "runs"
        assert _lines(store / f"{RUN_RES}.jsonl")[0]["capsule_state"] == "sealed"
        assert _lines(store / f"{RUN_REC}.jsonl")[0]["capsule_state"] == "unsealed"

    def test_absent_and_unknown_states(self, tmp_path: Path) -> None:
        _ingest(_payload(), tmp_path / "a", capsule_dir=tmp_path / "none")
        row = _lines(tmp_path / "a" / "runs" / f"{RUN_RES}.jsonl")[0]
        assert row["capsule_state"] == "absent"
        _ingest(_payload(), tmp_path / "b")
        row = _lines(tmp_path / "b" / "runs" / f"{RUN_RES}.jsonl")[0]
        assert row["capsule_state"] == "unknown"
        trace_row = _lines(tmp_path / "b" / "traces" / f"{TRACE}.jsonl")[0]
        assert "capsule_state" not in trace_row


class TestHygiene:
    def test_default_stores_metadata_only(self, tmp_path: Path) -> None:
        payload = _record(
            traceId=TRACE,
            body={"stringValue": f"prompt text with {SECRET}"},
            attributes=[{"key": "gen_ai.prompt", "value": {"stringValue": "user said hi"}}],
        )
        _ingest(payload, tmp_path)
        raw = (tmp_path / "traces" / f"{TRACE}.jsonl").read_text()
        assert "prompt text" not in raw and SECRET not in raw and "user said hi" not in raw
        row = json.loads(raw)
        assert row["schema"] == LOG_RECORD_SCHEMA
        assert row["attribute_keys"] == ["gen_ai.prompt"]
        assert "attributes" not in row and "text" not in row["body"]
        body = f"prompt text with {SECRET}".encode()
        assert row["body"] == {
            "type": "string",
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }

    def test_opt_in_body_is_redacted_and_truncated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(logs_ingest, "MAX_BODY_CHARS", 40)
        payload = _record(
            traceId=TRACE,
            body={"stringValue": f"{SECRET} " + "x" * 100},
            attributes=[
                {"key": "token", "value": {"stringValue": SECRET}},
                {"key": "n", "value": {"intValue": "3"}},
                {"key": "list", "value": {"arrayValue": {"values": [{"stringValue": "a"}]}}},
            ],
        )
        result = _ingest(payload, tmp_path, store_body=True)
        raw = (tmp_path / "traces" / f"{TRACE}.jsonl").read_text()
        assert SECRET not in raw
        row = json.loads(raw)
        assert row["body"]["text"].startswith("[REDACTED:")
        assert len(row["body"]["text"]) <= 40 and row["body"]["truncated"] is True
        assert row["attributes"]["n"] == 3
        assert row["attributes"]["token"].startswith("[REDACTED:")
        assert row["attributes"]["list"] == '["a"]'
        assert result.redacted_fields == 2 and result.store_body is True

    def test_store_body_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOVAFABRIC_OTLP_LOGS_STORE_BODY", "1")
        body = json.dumps(_record(traceId=TRACE, body={"stringValue": "hello"})).encode()
        out = ingest_otlp_logs_body(body, "application/json", store_dir=tmp_path)
        assert out["store_body"] is True
        assert _lines(tmp_path / "traces" / f"{TRACE}.jsonl")[0]["body"]["text"] == "hello"

    def test_attribute_key_cap(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(logs_ingest, "MAX_ATTRIBUTE_KEYS", 2)
        attrs = [{"key": f"k{i}", "value": {"intValue": str(i)}} for i in range(5)]
        _ingest(_record(traceId=TRACE, attributes=attrs), tmp_path)
        row = _lines(tmp_path / "traces" / f"{TRACE}.jsonl")[0]
        assert row["attribute_keys"] == ["k0", "k1"]
        assert row["attribute_keys_truncated"] == 3


class TestSeverity:
    @pytest.mark.parametrize(
        ("fields", "level"),
        [
            ({"severityNumber": 13}, "warn"),
            ({"severityNumber": "SEVERITY_NUMBER_ERROR2"}, "error"),
            ({"severityNumber": 21}, "error"),
            ({"severityNumber": 1}, "debug"),
            ({"severityText": "WARNING"}, "warn"),
            ({"severityNumber": 0, "severityText": "error"}, "error"),
            ({"severityNumber": 99, "severityText": "error"}, None),
            ({"severityNumber": "13"}, None),
            ({}, None),
        ],
    )
    def test_mapping(self, tmp_path: Path, fields: dict[str, Any], level: str | None) -> None:
        _ingest(_record(traceId=TRACE, **fields), tmp_path)
        row = _lines(tmp_path / "traces" / f"{TRACE}.jsonl")[0]
        assert row.get("log_level") == level

    def test_read_filters_by_min_level(self, tmp_path: Path) -> None:
        payload = {
            "resourceLogs": [
                {
                    "scopeLogs": [
                        {
                            "logRecords": [
                                {"traceId": TRACE, "severityNumber": 5},
                                {"traceId": TRACE},
                                {"traceId": TRACE, "severityNumber": 17},
                            ]
                        }
                    ]
                }
            ]
        }
        _ingest(payload, tmp_path)
        assert len(read_log_records(tmp_path, trace_id=TRACE)) == 3
        assert len(read_log_records(tmp_path, trace_id=TRACE.upper(), min_level="info")) == 2
        assert [r["log_level"] for r in read_log_records(tmp_path, trace_id=TRACE, min_level="warn")] == [
            "error"
        ]
        assert len(read_log_records(tmp_path, trace_id=TRACE, limit=1)) == 1


class TestRead:
    def test_by_run(self, tmp_path: Path) -> None:
        _ingest(_payload(), tmp_path)
        rows = read_log_records(tmp_path, run_id=RUN_RES)
        assert len(rows) == 1 and rows[0]["service_name"] == "agent-svc"
        assert read_log_records(tmp_path, run_id="01NOSUCHRUN") == []

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"run_id": "a", "trace_id": TRACE},
            {"run_id": "../x"},
            {"trace_id": "nothex"},
            {"run_id": "a", "min_level": "fatal"},
        ],
    )
    def test_bad_arguments(self, tmp_path: Path, kwargs: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            read_log_records(tmp_path, **kwargs)

    def test_skips_symlinked_and_corrupt_streams(self, tmp_path: Path) -> None:
        (tmp_path / "traces").mkdir()
        target = tmp_path / "elsewhere.jsonl"
        target.write_text('{"x": 1}\n')
        (tmp_path / "traces" / f"{TRACE}.jsonl").symlink_to(target)
        assert read_log_records(tmp_path, trace_id=TRACE) == []
        (tmp_path / "runs").mkdir()
        (tmp_path / "runs" / "r1.jsonl").write_text('not json\n[1]\n{"ok": true}\n\n')
        assert read_log_records(tmp_path, run_id="r1") == [{"ok": True}]


class TestStoreSafety:
    def test_modes(self, tmp_path: Path) -> None:
        store = tmp_path / "store"
        _ingest(_payload(), store)
        assert stat.S_IMODE(os.stat(store).st_mode) == 0o700
        f = store / "traces" / f"{TRACE}.jsonl"
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600

    def test_symlinked_stream_dir_refused(self, tmp_path: Path) -> None:
        store = tmp_path / "store"
        store.mkdir()
        (tmp_path / "evil").mkdir()
        (store / "traces").symlink_to(tmp_path / "evil")
        with pytest.raises(OTLPLogsIngestError, match="not a real directory"):
            _ingest(_record(traceId=TRACE), store)
        assert list((tmp_path / "evil").iterdir()) == []

    def test_symlinked_stream_file_refused(self, tmp_path: Path) -> None:
        store = tmp_path / "store"
        (store / "traces").mkdir(parents=True)
        victim = tmp_path / "victim"
        victim.write_text("")
        (store / "traces" / f"{TRACE}.jsonl").symlink_to(victim)
        with pytest.raises(OTLPLogsIngestError, match="cannot open"):
            _ingest(_record(traceId=TRACE), store)
        assert victim.read_text() == ""

    def test_stream_size_cap_reports_partial_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = {
            "resourceLogs": [
                {"scopeLogs": [{"logRecords": [{"traceId": TRACE} for _ in range(5)]}]}
            ]
        }
        monkeypatch.setattr(logs_ingest, "MAX_STREAM_BYTES", 900)
        result = _ingest(payload, tmp_path)
        assert 0 < result.records_stored < 5
        assert result.rejected == 5 - result.records_stored
        resp = result.to_response()
        assert resp["partialSuccess"]["rejectedLogRecords"] == result.rejected
        assert (tmp_path / "traces" / f"{TRACE}.jsonl").stat().st_size <= 900

    def test_empty_export_writes_nothing(self, tmp_path: Path) -> None:
        result = _ingest({"resourceLogs": []}, tmp_path / "store")
        assert result.records_seen == 0
        assert not (tmp_path / "store").exists()
        assert result.to_response()["partialSuccess"] == {}


class TestBodyEntryPoint:
    def test_json_body(self, tmp_path: Path) -> None:
        out = ingest_otlp_logs_body(
            FIXTURE.read_bytes(), "application/json; charset=utf-8", store_dir=tmp_path,
            store_body=False,
        )
        assert out["records_stored"] == 4 and out["capsule_amended"] is False

    def test_invalid_json(self, tmp_path: Path) -> None:
        with pytest.raises(OTLPLogsIngestError, match="invalid JSON"):
            ingest_otlp_logs_body(b"{", "application/json", store_dir=tmp_path)

    def test_oversize(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(logs_ingest, "MAX_REQUEST_BYTES", 10)
        with pytest.raises(OTLPLogsIngestError, match="exceeds"):
            ingest_otlp_logs_body(b"x" * 11, "application/json", store_dir=tmp_path)

    def test_bad_protobuf(self, tmp_path: Path) -> None:
        pytest.importorskip("opentelemetry.proto")
        with pytest.raises(OTLPLogsIngestError):
            ingest_otlp_logs_body(b"\xff\xff\xff", "application/x-protobuf", store_dir=tmp_path)

    def test_default_store_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("NOVAFABRIC_OTLP_LOG_DIR", str(tmp_path / "x"))
        assert default_log_store_dir() == tmp_path / "x"
        monkeypatch.delenv("NOVAFABRIC_OTLP_LOG_DIR")
        monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
        assert default_log_store_dir() == tmp_path / "home" / "otlp-logs"


class TestProtobuf:
    def test_protobuf_matches_json(self, tmp_path: Path) -> None:
        pytest.importorskip("opentelemetry.proto")
        from google.protobuf.json_format import ParseDict
        from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
            ExportLogsServiceRequest,
        )

        payload = _payload()
        # OTLP/JSON carries ids as hex; the protobuf JSON mapping expects base64.
        import base64

        for rl in payload["resourceLogs"]:
            for sl in rl["scopeLogs"]:
                for lr in sl["logRecords"]:
                    for k in ("traceId", "spanId"):
                        if k in lr:
                            lr[k] = base64.b64encode(bytes.fromhex(lr[k])).decode()
        msg = ParseDict(payload, ExportLogsServiceRequest())
        pb_records = parse_otlp_logs_protobuf(msg.SerializeToString())
        json_records = parse_otlp_logs_json(_payload())
        assert len(pb_records) == len(json_records)
        for pb, js in zip(pb_records, json_records, strict=True):
            assert pb["trace_id"] == js["trace_id"]
            assert pb["span_id"] == js["span_id"]
            assert pb["body"] == js["body"]
            assert pb["time_unix_nano"] == js["time_unix_nano"]
        ingest_otlp_logs(pb_records, tmp_path / "pb", now=NOW)
        ingest_otlp_logs(json_records, tmp_path / "js", now=NOW)
        for rel in (f"traces/{TRACE}.jsonl", f"runs/{RUN_RES}.jsonl", f"runs/{RUN_REC}.jsonl"):
            pb_rows = _lines(tmp_path / "pb" / rel)
            js_rows = _lines(tmp_path / "js" / rel)
            for a, b in zip(pb_rows, js_rows, strict=True):
                assert a.get("log_level") == b.get("log_level")
                assert a["body"] == b["body"]


class TestRoute:
    def test_registered_route(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("fastapi")
        from fastapi import FastAPI, Header, HTTPException
        from fastapi.testclient import TestClient

        monkeypatch.setenv("NOVAFABRIC_OTLP_LOG_DIR", str(tmp_path / "logs"))

        async def verify_token(authorization: str = Header(default="")) -> None:
            if authorization != "Bearer t":
                raise HTTPException(status_code=401)

        app = FastAPI()
        logs_ingest.register_otlp_logs_route(
            app, capsule_dir=tmp_path / "caps", verify_token=verify_token
        )
        client = TestClient(app)
        assert client.post("/api/otlp/v1/logs", json=_payload()).status_code == 401
        ok = client.post(
            "/api/otlp/v1/logs", json=_payload(), headers={"Authorization": "Bearer t"}
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["records_stored"] == 4
        bad = client.post(
            "/api/otlp/v1/logs", json={"nope": 1}, headers={"Authorization": "Bearer t"}
        )
        assert bad.status_code == 400
