"""ADR-0191 D3 rotating-file sink for `nova audit-log tail`.

Covers: rotation at the size threshold; records are never split across a
boundary; generation retention and deletion of the oldest; backup_count=0
truncation; append-on-restart; configuration rejection; and the CLI wiring
(including that redaction still applies through the sink).
"""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from novafabric.audit.sinks import (
    MIN_MAX_BYTES,
    TRUNCATION_MARKER,
    RotatingFileSink,
    SinkError,
    SyslogSink,
)
from novafabric.cli.main import app

runner = CliRunner()

SECRET = "sk-ant-api03-verysecretvalue0123456789"


def _gen(path: Path, n: int) -> Path:
    return path.with_suffix(path.suffix + f".{n}")


def test_writes_through_without_rotating_below_threshold(tmp_path: Path) -> None:
    path = tmp_path / "out.jsonl"
    with RotatingFileSink(path, max_bytes=MIN_MAX_BYTES) as sink:
        sink.write("a" * 100 + "\n")
        sink.write("b" * 100 + "\n")
    assert sink.rotations == 0
    assert not _gen(path, 1).exists()
    assert path.read_text(encoding="utf-8").count("\n") == 2


def test_rotates_at_threshold_and_keeps_generations(tmp_path: Path) -> None:
    path = tmp_path / "out.jsonl"
    line = "x" * 500 + "\n"  # 501 bytes
    with RotatingFileSink(path, max_bytes=MIN_MAX_BYTES, backup_count=3) as sink:
        for _ in range(6):
            sink.write(line)
    assert sink.rotations >= 2
    assert _gen(path, 1).exists()
    # Every retained file holds only whole lines.
    for candidate in (path, _gen(path, 1), _gen(path, 2)):
        if candidate.exists():
            text = candidate.read_text(encoding="utf-8")
            assert text.endswith("\n")
            assert all(ln == "x" * 500 for ln in text.splitlines())


def test_a_record_is_never_split_across_a_rotation(tmp_path: Path) -> None:
    """A half-record is unparseable to the collector — rotate first."""
    path = tmp_path / "out.jsonl"
    payload = json.dumps({"k": "v" * 300}) + "\n"
    with RotatingFileSink(path, max_bytes=MIN_MAX_BYTES, backup_count=5) as sink:
        for _ in range(8):
            sink.write(payload)

    for candidate in [path] + [_gen(path, i) for i in range(1, 6)]:
        if not candidate.exists():
            continue
        for ln in candidate.read_text(encoding="utf-8").splitlines():
            json.loads(ln)  # raises if a record was split


def test_backup_count_bounds_the_number_of_files(tmp_path: Path) -> None:
    path = tmp_path / "out.jsonl"
    with RotatingFileSink(path, max_bytes=MIN_MAX_BYTES, backup_count=2) as sink:
        for _ in range(20):
            sink.write("y" * 600 + "\n")
    assert not _gen(path, 3).exists(), "oldest generation was not deleted"
    kept = [p for p in (path, _gen(path, 1), _gen(path, 2)) if p.exists()]
    assert len(kept) <= 3


def test_backup_count_zero_truncates_and_keeps_nothing(tmp_path: Path) -> None:
    path = tmp_path / "out.jsonl"
    with RotatingFileSink(path, max_bytes=MIN_MAX_BYTES, backup_count=0) as sink:
        for _ in range(10):
            sink.write("z" * 600 + "\n")
    assert not _gen(path, 1).exists()
    assert path.stat().st_size <= MIN_MAX_BYTES


def test_reopening_appends_rather_than_destroying(tmp_path: Path) -> None:
    """A restarted tailer must not wipe what it already shipped."""
    path = tmp_path / "out.jsonl"
    with RotatingFileSink(path, max_bytes=MIN_MAX_BYTES) as sink:
        sink.write("first\n")
    with RotatingFileSink(path, max_bytes=MIN_MAX_BYTES) as sink:
        sink.write("second\n")
    assert path.read_text(encoding="utf-8").splitlines() == ["first", "second"]


def test_oversized_single_record_is_written_whole(tmp_path: Path) -> None:
    """Truncating a record would corrupt the stream — keep it intact."""
    path = tmp_path / "out.jsonl"
    huge = "q" * (MIN_MAX_BYTES * 3) + "\n"
    with RotatingFileSink(path, max_bytes=MIN_MAX_BYTES) as sink:
        sink.write("small\n")
        sink.write(huge)
    written = path.read_text(encoding="utf-8")
    assert huge.strip() in written


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_bytes": 10},
        {"max_bytes": 0},
        {"backup_count": -1},
    ],
)
def test_absurd_configuration_is_rejected(tmp_path: Path, kwargs: Any) -> None:
    with pytest.raises(SinkError):
        RotatingFileSink(tmp_path / "out.jsonl", **kwargs)


def test_creates_missing_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deeper" / "out.jsonl"
    with RotatingFileSink(path, max_bytes=MIN_MAX_BYTES) as sink:
        sink.write("ok\n")
    assert path.exists()


# ---------------------------------------------------------------------------
# RFC 5424 syslog sink (ADR-0191 D3) — local endpoints only
# ---------------------------------------------------------------------------


def _parse_5424(raw: bytes) -> dict[str, str]:
    """Split an RFC 5424 message into its header fields plus MSG."""
    text = raw.decode("utf-8")
    pri, _, rest = text.partition(">")
    version, ts, host, app, procid, msgid, sd, *msg = rest.split(" ", 7)
    return {
        "pri": pri.lstrip("<"),
        "version": version,
        "timestamp": ts,
        "hostname": host,
        "app_name": app,
        "procid": procid,
        "msgid": msgid,
        "structured_data": sd,
        "msg": msg[0] if msg else "",
    }


def test_syslog_udp_emits_wellformed_rfc5424(tmp_path: Path) -> None:
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(5)
    port = receiver.getsockname()[1]

    with SyslogSink(f"127.0.0.1:{port}", transport="udp", msgid="audit-cef") as sink:
        sink.write('{"action":"retention.apply"}\n')

    raw, _addr = receiver.recvfrom(65535)
    receiver.close()
    parsed = _parse_5424(raw)

    # PRI = facility*8 + severity = 16*8 + 6 = 134 (local0.info)
    assert parsed["pri"] == "134"
    assert parsed["version"] == "1"
    assert parsed["timestamp"].endswith("Z")
    assert parsed["app_name"] == "novafabric"
    assert parsed["procid"] == str(os.getpid())
    assert parsed["msgid"] == "audit-cef"
    assert parsed["structured_data"] == "-"
    assert '"action":"retention.apply"' in parsed["msg"]
    assert sink.messages_sent == 1


def test_syslog_unix_socket(tmp_path: Path) -> None:
    sock_path = tmp_path / "log.sock"
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    receiver.bind(str(sock_path))
    receiver.settimeout(5)

    with SyslogSink(str(sock_path)) as sink:
        sink.write("hello\n")

    raw = receiver.recv(65535)
    receiver.close()
    assert _parse_5424(raw)["msg"] == "hello"


def test_syslog_tcp_uses_octet_counting(tmp_path: Path) -> None:
    """RFC 6587 framing — a stream receiver must find message boundaries."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    port = listener.getsockname()[1]

    received: list[bytes] = []

    def _serve() -> None:
        conn, _ = listener.accept()
        with conn:
            data = b""
            while len(data) < 200:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            received.append(data)

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()

    with SyslogSink(f"127.0.0.1:{port}", transport="tcp") as sink:
        sink.write("first\n")
        sink.write("second\n")
    thread.join(timeout=5)
    listener.close()

    data = received[0]
    # Each frame is "<length> <message>"; length must parse and match.
    length_text, _, remainder = data.partition(b" ")
    declared = int(length_text)
    assert remainder[:declared].endswith(b"first")


def test_syslog_refuses_non_loopback_host() -> None:
    """ADR-0191 D3 scopes this to a LOCAL endpoint — enforced, not advised."""
    for address in ("10.0.0.5:514", "198.51.100.7:514", "syslog.example.com:514"):
        with pytest.raises(SinkError, match="not loopback"):
            SyslogSink(address, transport="udp")


def test_syslog_accepts_loopback_forms() -> None:
    for address in ("127.0.0.1:0", "localhost:0", "127.5.5.5:0"):
        # Connect may still fail on port 0; we only assert the guard passes.
        try:
            SyslogSink(address, transport="udp").close()
        except SinkError as exc:
            assert "not loopback" not in str(exc)


def test_syslog_rejects_bad_configuration() -> None:
    with pytest.raises(SinkError, match="transport"):
        SyslogSink("127.0.0.1:514", transport="carrier-pigeon")
    with pytest.raises(SinkError, match="facility"):
        SyslogSink("127.0.0.1:514", transport="udp", facility=99)
    with pytest.raises(SinkError, match="host:port"):
        SyslogSink("no-port-here", transport="udp")


def test_syslog_truncation_is_marked_not_silent(tmp_path: Path) -> None:
    """A silently shortened audit record reads as a complete one."""
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(5)
    port = receiver.getsockname()[1]

    with SyslogSink(
        f"127.0.0.1:{port}", transport="udp", max_datagram_bytes=512
    ) as sink:
        sink.write("x" * 4000 + "\n")
        assert sink.messages_truncated == 1

    raw, _ = receiver.recvfrom(65535)
    receiver.close()
    assert len(raw) <= 512
    assert TRUNCATION_MARKER in raw.decode("utf-8")


def test_syslog_skips_blank_writes(tmp_path: Path) -> None:
    sock_path = tmp_path / "blank.sock"
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    receiver.bind(str(sock_path))
    with SyslogSink(str(sock_path)) as sink:
        sink.write("\n")
        assert sink.messages_sent == 0
    receiver.close()


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def _dashboard_log(path: Path) -> None:
    record = {
        "audit_id": "a1",
        "ts": "2026-07-12T12:00:00+00:00",
        "action": "retention.apply",
        "args": {"note": f"leaked {SECRET}"},
        "actor_token_fp": "fp",
        "result": "ok",
    }
    path.write_text(json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8")


def test_cli_tail_out_writes_a_rotating_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "dash.jsonl"
    _dashboard_log(src)
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(src))
    out = tmp_path / "shipped.jsonl"

    result = runner.invoke(
        app,
        ["audit-log", "tail", "--source", "dashboard", "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    text = out.read_text(encoding="utf-8")
    assert "retention.apply" in text
    # Redaction still applies through the sink.
    assert SECRET not in text
    # ...and stdout stayed clean, since output went to the file.
    assert "retention.apply" not in result.stdout


def test_cli_tail_syslog_sink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "dash.jsonl"
    _dashboard_log(src)
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(src))

    sock_path = tmp_path / "cli.sock"
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    receiver.bind(str(sock_path))
    receiver.settimeout(5)

    result = runner.invoke(
        app,
        ["audit-log", "tail", "--source", "dashboard", "--syslog", str(sock_path)],
    )
    assert result.exit_code == 0, result.output

    # Manifest first, then the entry — both as RFC 5424 messages.
    manifest = _parse_5424(receiver.recv(65535))
    entry = _parse_5424(receiver.recv(65535))
    receiver.close()
    assert "nova_siem_export" in manifest["msg"]
    assert "retention.apply" in entry["msg"]
    assert SECRET not in entry["msg"]  # redaction applies through the sink
    assert entry["msgid"] == "audit-jsonl"


def test_cli_tail_refuses_non_loopback_syslog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "dash.jsonl"
    _dashboard_log(src)
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(src))
    result = runner.invoke(
        app,
        [
            "audit-log", "tail", "--source", "dashboard",
            "--syslog", "10.0.0.5:514", "--syslog-transport", "udp",
        ],
    )
    assert result.exit_code == 2
    assert "not loopback" in result.output


def test_cli_tail_out_and_syslog_are_mutually_exclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "dash.jsonl"
    _dashboard_log(src)
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(src))
    result = runner.invoke(
        app,
        [
            "audit-log", "tail", "--source", "dashboard",
            "--out", str(tmp_path / "o.jsonl"), "--syslog", "/dev/log",
        ],
    )
    assert result.exit_code != 0


def test_cli_tail_rejects_absurd_rotation_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "dash.jsonl"
    _dashboard_log(src)
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(src))
    result = runner.invoke(
        app,
        [
            "audit-log", "tail", "--source", "dashboard",
            "--out", str(tmp_path / "o.jsonl"), "--max-bytes", "10",
        ],
    )
    assert result.exit_code == 2
