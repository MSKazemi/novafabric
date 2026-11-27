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
- any other ``/home/<name>``, ``/Users/<name>`` or ``C:\\Users\\<name>`` prefix ->
  the same prefix with ``[USER]``;
- the current user name where it is a whole path segment -> ``[USER]``;
- this machine's host name (full and short form) where it is a whole name ->
  ``[HOSTNAME]``.

What it does not rewrite, and the bundle's ``MANIFEST.txt`` says so: remote host
names and IP addresses (a database host in a DSN is often the diagnosis), and a
user name in free text outside a path. Lookups are local only: no DNS query is
made (``socket.getfqdn`` is not called), so a bundle can be built offline.
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

#: Characters that end a path or a name in free text.
_END = r"""(?=$|[/\\\s"'`,;:()\[\]{}<>=])"""

#: Another user's home-directory prefix (POSIX and macOS, then Windows). Not
#: anchored to the start of a path: ``/data/home/<name>`` is a home too, and a
#: URL path that happens to contain ``/home/`` losing one segment costs nothing.
_OTHER_HOME_RE = re.compile(r"""(/home/|/Users/)[^/\\\s"'`,;:()\[\]{}<>=]+""")
_WINDOWS_HOME_RE = re.compile(r"""(?i)(?<![\w])([A-Z]:\\Users\\)[^\\/\s"'`,;:()\[\]{}<>=]+""")


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
    """Rewrite home paths, user names in paths and this host's name in *text*."""
    for home in identity.homes:
        text = re.sub(re.escape(home) + _END, HOME_PLACEHOLDER, text)
    text = _OTHER_HOME_RE.sub(lambda m: m.group(1) + USER_PLACEHOLDER, text)
    text = _WINDOWS_HOME_RE.sub(lambda m: m.group(1) + USER_PLACEHOLDER, text)
    if identity.user:
        text = re.sub(r"(?<=[/\\])" + re.escape(identity.user) + _END, USER_PLACEHOLDER, text)
    # A hyphen is not a boundary-breaker: ``serve-<host>.log`` is a common log
    # name, and leaving the host in it would defeat the scrub.
    for host in identity.hostnames:
        text = re.sub(
            r"(?i)(?<![A-Za-z0-9])" + re.escape(host) + r"(?![A-Za-z0-9])",
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
