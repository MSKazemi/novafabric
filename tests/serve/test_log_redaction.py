"""Issue #278 — a ``?token=`` value never reaches ``nova serve``'s logs.

The dashboard sends its token in the query string (``EventSource`` cannot set a
header), so a request line carries it. ``nova serve`` keeps uvicorn's access log
off; these tests pin the second layer: the redacting filter on uvicorn's loggers,
and that ``nova serve`` installs it before the server starts.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from novafabric.serve.log_redaction import (
    REDACTED,
    UVICORN_LOGGERS,
    TokenQueryRedactingFilter,
    install_token_redaction,
    redact_record,
    redact_token_query,
)

SECRET = "nf-secret-0123456789abcdef"
SECRET2 = "nf-second-secret-fedcba9876543210"


@pytest.fixture(autouse=True)
def _restore_record_factory() -> Iterator[None]:
    # install_token_redaction wraps the process-wide record factory; undo it per test.
    saved = logging.getLogRecordFactory()
    yield
    logging.setLogRecordFactory(saved)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"GET /api/runs/stream?token={SECRET}", f"GET /api/runs/stream?token={REDACTED}"),
        (f"/api/runs?limit=5&token={SECRET}&x=1", f"/api/runs?limit=5&token={REDACTED}&x=1"),
        (f"/topology/?token={SECRET}#tv5", f"/topology/?token={REDACTED}#tv5"),
        (f"/x?access_token={SECRET}", f"/x?access_token={REDACTED}"),
        (f"/x?TOKEN={SECRET}", f"/x?TOKEN={REDACTED}"),
        (f'"/x?token={SECRET}"', f'"/x?token={REDACTED}"'),
        (f"token={SECRET}", f"token={REDACTED}"),
    ],
)
def test_token_values_are_redacted(text: str, expected: str) -> None:
    assert redact_token_query(text) == expected
    assert SECRET not in redact_token_query(text)


@pytest.mark.parametrize("text", ["/api/runs?limit=5", "/x?tok=1", "token=", "no query at all"])
def test_text_without_a_token_value_is_unchanged(text: str) -> None:
    assert redact_token_query(text) == text


def _capture(name: str) -> tuple[logging.Logger, list[str]]:
    lines: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(self.format(record))

    target = logging.getLogger(name)
    target.handlers = [_Collect()]
    target.propagate = False
    target.setLevel(logging.INFO)
    return target, lines


def test_an_access_log_line_is_written_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    name = "novafabric.tests.access-redaction"
    target, lines = _capture(name)
    monkeypatch.setattr(target, "filters", [])
    install_token_redaction([name])
    # uvicorn's access-log call shape: positional args, status code as an int.
    target.info(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:5555",
        "GET",
        f"/api/runs/stream?token={SECRET}",
        "1.1",
        200,
    )
    assert lines == [f'127.0.0.1:5555 - "GET /api/runs/stream?token={REDACTED} HTTP/1.1" 200']


def test_the_message_and_mapping_args_are_redacted_too() -> None:
    f = TokenQueryRedactingFilter()
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, f"ws /x?token={SECRET}", None, None)
    assert f.filter(rec) is True
    assert SECRET not in rec.getMessage()
    rec = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "%(path)s", ({"path": f"?token={SECRET}"},), None
    )
    f.filter(rec)
    assert SECRET not in rec.getMessage()


def _record(msg: object, args: object) -> logging.LogRecord:
    return logging.LogRecord("x", logging.INFO, __file__, 1, msg, args, None)  # type: ignore[arg-type]


def test_a_token_in_the_format_string_keeps_the_record_formattable() -> None:
    # Review of #361: redacting the format string alone turned "token=%s" into
    # "token=[REDACTED]", which no longer consumes its arg; formatting raised and
    # logging printed the raw args to stderr.
    rec = _record("GET /x?token=%s HTTP/1.1", (SECRET2,))
    redact_record(rec)
    message = rec.getMessage()
    assert message == f"GET /x?token={REDACTED} HTTP/1.1"
    assert SECRET2 not in message


def test_an_unformattable_record_becomes_a_plain_redacted_string() -> None:
    rec = _record(f"/x?token={SECRET} %s %s", ("only-one",))
    redact_record(rec)
    message = rec.getMessage()  # must not raise
    assert SECRET not in message
    assert rec.args == ()


def test_nothing_secret_reaches_stderr_through_a_real_handler(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "novafabric.tests.access-stderr"
    target = logging.getLogger(name)
    stream = io.StringIO()
    monkeypatch.setattr(target, "handlers", [logging.StreamHandler(stream)])
    monkeypatch.setattr(target, "filters", [])
    monkeypatch.setattr(target, "propagate", False)
    monkeypatch.setattr(logging, "raiseExceptions", True)
    target.setLevel(logging.INFO)
    install_token_redaction([name])
    target.info("ws /api/runs/stream?token=%s", SECRET2)
    target.info(f"GET /x?token={SECRET} %s %s", "only-one")
    out = stream.getvalue()
    err = capsys.readouterr().err
    assert f"token={REDACTED}" in out
    assert SECRET not in out + err
    assert SECRET2 not in out + err
    assert "Logging error" not in err
    assert len(out.splitlines()) == 2


def test_child_loggers_are_redacted_too(monkeypatch: pytest.MonkeyPatch) -> None:
    parent = "novafabric.tests.access-parent"
    child, lines = _capture(parent + ".child")
    monkeypatch.setattr(child, "filters", [])
    install_token_redaction([parent])
    child.info("GET %s", f"/x?token={SECRET}")
    assert lines == [f"GET /x?token={REDACTED}"]
    # A sibling whose name merely starts with the same text is not a child.
    sibling, sibling_lines = _capture(parent + "x")
    sibling.info("GET %s", f"/x?token={SECRET}")
    assert sibling_lines == [f"GET /x?token={SECRET}"]


def test_the_record_factory_is_wrapped_once(monkeypatch: pytest.MonkeyPatch) -> None:
    name = "novafabric.tests.factory-once"
    monkeypatch.setattr(logging.getLogger(name), "filters", [])
    install_token_redaction([name])
    first = logging.getLogRecordFactory()
    install_token_redaction([name])
    assert logging.getLogRecordFactory() is first


def test_install_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    name = "novafabric.tests.access-idempotent"
    monkeypatch.setattr(logging.getLogger(name), "filters", [])
    install_token_redaction([name])
    install_token_redaction([name])
    filters = logging.getLogger(name).filters
    assert sum(isinstance(f, TokenQueryRedactingFilter) for f in filters) == 1


def test_nova_serve_installs_the_filter_before_uvicorn_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("uvicorn")
    from typer.testing import CliRunner  # noqa: PLC0415

    from novafabric.cli.main import app as nova_app  # noqa: PLC0415

    for name in UVICORN_LOGGERS:
        monkeypatch.setattr(logging.getLogger(name), "filters", [])
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)
    caps = tmp_path / "runs"
    caps.mkdir()

    seen: dict[str, bool] = {}

    def _fake_run(*_args: object, **kwargs: object) -> None:
        for name in ("uvicorn.access", "uvicorn.error"):
            seen[name] = any(
                isinstance(f, TokenQueryRedactingFilter) for f in logging.getLogger(name).filters
            )
        seen["access_log_off"] = kwargs.get("access_log") is False

    with patch("uvicorn.run", side_effect=_fake_run):
        result = CliRunner().invoke(
            nova_app,
            ["serve", "--experimental", "--capsule-dir", str(caps), "--no-browser"],
        )
    assert result.exit_code == 0, result.output
    assert seen == {"uvicorn.access": True, "uvicorn.error": True, "access_log_off": True}
