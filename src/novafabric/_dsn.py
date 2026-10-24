"""Postgres DSNs that never show or pass on their password.

Two needs, one module (review storage-backup-09/-10):

* :func:`redact_dsn` — what a DSN may look like on a console or in a log:
  ``host/dbname``, never a user, a password, or the raw string. Four copies of
  this had drifted apart; one printed a keyword DSN's ``password=`` verbatim and
  another cut a URL password at its first ``@``, leaking the tail.
* :func:`libpq_args` — a DSN for a libpq client binary (``pg_dump``,
  ``pg_restore``) with the password moved out of it into ``PGPASSWORD``. Process
  argv is world-readable (``/proc/<pid>/cmdline``, ``ps -ef``) for the life of the
  process; the environment is not.

Both URL DSNs (``postgresql://user:pass@host:5432/db?…``, including an unescaped
``@`` in the password and a ``password=`` query parameter) and libpq keyword DSNs
(``host=… password='…'``) are handled.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

_PASSWORD_ENV = "PGPASSWORD"


def redact_dsn(dsn: str) -> str:
    """Reduce a DSN to ``host/dbname`` — never credentials, never the raw string.

    Anything unparseable degrades to ``"unknown"`` parts rather than ever echoing
    the input.
    """
    host = "unknown"
    dbname = "unknown"
    if "://" in dsn:
        try:
            parts = urlsplit(dsn)
            host = parts.hostname or "unknown"
            dbname = parts.path.lstrip("/") or "unknown"
        except ValueError:
            pass
    else:
        try:
            pairs = _parse_keywords(dsn)
        except ValueError:
            pairs = []
        for key, value in pairs:
            if key == "host" and value:
                host = value
            elif key == "dbname" and value:
                dbname = value
    return f"{host}/{dbname}"


def libpq_args(dsn: str) -> tuple[str, dict[str, str]]:
    """``(dsn_without_password, env)`` for a libpq client binary.

    *env* is ``{"PGPASSWORD": password}`` when the DSN carried one, else empty;
    merge it into the child's environment. A DSN without a password is returned
    unchanged.

    Raises:
        ValueError: a keyword DSN that cannot be parsed (an unterminated quote).
            The message never contains the DSN.
    """
    if "://" in dsn:
        return _url_args(dsn)
    pairs = _parse_keywords(dsn)
    passwords = [value for key, value in pairs if key == "password"]
    if not passwords:
        return dsn, {}
    clean = " ".join(f"{key}={_quote(value)}" for key, value in pairs if key != "password")
    return clean, {_PASSWORD_ENV: passwords[-1]}  # libpq: the last one wins


def _url_args(dsn: str) -> tuple[str, dict[str, str]]:
    parts = urlsplit(dsn)
    userinfo, at, hostport = parts.netloc.rpartition("@")
    password: str | None = None
    netloc = parts.netloc
    if at:
        user, colon, raw_password = userinfo.partition(":")
        if colon:
            password = unquote(raw_password)
            netloc = f"{user}@{hostport}" if user else hostport
    query = parts.query
    if query:
        params = parse_qsl(query, keep_blank_values=True)
        kept = [(k, v) for k, v in params if k != "password"]
        if len(kept) != len(params):
            if password is None:
                password = [v for k, v in params if k == "password"][-1]
            query = urlencode(kept)
    if password is None:
        return dsn, {}
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment)), {
        _PASSWORD_ENV: password
    }


def _parse_keywords(dsn: str) -> list[tuple[str, str]]:
    """libpq ``key = value`` pairs; a value is bare or single-quoted (``\\'``, ``\\\\``)."""
    pairs: list[tuple[str, str]] = []
    i, n = 0, len(dsn)
    while True:
        while i < n and dsn[i].isspace():
            i += 1
        if i >= n:
            return pairs
        start = i
        while i < n and dsn[i] != "=" and not dsn[i].isspace():
            i += 1
        key = dsn[start:i]
        while i < n and dsn[i].isspace():
            i += 1
        if i >= n or dsn[i] != "=" or not key:
            raise ValueError("malformed keyword DSN: expected key=value")
        i += 1
        while i < n and dsn[i].isspace():
            i += 1
        value: list[str] = []
        if i < n and dsn[i] == "'":
            i += 1
            while True:
                if i >= n:
                    raise ValueError("malformed keyword DSN: unterminated quoted value")
                ch = dsn[i]
                if ch == "\\" and i + 1 < n:
                    value.append(dsn[i + 1])
                    i += 2
                elif ch == "'":
                    i += 1
                    break
                else:
                    value.append(ch)
                    i += 1
        else:
            while i < n and not dsn[i].isspace():
                if dsn[i] == "\\" and i + 1 < n:
                    value.append(dsn[i + 1])
                    i += 2
                else:
                    value.append(dsn[i])
                    i += 1
        pairs.append((key, "".join(value)))


def _quote(value: str) -> str:
    """A libpq keyword value, quoted when it needs to be."""
    if value and not any(c.isspace() or c in "'\\" for c in value):
        return value
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
