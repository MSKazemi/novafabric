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


class TokenQueryRedactingFilter(logging.Filter):
    """A logging filter that redacts ``…token=`` values in a record's message and args.

    The args keep their position and type (uvicorn's access formatter unpacks them
    by position, and the status code stays an ``int``). The record is never dropped.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_token_query(record.msg)
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(_redact(a) for a in args)
        elif isinstance(args, Mapping):
            record.args = {k: _redact(v) for k, v in args.items()}
        return True


def install_token_redaction(logger_names: Iterable[str] = UVICORN_LOGGERS) -> None:
    """Attach one :class:`TokenQueryRedactingFilter` to each named logger (idempotent).

    Logger filters survive uvicorn's own ``dictConfig`` call (it replaces handlers,
    not filters), so installing before ``uvicorn.run`` is enough.
    """
    for name in logger_names:
        target = logging.getLogger(name)
        if not any(isinstance(f, TokenQueryRedactingFilter) for f in target.filters):
            target.addFilter(TokenQueryRedactingFilter())
