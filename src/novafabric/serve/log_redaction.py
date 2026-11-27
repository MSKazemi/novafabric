"""Keep the dashboard token out of ``nova serve``'s logs (issue #278).

The dashboard still sends its token as ``?token=…`` (an ``EventSource`` cannot set
an ``Authorization`` header), so every request line carries it. ``nova serve``
runs uvicorn with its access log off and at ``warning`` level, which records no
request line today. This filter is the second layer: if those loggers are turned
up, or the app is run under another configuration, a ``…token=<value>`` in a log
line is written as ``…token=[REDACTED]``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping

#: What a redacted value is replaced with.
REDACTED = "[REDACTED]"

#: The loggers uvicorn writes request lines and handshake results to.
UVICORN_LOGGERS: tuple[str, ...] = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")

# Any query/form parameter whose name ends in "token" (token, access_token,
# id_token, …), at the start of the text, after ?, &, ; or whitespace, or after a
# quote. The value runs to the next &, #, whitespace or quote.
_TOKEN_PARAM_RE = re.compile(
    r"((?:^|[?&;\s\"'])[A-Za-z0-9_.-]*token=)[^&#\s\"']+",
    re.IGNORECASE,
)


def redact_token_query(text: str) -> str:
    """*text* with the value of every ``…token=`` parameter replaced by ``[REDACTED]``."""
    return _TOKEN_PARAM_RE.sub(lambda m: m.group(1) + REDACTED, text)


def _redact(value: object) -> object:
    return redact_token_query(value) if isinstance(value, str) else value


def redact_record(record: logging.LogRecord) -> None:
    """Redact ``…token=`` values in *record* in place, never leaving it unformattable.

    * Each string arg is redacted where it stands, so the args keep their position
      and type (uvicorn's access formatter unpacks them by position, and the status
      code stays an ``int``).
    * A record without args has its message redacted directly.
    * A record with args is formatted once. If a token value sits in the format
      string itself, the message becomes the redacted *formatted* text and the args
      are cleared. Redacting the format string alone could turn ``token=%s`` into
      ``token=[REDACTED]``, which no longer consumes its arg, so formatting would
      raise and ``logging`` would print the raw args to stderr.
    * A record that cannot be formatted at all becomes one plain, redacted string
      holding the message and the args, so the handler never reaches its error path.
    """
    args = record.args
    if isinstance(args, tuple):
        record.args = tuple(_redact(a) for a in args)
    elif isinstance(args, Mapping):
        record.args = {k: _redact(v) for k, v in args.items()}
    if not record.args:
        if isinstance(record.msg, str):
            record.msg = redact_token_query(record.msg)
        return
    try:
        formatted = record.getMessage()
    except Exception:  # noqa: BLE001 — any formatting error must not reach handleError
        record.msg = redact_token_query(f"{record.msg!s} {record.args!r}")
        record.args = ()
        return
    redacted = redact_token_query(formatted)
    if redacted != formatted:
        record.msg = redacted
        record.args = ()


def _covers(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name == p or name.startswith(p + ".") for p in prefixes)


class TokenQueryRedactingFilter(logging.Filter):
    """A logging filter that applies :func:`redact_record`. The record is never dropped."""

    def filter(self, record: logging.LogRecord) -> bool:
        redact_record(record)
        return True


#: Marks a record factory this module installed, so a second install is a no-op.
_FACTORY_MARK = "_novafabric_token_redaction_prefixes"


def install_token_redaction(logger_names: Iterable[str] = UVICORN_LOGGERS) -> None:
    """Redact ``…token=`` values in every record of the named loggers and their children.

    Two layers, both idempotent:

    * a :class:`TokenQueryRedactingFilter` on each named logger (logger filters
      survive uvicorn's own ``dictConfig`` call, which replaces handlers, not
      filters), and
    * a wrapper around the log-record factory that redacts any record whose logger
      is one of the names or a child of one (``uvicorn.error.x``). A logger filter
      only sees records logged on that exact logger, so children need this layer.
    """
    names = tuple(logger_names)
    for name in names:
        target = logging.getLogger(name)
        if not any(isinstance(f, TokenQueryRedactingFilter) for f in target.filters):
            target.addFilter(TokenQueryRedactingFilter())

    current = logging.getLogRecordFactory()
    installed: tuple[str, ...] = getattr(current, _FACTORY_MARK, ())
    missing = tuple(n for n in names if n not in installed)
    if not missing:
        return
    prefixes = installed + missing
    inner = getattr(current, "_novafabric_inner_factory", current)

    def _factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = inner(*args, **kwargs)
        if _covers(record.name, prefixes):
            redact_record(record)
        return record

    setattr(_factory, _FACTORY_MARK, prefixes)
    setattr(_factory, "_novafabric_inner_factory", inner)  # noqa: B010
    logging.setLogRecordFactory(_factory)
