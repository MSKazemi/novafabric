"""Local bearer-token auth for the server when OIDC is disabled (ADR-0184).

Secure-by-default local mode: instead of granting anonymous admin, the server
requires an auto-generated bearer token. Token resolution has parity with
``novafabric.serve.auth`` (the dashboard's one-shot token):

1. ``NOVAFABRIC_SERVER_TOKEN`` env var — pin a stable token in Docker / CI.
2. Existing ``$NOVAFABRIC_HOME/.server-token`` file — survive restarts.
3. Fresh cryptographically-random token (persisted mode ``0600``).
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path
from typing import Final

from novafabric._paths import server_token_path

TOKEN_ENV: Final[str] = "NOVAFABRIC_SERVER_TOKEN"

#: Subject the local token authenticates as (always role ``admin``).
LOCAL_ADMIN_SUBJECT: Final[str] = "local-admin"


def generate_token() -> str:
    """Return the local server auth token.

    Priority (highest to lowest):
    1. NOVAFABRIC_SERVER_TOKEN env var — allows pinning a stable token.
    2. Existing .server-token file — survives process restarts.
    3. Fresh cryptographically-random token.
    """
    env_token = os.environ.get(TOKEN_ENV, "").strip()
    if env_token:
        return env_token
    existing = server_token_path()
    if existing.exists():
        try:
            stored = existing.read_text().strip()
            if stored:
                return stored
        except OSError:
            pass
    return secrets.token_urlsafe(32)


def write_token_file(token: str) -> Path:
    """Write the token to ``$NOVAFABRIC_HOME/.server-token`` with mode 0600.

    Returns the path.
    """
    path = server_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


def ensure_local_token() -> tuple[str, Path]:
    """Resolve the local token and make sure the token file exists (mode 0600).

    Returns ``(token, token_file_path)``. The file is created if absent so an
    operator on the same machine can always recover the token without
    restarting the server.
    """
    token = generate_token()
    path = server_token_path()
    if not path.exists():
        path = write_token_file(token)
    return token, path
