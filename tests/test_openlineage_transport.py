from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

_EVENTS: list[dict[str, Any]] = [{"eventType": "START"}, {"eventType": "COMPLETE"}]


def test_emit_if_configured_noop_when_no_env(monkeypatch: Any) -> None:
    monkeypatch.delenv("OPENLINEAGE_URL", raising=False)
    monkeypatch.delenv("OPENLINEAGE_FILE", raising=False)
    from novafabric.lineage._ol_transport import emit_if_configured
    emit_if_configured(_EVENTS)  # must not raise


def test_emit_if_configured_calls_file_when_file_set(tmp_path: Path, monkeypatch: Any) -> None:
    out = tmp_path / "ol.ndjson"
    monkeypatch.setenv("OPENLINEAGE_FILE", str(out))
    monkeypatch.delenv("OPENLINEAGE_URL", raising=False)
    from novafabric.lineage._ol_transport import emit_if_configured
    emit_if_configured(_EVENTS)
    lines = [line for line in out.read_text().splitlines() if line.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["eventType"] == "START"


def test_emit_if_configured_calls_http_when_url_set(monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENLINEAGE_URL", "http://localhost:9999")
    monkeypatch.delenv("OPENLINEAGE_FILE", raising=False)
    from novafabric.lineage._ol_transport import emit_if_configured
    posted: list[str] = []

    def fake_urlopen(req: Any, timeout: int = 5) -> Any:
        posted.append(req.full_url)
        m = MagicMock()
        m.__enter__ = lambda s: s
        m.__exit__ = MagicMock(return_value=False)
        m.status = 200
        return m

    with patch("urllib.request.urlopen", fake_urlopen):
        emit_if_configured(_EVENTS)
    assert len(posted) == 2
    assert all("/api/v1/lineage" in url for url in posted)


def test_emit_if_configured_both_when_both_set(tmp_path: Path, monkeypatch: Any) -> None:
    out = tmp_path / "ol.ndjson"
    monkeypatch.setenv("OPENLINEAGE_FILE", str(out))
    monkeypatch.setenv("OPENLINEAGE_URL", "http://localhost:9999")
    from novafabric.lineage._ol_transport import emit_if_configured
    posted: list[str] = []

    def fake_urlopen(req: Any, timeout: int = 5) -> Any:
        posted.append(req.full_url)
        m = MagicMock()
        m.__enter__ = lambda s: s
        m.__exit__ = MagicMock(return_value=False)
        m.status = 200
        return m

    with patch("urllib.request.urlopen", fake_urlopen):
        emit_if_configured(_EVENTS)
    assert len(posted) == 2  # HTTP was called
    lines = [line for line in out.read_text().splitlines() if line.strip()]
    assert len(lines) == 2  # file was also written


def test_emit_http_posts_to_api_lineage_endpoint(monkeypatch: Any) -> None:
    from novafabric.lineage._ol_transport import emit_http
    posted: list[str] = []

    def fake_urlopen(req: Any, timeout: int = 5) -> Any:
        posted.append(req.full_url)
        m = MagicMock()
        m.__enter__ = lambda s: s
        m.__exit__ = MagicMock(return_value=False)
        m.status = 200
        return m

    with patch("urllib.request.urlopen", fake_urlopen):
        emit_http([{"eventType": "START"}], "http://marquez:5000")
    assert posted == ["http://marquez:5000/api/v1/lineage"]


def test_emit_http_silent_on_connection_error(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setenv("OPENLINEAGE_URL", "http://localhost:19999")
    monkeypatch.delenv("OPENLINEAGE_FILE", raising=False)
    from novafabric.lineage._ol_transport import emit_if_configured
    emit_if_configured([{"eventType": "START"}])  # must not raise
    captured = capsys.readouterr()
    assert "OpenLineage" in captured.err


def test_emit_file_appends_ndjson(tmp_path: Path) -> None:
    from novafabric.lineage._ol_transport import emit_file
    out = tmp_path / "ol.ndjson"
    emit_file([{"eventType": "START"}], out)
    emit_file([{"eventType": "COMPLETE"}], out)
    lines = [line for line in out.read_text().splitlines() if line.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["eventType"] == "START"
    assert json.loads(lines[1])["eventType"] == "COMPLETE"


def test_emit_to_stdout(capsys: Any) -> None:
    from novafabric.lineage._ol_transport import emit_to
    emit_to([{"eventType": "START"}], "-")
    captured = capsys.readouterr()
    assert json.loads(captured.out.strip())["eventType"] == "START"


def test_emit_to_http_url(monkeypatch: Any) -> None:
    from novafabric.lineage._ol_transport import emit_to
    posted: list[str] = []

    def fake_urlopen(req: Any, timeout: int = 5) -> Any:
        posted.append(req.full_url)
        m = MagicMock()
        m.__enter__ = lambda s: s
        m.__exit__ = MagicMock(return_value=False)
        m.status = 200
        return m

    with patch("urllib.request.urlopen", fake_urlopen):
        emit_to([{"eventType": "START"}], "http://marquez:5000")
    assert len(posted) == 1


def test_emit_to_file_path(tmp_path: Path) -> None:
    from novafabric.lineage._ol_transport import emit_to
    out = tmp_path / "out.ndjson"
    emit_to([{"eventType": "START"}], str(out))
    assert json.loads(out.read_text().strip())["eventType"] == "START"
