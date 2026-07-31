"""One-shot token generation + verification for `nova serve`."""

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

    Returns the path. Caller is responsible for cleanup on shutdown.
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
