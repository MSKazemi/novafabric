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

"""OTLP logs ingest into an append-only sidecar log store (ADR-0293, experimental).

ADR-0127 left standalone OTLP *logs* (``resourceLogs`` → ``scopeLogs`` →
``logRecords``) as future design because a span-less log record has nowhere to
go: the trace ingest (:mod:`genai_ingest`) seals one capsule per export, sealed
capsules are never amended, and an open run already has its single writer.

Decision (ADR-0293): log records land in a **sidecar store outside every
capsule** — one append-only JSONL stream per link key under
``$NOVAFABRIC_OTLP_LOG_DIR`` (default ``$NOVAFABRIC_HOME/otlp-logs``):

- ``runs/<run_id>.jsonl`` — the record or its resource carries ``novafabric.run_id``;
- ``traces/<trace_id>.jsonl`` — else a valid, non-zero 32-hex ``traceId``;
- ``unlinked/<YYYY-MM-DD>.jsonl`` — else by UTC day of the record time.

No capsule file is ever written. A linked capsule's sealed/unsealed state is
*observed* and stamped on the record (``capsule_state``); the response always
says ``capsule_amended: false``. The sidecar is correlation data, not evidence.

Content hygiene (ADR-0009, ADR-0021 §9): by default only metadata is stored —
times, severity (raw + canonical ``log_level``), ids, ``service.name``,
attribute *keys*, and the body's type, UTF-8 length and SHA-256. Body text and
string attribute values are stored only with ``store_body=True``
(``NOVAFABRIC_OTLP_LOGS_STORE_BODY=1``), redacted with the ADR-0009 rule pack
and truncated.

Bounds: :data:`MAX_REQUEST_BYTES`, :data:`MAX_RECORDS_PER_REQUEST` (over ⇒
:class:`OTLPLogsIngestError`, nothing written), :data:`MAX_ATTRIBUTE_KEYS` per
record, :data:`MAX_STREAM_BYTES` per sidecar file (records past it are rejected
and reported in the OTLP ``partialSuccess``, never silently dropped).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from novafabric.capture.log_level import (
    LOG_LEVELS,
    from_otel_severity,
    from_otel_severity_text,
    severity_rank,
)
from novafabric.otel.genai_ingest import (
    _b64_to_hex,
    _decode_any_value,
    _decode_attributes,
    _iso,
    _severity_number,
    _to_nanos,
)

__all__ = [
    "LOG_RECORD_SCHEMA",
    "MAX_RECORDS_PER_REQUEST",
    "MAX_REQUEST_BYTES",
    "MAX_STREAM_BYTES",
    "OTLPLogsIngestError",
    "LogIngestResult",
    "default_log_store_dir",
    "ingest_otlp_logs",
    "ingest_otlp_logs_body",
    "parse_otlp_logs_json",
    "parse_otlp_logs_protobuf",
    "read_log_records",
    "register_otlp_logs_route",
]

#: ``schema`` tag stamped on every stored record.
LOG_RECORD_SCHEMA = "novafabric/otlp-log-record/v0"
#: Attribute (record or resource) that links a log record to a run.
RUN_ID_ATTR = "novafabric.run_id"
#: Env var overriding the sidecar root.
ENV_LOG_DIR = "NOVAFABRIC_OTLP_LOG_DIR"
#: Env var opting in to redacted body/attribute-value storage.
ENV_STORE_BODY = "NOVAFABRIC_OTLP_LOGS_STORE_BODY"

MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_RECORDS_PER_REQUEST = 10_000
MAX_ATTRIBUTE_KEYS = 64
MAX_BODY_CHARS = 4096
MAX_ATTRIBUTE_VALUE_CHARS = 512
MAX_SEVERITY_TEXT_CHARS = 64
MAX_STREAM_BYTES = 64 * 1024 * 1024

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_TRUTHY = frozenset({"1", "true", "yes", "on"})


class OTLPLogsIngestError(ValueError):
    """The payload is not a decodable OTLP logs export, or exceeds a request bound."""


def default_log_store_dir() -> Path:
    """Sidecar root: ``$NOVAFABRIC_OTLP_LOG_DIR`` or ``$NOVAFABRIC_HOME/otlp-logs``.

    Deliberately outside any capsule directory (the ``dashboards_dir``
    precedent): capsules are sealed evidence and are never amended.
    """
    env = os.environ.get(ENV_LOG_DIR, "").strip()
    if env:
        return Path(env)
    from novafabric._paths import nova_home

    return nova_home() / "otlp-logs"


def store_body_from_env() -> bool:
    """True when ``NOVAFABRIC_OTLP_LOGS_STORE_BODY`` opts in to body storage."""
    return os.environ.get(ENV_STORE_BODY, "").strip().lower() in _TRUTHY


# ── parsing ──────────────────────────────────────────────────────────────────


def _hex_id(value: Any, pattern: re.Pattern[str]) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    candidate = value.lower()
    if not pattern.fullmatch(candidate):
        candidate = str(_b64_to_hex(value)).lower()
    if not pattern.fullmatch(candidate) or set(candidate) == {"0"}:
        return None
    return candidate


def parse_otlp_logs_json(payload: Any) -> list[dict[str, Any]]:
    """Flatten an OTLP/JSON ``ExportLogsServiceRequest`` into normalized records.

    Pure function. Each record carries ``attributes`` (decoded), ``resource``
    (decoded resource attributes), ``body`` (decoded ``AnyValue``),
    ``time_unix_nano``, ``observed_time_unix_nano``, ``severity_number``
    (raw wire value), ``severity_text``, ``trace_id``/``span_id`` (lowercase
    hex or None), ``event_name``.

    Raises:
        OTLPLogsIngestError: not an object with a ``resourceLogs`` list, or more
            than :data:`MAX_RECORDS_PER_REQUEST` records.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("resourceLogs"), list):
        raise OTLPLogsIngestError("payload is not an OTLP logs export (no resourceLogs list)")
    records: list[dict[str, Any]] = []
    for rl in payload["resourceLogs"]:
        if not isinstance(rl, dict):
            continue
        resource = rl.get("resource")
        resource_attrs = _decode_attributes(
            resource.get("attributes") if isinstance(resource, dict) else None
        )
        for sl in rl.get("scopeLogs") or []:
            if not isinstance(sl, dict):
                continue
            for lr in sl.get("logRecords") or []:
                if not isinstance(lr, dict):
                    continue
                if len(records) >= MAX_RECORDS_PER_REQUEST:
                    raise OTLPLogsIngestError(
                        f"export carries more than {MAX_RECORDS_PER_REQUEST} log records; "
                        "split the batch (ADR-0293 bound)"
                    )
                records.append(
                    {
                        "attributes": _decode_attributes(lr.get("attributes")),
                        "resource": resource_attrs,
                        "body": _decode_any_value(lr.get("body")),
                        "time_unix_nano": _to_nanos(lr.get("timeUnixNano")),
                        "observed_time_unix_nano": _to_nanos(lr.get("observedTimeUnixNano")),
                        "severity_number": lr.get("severityNumber"),
                        "severity_text": lr.get("severityText"),
                        "trace_id": _hex_id(lr.get("traceId"), _TRACE_ID_RE),
                        "span_id": _hex_id(lr.get("spanId"), _SPAN_ID_RE),
                        "event_name": lr.get("eventName")
                        if isinstance(lr.get("eventName"), str)
                        else None,
                    }
                )
    return records


def _protobuf_to_payload(data: bytes) -> dict[str, Any]:
    try:
        from google.protobuf.json_format import MessageToDict
        from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
            ExportLogsServiceRequest,
        )
    except ImportError as exc:  # pragma: no cover - import guard
        raise OTLPLogsIngestError(
            "OTLP/protobuf logs ingest requires the 'otlp' extra: "
            "pip install 'novafabric[otlp]' (adds opentelemetry-proto, Apache-2.0)."
        ) from exc
    req = ExportLogsServiceRequest()
    try:
        req.ParseFromString(data)
    except Exception as exc:  # protobuf DecodeError and friends
        raise OTLPLogsIngestError(f"not a decodable OTLP/protobuf logs export: {exc}") from exc
    payload: dict[str, Any] = MessageToDict(req)
    payload.setdefault("resourceLogs", [])
    return payload


def parse_otlp_logs_protobuf(data: bytes) -> list[dict[str, Any]]:
    """Binary sibling of :func:`parse_otlp_logs_json` (ids arrive base64, normalized to hex)."""
    return parse_otlp_logs_json(_protobuf_to_payload(data))


# ── record shaping ───────────────────────────────────────────────────────────


def _level(record: dict[str, Any]) -> str | None:
    """Canonical ADR-0127 level; the number is authoritative, the text a fallback."""
    number = record.get("severity_number")
    if number is not None and number != 0 and number != "SEVERITY_NUMBER_UNSPECIFIED":
        return from_otel_severity(_severity_number(number))
    return from_otel_severity_text(record.get("severity_text"))


def _body_digest(body: Any) -> tuple[str, str]:
    """``(type, canonical text)`` for the body — the text is hashed, stored only on opt-in."""
    if body is None:
        return "empty", ""
    if isinstance(body, str):
        return "string", body
    if isinstance(body, bool):
        return "bool", json.dumps(body)
    if isinstance(body, (int, float)):
        return "number", json.dumps(body)
    if isinstance(body, list):
        return "array", json.dumps(body, sort_keys=True, default=str)
    if isinstance(body, dict):
        return "kvlist", json.dumps(body, sort_keys=True, default=str)
    return "other", str(body)


@dataclass
class _Redactor:
    count: int = 0

    def __call__(self, text: str, limit: int) -> str:
        from novafabric.capture.secrets import redact_secrets_in_text

        cleaned = redact_secrets_in_text(text)
        if cleaned != text:
            self.count += 1
        return cleaned[:limit]


def _run_id(record: dict[str, Any]) -> str | None:
    for source in (record["attributes"], record["resource"]):
        value = source.get(RUN_ID_ATTR)
        if isinstance(value, str) and _RUN_ID_RE.fullmatch(value):
            return value
    return None


def _capsule_state(run_id: str, capsule_dir: Path | None) -> str:
    if capsule_dir is None:
        return "unknown"
    cdir = capsule_dir / run_id
    if not (cdir / "capsule.yaml").is_file():
        return "absent"
    from novafabric.capsule._manifest_write import is_sealed

    return "sealed" if is_sealed(cdir) else "unsealed"


def _shape(
    record: dict[str, Any],
    *,
    run_id: str | None,
    capsule_state: str | None,
    store_body: bool,
    redact: _Redactor,
    ingested_at: str,
) -> dict[str, Any]:
    time_iso = _iso(record["time_unix_nano"]) or _iso(record["observed_time_unix_nano"])
    body_type, body_text = _body_digest(record["body"])
    body_bytes = body_text.encode("utf-8")
    attrs = record["attributes"]
    keys = sorted(str(k) for k in attrs)
    out: dict[str, Any] = {
        "schema": LOG_RECORD_SCHEMA,
        "ingested_at": ingested_at,
        "time": time_iso,
        "observed_time": _iso(record["observed_time_unix_nano"]),
        "trace_id": record["trace_id"],
        "span_id": record["span_id"],
        "run_id": run_id,
        "body": {
            "type": body_type,
            "bytes": len(body_bytes),
            "sha256": hashlib.sha256(body_bytes).hexdigest(),
        },
        "attribute_keys": keys[:MAX_ATTRIBUTE_KEYS],
    }
    if len(keys) > MAX_ATTRIBUTE_KEYS:
        out["attribute_keys_truncated"] = len(keys) - MAX_ATTRIBUTE_KEYS
    if capsule_state is not None:
        out["capsule_state"] = capsule_state
    number = record["severity_number"]
    if isinstance(number, (int, str)) and not isinstance(number, bool):
        out["severity_number"] = number
    text = record["severity_text"]
    if isinstance(text, str) and text:
        out["severity_text"] = redact(text, MAX_SEVERITY_TEXT_CHARS)
    level = _level(record)
    if level is not None:
        out["log_level"] = level
    service = record["resource"].get("service.name")
    if isinstance(service, str) and service:
        out["service_name"] = redact(service, MAX_ATTRIBUTE_VALUE_CHARS)
    if isinstance(record.get("event_name"), str):
        out["event_name"] = redact(record["event_name"], MAX_ATTRIBUTE_VALUE_CHARS)
    if store_body:
        if body_text:
            out["body"]["text"] = redact(body_text, MAX_BODY_CHARS)
            out["body"]["truncated"] = len(body_text) > MAX_BODY_CHARS
        values: dict[str, Any] = {}
        for key in keys[:MAX_ATTRIBUTE_KEYS]:
            value = attrs[key]
            if isinstance(value, str):
                values[key] = redact(value, MAX_ATTRIBUTE_VALUE_CHARS)
            elif isinstance(value, (bool, int, float)) or value is None:
                values[key] = value
            else:
                values[key] = redact(
                    json.dumps(value, sort_keys=True, default=str), MAX_ATTRIBUTE_VALUE_CHARS
                )
        out["attributes"] = values
    return out


# ── sidecar store ────────────────────────────────────────────────────────────


def _secure_dir(path: Path) -> None:
    """Create *path* (mode 0700) and refuse a symlink or non-directory."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise OTLPLogsIngestError(f"log store path {path} is not a real directory; refusing")


def _append_lines(path: Path, lines: list[str]) -> int:
    """Append *lines* to *path* while it stays under :data:`MAX_STREAM_BYTES`.

    One ``O_APPEND`` write under ``flock`` (where available); ``O_NOFOLLOW``
    refuses a symlinked file. Returns how many lines were written (a prefix).
    """
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise OTLPLogsIngestError(
            f"cannot open log stream {path.name}: {type(exc).__name__}"
        ) from exc
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - non-POSIX
            pass
        size = os.fstat(fd).st_size
        chunk: list[bytes] = []
        for line in lines:
            data = (line + "\n").encode("utf-8")
            if size + len(data) > MAX_STREAM_BYTES:
                break
            chunk.append(data)
            size += len(data)
        if chunk:
            os.write(fd, b"".join(chunk))
        return len(chunk)
    finally:
        os.close(fd)


@dataclass
class LogIngestResult:
    """Outcome of one logs export."""

    records_seen: int = 0
    records_stored: int = 0
    rejected: int = 0
    linked_runs: list[str] = field(default_factory=list)
    linked_traces: int = 0
    unlinked: int = 0
    sealed_runs: list[str] = field(default_factory=list)
    redacted_fields: int = 0
    store_body: bool = False

    def to_response(self) -> dict[str, Any]:
        """OTLP ``ExportLogsServiceResponse``-compatible body plus NovaFabric counters."""
        body: dict[str, Any] = {
            "records_seen": self.records_seen,
            "records_stored": self.records_stored,
            "linked_runs": self.linked_runs,
            "linked_traces": self.linked_traces,
            "unlinked": self.unlinked,
            "sealed_runs": self.sealed_runs,
            "redacted_fields": self.redacted_fields,
            "store_body": self.store_body,
            "capsule_amended": False,
            "storage": "otlp-log-sidecar (not sealed evidence; ADR-0293)",
        }
        if self.rejected:
            body["partialSuccess"] = {
                "rejectedLogRecords": self.rejected,
                "errorMessage": "log stream size cap reached (ADR-0293 bound)",
            }
        else:
            body["partialSuccess"] = {}
        return body


def ingest_otlp_logs(
    records: list[dict[str, Any]],
    store_dir: Path,
    *,
    capsule_dir: Path | None = None,
    store_body: bool = False,
    now: datetime | None = None,
) -> LogIngestResult:
    """Append normalized *records* to the sidecar store under *store_dir*.

    Never writes inside *capsule_dir*; it is consulted only to stamp each
    run-linked record's ``capsule_state``.
    """
    now = now or datetime.now(timezone.utc)
    ingested_at = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    redact = _Redactor()
    result = LogIngestResult(records_seen=len(records), store_body=store_body)
    streams: dict[tuple[str, str], list[str]] = {}
    states: dict[str, str] = {}
    traces: set[str] = set()
    for record in records:
        run_id = _run_id(record)
        state: str | None = None
        if run_id is not None:
            if run_id not in states:
                states[run_id] = _capsule_state(run_id, capsule_dir)
            state = states[run_id]
            key = ("runs", run_id)
        elif record["trace_id"] is not None:
            key = ("traces", record["trace_id"])
            traces.add(record["trace_id"])
        else:
            nanos = record["time_unix_nano"] or record["observed_time_unix_nano"]
            day = (
                datetime.fromtimestamp(nanos / 1e9, tz=timezone.utc) if nanos > 0 else now
            ).strftime("%Y-%m-%d")
            key = ("unlinked", day)
            result.unlinked += 1
        shaped = _shape(
            record,
            run_id=run_id,
            capsule_state=state,
            store_body=store_body,
            redact=redact,
            ingested_at=ingested_at,
        )
        streams.setdefault(key, []).append(json.dumps(shaped, sort_keys=True))
    if streams:
        _secure_dir(store_dir)
    for (kind, name), lines in sorted(streams.items()):
        sub = store_dir / kind
        _secure_dir(sub)
        written = _append_lines(sub / f"{name}.jsonl", lines)
        result.records_stored += written
        result.rejected += len(lines) - written
    result.linked_runs = sorted(states)
    result.sealed_runs = sorted(r for r, s in states.items() if s == "sealed")
    result.linked_traces = len(traces)
    result.redacted_fields = redact.count
    return result


def ingest_otlp_logs_body(
    body: bytes,
    content_type: str,
    *,
    store_dir: Path | None = None,
    capsule_dir: Path | None = None,
    store_body: bool | None = None,
) -> dict[str, Any]:
    """Framework-free core of ``POST /api/otlp/v1/logs``: decode, store, respond.

    Raises:
        OTLPLogsIngestError: oversize body, malformed payload, or a store-path
            defect — the route maps it to HTTP 400.
    """
    if len(body) > MAX_REQUEST_BYTES:
        raise OTLPLogsIngestError(
            f"request body exceeds {MAX_REQUEST_BYTES} bytes (ADR-0293 bound)"
        )
    media = content_type.split(";", 1)[0].strip().lower()
    if media == "application/x-protobuf":
        records = parse_otlp_logs_protobuf(body)
    else:
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise OTLPLogsIngestError(f"invalid JSON body: {exc}") from exc
        records = parse_otlp_logs_json(payload)
    result = ingest_otlp_logs(
        records,
        store_dir if store_dir is not None else default_log_store_dir(),
        capsule_dir=capsule_dir,
        store_body=store_body_from_env() if store_body is None else store_body,
    )
    return result.to_response()


def register_otlp_logs_route(app: Any, *, capsule_dir: Path, verify_token: Any) -> None:
    """Register ``POST /api/otlp/v1/logs`` on a FastAPI *app* (coordinator wiring).

    One call from ``serve/app.py`` next to the traces route::

        register_otlp_logs_route(app, capsule_dir=capsule_dir, verify_token=verify_token)

    Token-guarded like ``/api/otlp/v1/traces``.
    """
    from fastapi import Depends, HTTPException, Request
    from pydantic import BaseModel, Field

    class OTLPLogsIngestResponse(BaseModel):
        """Declared (not bound) body of ``POST /api/otlp/v1/logs`` — see ``to_response``."""

        records_seen: int
        records_stored: int
        linked_runs: list[str]
        linked_traces: int
        unlinked: int
        sealed_runs: list[str]
        redacted_fields: int
        store_body: bool
        capsule_amended: bool
        storage: str
        partialSuccess: dict[str, Any] = Field(default_factory=dict)  # noqa: N815 — OTLP wire name

    async def otlp_ingest_logs(request: Request) -> dict[str, Any]:
        """Ingest an OTLP logs export into the sidecar log store (ADR-0293, experimental)."""
        try:
            return ingest_otlp_logs_body(
                await request.body(),
                request.headers.get("content-type", ""),
                capsule_dir=capsule_dir,
            )
        except OTLPLogsIngestError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ``from __future__ import annotations`` leaves "Request" a string that
    # FastAPI resolves against module globals; bind the real class here so the
    # import stays local (no import-time FastAPI dependency).
    otlp_ingest_logs.__annotations__["request"] = Request
    app.add_api_route(
        "/api/otlp/v1/logs",
        otlp_ingest_logs,
        methods=["POST"],
        dependencies=[Depends(verify_token)],
        operation_id="dashboardIngestOtlpLogs",
        responses={200: {"model": OTLPLogsIngestResponse}},
        response_model=None,
    )


# ── read path ────────────────────────────────────────────────────────────────


def _iter_stream(path: Path) -> Iterator[dict[str, Any]]:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row


def read_log_records(
    store_dir: Path,
    *,
    run_id: str | None = None,
    trace_id: str | None = None,
    min_level: str | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Read records for one run or one trace, optionally filtered by level.

    Exactly one of *run_id* / *trace_id*. *min_level* follows ADR-0129
    semantics: an absent ``log_level`` reads as ``info``. Bounded by *limit*.
    """
    if (run_id is None) == (trace_id is None):
        raise ValueError("pass exactly one of run_id or trace_id")
    if min_level is not None and min_level not in LOG_LEVELS:
        raise ValueError(f"min_level must be one of {LOG_LEVELS}")
    if run_id is not None:
        if not _RUN_ID_RE.fullmatch(run_id):
            raise ValueError("invalid run_id")
        path = store_dir / "runs" / f"{run_id}.jsonl"
    else:
        tid = (trace_id or "").lower()
        if not _TRACE_ID_RE.fullmatch(tid):
            raise ValueError("invalid trace_id")
        path = store_dir / "traces" / f"{tid}.jsonl"
    floor = severity_rank(min_level) if min_level is not None else None
    out: list[dict[str, Any]] = []
    for row in _iter_stream(path):
        level = row.get("log_level")
        if floor is not None:
            rank = severity_rank(level) if level in LOG_LEVELS else severity_rank("info")
            if rank < floor:
                continue
        out.append(row)
        if len(out) >= max(1, limit):
            break
    return out
