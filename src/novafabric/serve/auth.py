"""Session-token generation + verification for `nova serve`.

The token is *persistent*, not one-shot: an existing ``.serve-token`` is
reused so a restart does not invalidate open browser sessions, and it
survives on disk after the process exits.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
from pathlib import Path
from typing import Final

from novafabric._paths import serve_token_path

TOKEN_ENV: Final[str] = "NOVAFABRIC_SERVE_TOKEN"

#: Domain tag for :func:`token_fingerprint`. It keeps the fingerprint from being
#: equal to (a prefix of) any plain SHA-256 of the same token that another surface
#: publishes, such as the ``token_digest`` an issued-token record stores.
_FINGERPRINT_DOMAIN: Final[bytes] = b"novafabric/serve/token-fingerprint/v1\x00"

#: Hex characters kept from the digest: 48 bits, enough to tell tokens apart in
#: an audit log and far too few to help reconstruct one.
FINGERPRINT_HEX_LEN: Final[int] = 12


def token_fingerprint(token: str) -> str:
    """Return a stable, non-reversible identifier for *token*.

    This is what the dashboard audit log, the SIEM export (``user.uid``) and
    ``GET /api/admin/tokens`` show for the server token. It is a truncated,
    domain-separated SHA-256 digest, never a slice of the token: a prefix is part
    of the secret, and for a short pinned ``NOVAFABRIC_SERVE_TOKEN`` it can be most
    of it. The same token always gives the same fingerprint.

    A digest does not protect a *guessable* token: anyone who can read the
    fingerprint can test candidate tokens against it offline. Pin only
    high-entropy tokens (the generated default is 256 bits).
    """
    digest = hashlib.sha256(_FINGERPRINT_DOMAIN + token.encode("utf-8")).hexdigest()
    return digest[:FINGERPRINT_HEX_LEN]


def token_matches(candidate: str, expected: str) -> bool:
    """Constant-time token comparison.

    Hash both sides first so the comparison length is fixed — a plain
    length check before compare_digest leaks the token length.
    """
    return hmac.compare_digest(
        hashlib.sha256(candidate.encode()).digest(),
        hashlib.sha256(expected.encode()).digest(),
    )


def extract_bearer(authorization: str | None) -> str | None:
    """Return the token from an ``Authorization: Bearer <token>`` header, else None."""
    if not authorization:
        return None
    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    credentials = credentials.strip()
    return credentials or None


def generate_token() -> str:
    """Return a token for nova serve.

    Priority (highest to lowest):
    1. NOVAFABRIC_SERVE_TOKEN env var — allows pinning a stable token in Docker / CI.
    2. Existing .serve-token file — survive process restarts without breaking browser sessions.
    3. Fresh cryptographically-random token.
    """
    env_token = os.environ.get(TOKEN_ENV, "").strip()
    if env_token:
        return env_token
    existing = serve_token_path()
    if existing.exists():
        try:
            stored = existing.read_text().strip()
            if stored:
                return stored
        except OSError:
            pass
    return secrets.token_urlsafe(32)


def write_token_file(token: str) -> Path:
    """Write the token to $NOVAFABRIC_HOME/.serve-token with mode 0600.

    Returns the path. **The file deliberately outlives the process**: it is
    priority 2 in :func:`generate_token`, so a restarted ``nova serve`` reuses
    the same token instead of invalidating every open browser session. Removing
    it on shutdown would make that branch unreachable.

    The consequence is worth stating rather than leaving to be discovered: a
    usable bearer token stays on disk at 0600 after the server exits, until
    something removes it. :func:`clear_token_file` is that something, and it has
    no caller — nothing in the shipped product deletes this file.
    """
    path = serve_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with 0600 atomically — a write-then-chmod sequence leaves a window
    # where the token is readable at the umask default.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


def clear_token_file() -> None:
    """Delete the persisted serve token, if present. Never raises.

    Has no production caller by design — see :func:`write_token_file` for why
    the file persists. Kept because the file is a live credential and something
    has to be able to remove it: this is what a "log out everywhere" action or a
    teardown script calls.
    """
    path = serve_token_path()
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass


def is_localhost_host(host_header: str | None) -> bool:
    """Reject Host headers that aren't localhost — DNS-rebinding defence."""
    if not host_header:
        return False
    # IPv6 Host headers use bracket notation: [::1] or [::1]:port.
    # Splitting on ":" would break "[::1]" into "[" + ":1]".
    if host_header.startswith("["):
        name = host_header.split("]", 1)[0] + "]"  # e.g. "[::1]"
    else:
        name = host_header.split(":", 1)[0]
    return name.lower() in {"127.0.0.1", "localhost", "[::1]"}
