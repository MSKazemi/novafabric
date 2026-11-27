"""Local-identity scrub for support-bundle members (ADR-0187 D2, bundle-only).

The redaction ruleset (:mod:`novafabric.support_bundle._redact`) removes
*credentials*. It does not touch who and where the bundle was made on: the home
directory in a database path (``/home/<user>/.novafabric/registry.db``), the user
name in a scratch path, the machine's host name in a log line. None of those is
a secret, so no secret rule fires, yet each identifies a person or a machine to
whoever the bundle is sent to. This module closes that gap for the support
bundle only.

It is deliberately **not** part of the shared ruleset: the audit-log enrichment
and the SIEM export also use ``_redact`` and record its version, and rewriting
paths in an audit record is a different decision from rewriting them in a
diagnostics archive a human is about to email.

What it rewrites (version :data:`LOCAL_SCRUB_VERSION`):

- this process's home directory (as given and as resolved) -> ``~``;
- the name after any home prefix -> ``[USER]``: ``/home/<name>``, ``/Users/<name>``,
  ``C:\\Users\\<name>`` (also JSON-escaped, with doubled backslashes) and the
  URL-encoded ``%2Fhome%2F<name>`` / ``%2FUsers%2F<name>``;
- the current user name where it is the path component right after a per-user
  root (``/scratch/``, ``/work/``, ``/tmp/``, ``/var/tmp/``; ``/tmp/<user>.d`` too),
  or the value of a ``user=`` / ``user:`` / ``for user "…"`` field -> ``[USER]``;
- this machine's host name (full and short form) as a whole token, matched
  case-sensitively -> ``[HOSTNAME]``.

It is deliberately narrow, because every false match rewrites diagnostic text:
with a host called ``linux`` and a user called ``lib``, a looser rule turned
``Linux-6.8`` into ``[HOSTNAME]-6.8`` and ``/var/lib/nova_x`` into
``/var/[USER]/[HOSTNAME]_x``. So a host name next to a letter, digit, ``_`` or
``-`` is left alone (``serve-<host>.log`` keeps the host; ``<host>.log`` does not),
and the bundle builder does not scrub ``versions.json`` at all: platform and
version strings hold no path, and a host such as ``ubuntu`` would otherwise
rewrite them.

What it does not rewrite, and the bundle's ``MANIFEST.txt`` says so: remote host
names and IP addresses (a database host in a DSN is often the diagnosis), and a
user name in free text outside the places above. Lookups are local only: no DNS
query is made (``socket.getfqdn`` is not called), so a bundle can be built offline.
"""

from __future__ import annotations

import getpass
import os
import platform
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "HOME_PLACEHOLDER",
    "HOST_PLACEHOLDER",
    "LOCAL_SCRUB_VERSION",
    "USER_PLACEHOLDER",
    "LocalIdentity",
    "scrub_text",
    "scrub_value",
]

#: Version of the local-identity scrub recorded in ``manifest.json``.
LOCAL_SCRUB_VERSION = "v1"

HOME_PLACEHOLDER = "~"
USER_PLACEHOLDER = "[USER]"
HOST_PLACEHOLDER = "[HOSTNAME]"

#: Host names that identify no particular machine; replacing them would only
#: make a log line harder to read.
_GENERIC_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "localdomain"})

#: Shorter names than this are not scrubbed as host or user names: a one- or
#: two-letter value would match far too much ordinary text.
_MIN_NAME_LEN = 3

#: Characters that end a path in free text (after the home directory).
_END = r"""(?=$|[/\\\s"'`,;:()\[\]{}<>=])"""

#: Characters that end a user name in a path; ``.`` too (``/tmp/<user>.d``).
_USER_END = r"""(?=$|[/\\\s"'`,;:()\[\]{}<>=.])"""

#: Characters a path component may hold (for the name after a home prefix).
_NAME = r"""[^/\\\s"'`,;:()\[\]{}<>=%&?#]+"""

#: Another user's home-directory prefix (POSIX and macOS). Not anchored to the
#: start of a path: ``/data/home/<name>`` is a home too, and a URL path that
#: happens to contain ``/home/`` losing one segment costs nothing.
_OTHER_HOME_RE = re.compile(r"(/home/|/Users/)" + _NAME)

#: ``C:\Users\<name>``, also JSON-escaped (``C:\\Users\\<name>``): the same
#: separator, one or two backslashes, on both sides of ``Users``.
_WINDOWS_HOME_RE = re.compile(r"(?i)(?<![\w])([A-Z]:(\\{1,2})Users\2)" + _NAME)

#: URL-encoded home prefix: ``%2Fhome%2F<name>`` / ``%2FUsers%2F<name>``.
_URL_HOME_RE = re.compile(r"(?i)(%2F(?:home|Users)%2F)" + _NAME)

#: Per-user roots whose next path component is, by convention, a user name.
_USER_ROOTS = r"(?:/scratch/|/work/|/var/tmp/|/tmp/)"

#: Token characters a host name must not touch on either side.
_HOST_TOKEN = r"[A-Za-z0-9_-]"


@dataclass(frozen=True)
class LocalIdentity:
    """The values that identify this user and machine, resolved once per bundle."""

    homes: tuple[str, ...] = ()
    user: str | None = None
    hostnames: tuple[str, ...] = ()

    @classmethod
    def current(cls) -> LocalIdentity:
        """Resolve from the running process. Never raises: a lookup that fails
        simply contributes nothing, so a broken environment still gets a bundle."""
        return cls(homes=_homes(), user=_user(), hostnames=_hostnames())


def _homes() -> tuple[str, ...]:
    found: set[str] = set()
    try:
        home = Path.home()
    except (RuntimeError, KeyError, OSError):
        return ()
    for candidate in (str(home), os.path.realpath(home)):
        candidate = candidate.rstrip("/\\")
        # "/" or "" as a home would rewrite every absolute path in the bundle.
        if len(candidate) > 1:
            found.add(candidate)
    return tuple(sorted(found, key=len, reverse=True))


def _user() -> str | None:
    try:
        name = getpass.getuser()
    except Exception:  # noqa: BLE001 — getpass raises OSError/KeyError/ImportError by platform
        return None
    return name if name and len(name) >= _MIN_NAME_LEN else None


def _hostnames() -> tuple[str, ...]:
    found: set[str] = set()
    for lookup in (socket.gethostname, platform.node):
        try:
            name = lookup()
        except Exception:  # noqa: BLE001 — best-effort, a local lookup only
            continue
        if not name:
            continue
        for candidate in (name, name.split(".", 1)[0]):
            if len(candidate) >= _MIN_NAME_LEN and candidate.lower() not in _GENERIC_HOSTNAMES:
                found.add(candidate)
    return tuple(sorted(found, key=len, reverse=True))


def scrub_text(text: str, identity: LocalIdentity) -> str:
    """Rewrite home paths, the user name where a path or field names it, and this
    host's name as a whole token, in *text*."""
    for home in identity.homes:
        text = re.sub(re.escape(home) + _END, HOME_PLACEHOLDER, text)
    text = _OTHER_HOME_RE.sub(lambda m: m.group(1) + USER_PLACEHOLDER, text)
    text = _WINDOWS_HOME_RE.sub(lambda m: m.group(1) + USER_PLACEHOLDER, text)
    text = _URL_HOME_RE.sub(lambda m: m.group(1) + USER_PLACEHOLDER, text)
    if identity.user:
        user = re.escape(identity.user)
        text = re.sub(
            "(" + _USER_ROOTS + ")" + user + _USER_END,
            lambda m: m.group(1) + USER_PLACEHOLDER,
            text,
        )
        text = re.sub(
            r"(?<![A-Za-z0-9_])((?:user(?:name)?\s*[=:]\s*|for user\s+)[\"']?)"
            + user
            + r"(?![A-Za-z0-9_])",
            lambda m: m.group(1) + USER_PLACEHOLDER,
            text,
        )
    for host in identity.hostnames:
        text = re.sub(
            "(?<!" + _HOST_TOKEN + ")" + re.escape(host) + "(?!" + _HOST_TOKEN + ")",
            HOST_PLACEHOLDER,
            text,
        )
    return text


def scrub_value(value: Any, identity: LocalIdentity) -> Any:
    """:func:`scrub_text` applied to every string inside *value* (keys kept)."""
    if isinstance(value, dict):
        return {key: scrub_value(val, identity) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_value(item, identity) for item in value]
    if isinstance(value, str):
        return scrub_text(value, identity)
    return value
