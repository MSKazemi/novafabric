"""CLI commands: ``nova login`` and ``nova logout``.

ADR-0018: Device Authorization Grant (RFC 8628) for authenticating against a
running NovaFabric server.

Credentials are stored in ``~/.config/novafabric/credentials.json`` (mode 0600).
The file is a JSON object keyed by server URL:

    {
        "http://localhost:7433": {
            "access_token": "...",
            "refresh_token": "...",
            "expiry": "2026-06-01T12:00:00+00:00",
            "server_url": "http://localhost:7433"
        }
    }

Local-mode CLI commands (``nova capture``, ``nova validate``, …) never require
credentials — auth is only for server-mode operations.
"""

from __future__ import annotations

import json
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

import typer

_CRED_DIR = Path.home() / ".config" / "novafabric"
_CRED_FILE = _CRED_DIR / "credentials.json"


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------


def _load_credentials() -> dict[str, object]:
    if not _CRED_FILE.exists():
        return {}
    try:
        return json.loads(_CRED_FILE.read_text())  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError):
        return {}


def _save_credentials(creds: dict[str, object]) -> None:
    _CRED_DIR.mkdir(parents=True, exist_ok=True)
    _CRED_FILE.write_text(json.dumps(creds, indent=2))
    # Restrict to owner read/write only
    _CRED_FILE.chmod(0o600)


def get_token(server_url: str) -> str | None:
    """Return a valid access token for *server_url*, or None if not logged in.

    Auto-refreshes if the token has expired and a refresh token is available.
    """
    creds = _load_credentials()
    entry = creds.get(server_url)
    if not entry or not isinstance(entry, dict):
        return None

    raw_at = entry.get("access_token")
    access_token: str | None = str(raw_at) if isinstance(raw_at, str) else None
    raw_exp = entry.get("expiry")
    expiry_str: str | None = str(raw_exp) if isinstance(raw_exp, str) else None

    if not access_token:
        return None

    # Check expiry
    if expiry_str:
        try:
            expiry = datetime.fromisoformat(expiry_str)
            if expiry <= datetime.now(timezone.utc):
                # Token expired — try refresh
                refreshed = _try_refresh(server_url, entry)
                if refreshed:
                    return refreshed
                return None
        except ValueError:
            pass

    return access_token


def _try_refresh(server_url: str, entry: dict[str, object]) -> str | None:
    """Attempt to refresh using refresh_token. Returns new access_token or None."""
    import httpx

    refresh_token = entry.get("refresh_token")
    if not refresh_token:
        return None

    try:
        resp = httpx.post(
            f"{server_url.rstrip('/')}/v0/auth/token",
            json={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=15,
        )
        data = resp.json()
    except Exception:  # noqa: BLE001
        return None

    if "access_token" in data:
        creds = _load_credentials()
        creds[server_url] = _build_entry(server_url, data)
        _save_credentials(creds)
        return str(data["access_token"])
    return None


def _build_entry(server_url: str, token_response: dict[str, object]) -> dict[str, object]:
    raw_ei = token_response.get("expires_in", 3600)
    expires_in = int(raw_ei) if isinstance(raw_ei, (int, float, str)) else 3600
    expiry = datetime.fromtimestamp(
        time.time() + expires_in, tz=timezone.utc
    ).isoformat()
    return {
        "access_token": token_response.get("access_token"),
        "refresh_token": token_response.get("refresh_token"),
        "expiry": expiry,
        "server_url": server_url,
    }


# ---------------------------------------------------------------------------
# ``nova login``
# ---------------------------------------------------------------------------


def login_cmd(
    server: Annotated[
        str,
        typer.Option(
            "--server",
            help="URL of the NovaFabric server (e.g. http://localhost:7433).",
        ),
    ] = "http://localhost:7433",
) -> None:
    """Authenticate with a NovaFabric server via Device Authorization Grant.

    Opens a browser to complete the login flow. Stores an offline token
    in the local credential store for subsequent CLI requests.

    Scope: single server.

    \b
    Examples:
      nova login --server https://nova.example.com
    """
    import httpx

    server_url = server.rstrip("/")

    typer.echo(f"Logging in to {server_url} …")

    # Step 1: request device code
    try:
        resp = httpx.post(
            f"{server_url}/v0/auth/device/code",
            json={"server_url": server_url},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Failed to contact server: {exc}", err=True)
        raise typer.Exit(code=1)

    device_code: str = data["device_code"]
    user_code: str = data["user_code"]
    verification_uri: str = data["verification_uri"]
    interval: int = int(data.get("interval", 5))
    expires_in: int = int(data.get("expires_in", 300))

    typer.echo("")
    typer.echo("  To complete login, visit:")
    typer.echo(f"    {verification_uri}")
    typer.echo(f"  Enter code: {user_code}")
    typer.echo("")
    typer.echo("Waiting for approval …")

    # Step 2: poll until approved or expired
    deadline = time.monotonic() + expires_in
    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            poll_resp = httpx.post(
                f"{server_url}/v0/auth/token",
                json={"device_code": device_code},
                timeout=15,
            )
            poll_data = poll_resp.json()
        except Exception as exc:  # noqa: BLE001
            typer.echo(f"Poll error: {exc}", err=True)
            raise typer.Exit(code=1)

        error = poll_data.get("error")
        if error == "authorization_pending":
            continue
        if error == "expired_token":
            typer.echo("Device code expired. Run `nova login` again.", err=True)
            raise typer.Exit(code=1)
        if "access_token" in poll_data:
            break
        typer.echo(f"Unexpected response: {poll_data}", err=True)
        raise typer.Exit(code=1)
    else:
        typer.echo("Login timed out. Run `nova login` again.", err=True)
        raise typer.Exit(code=1)

    # Step 3: store credentials
    creds = _load_credentials()
    creds[server_url] = _build_entry(server_url, poll_data)
    _save_credentials(creds)

    # Verify mode bits
    mode = stat.S_IMODE(_CRED_FILE.stat().st_mode)
    if mode != 0o600:
        _CRED_FILE.chmod(0o600)

    typer.echo(f"Logged in to {server_url} as {poll_data.get('subject', 'unknown')}.")
    typer.echo(f"Credentials stored in {_CRED_FILE}")


# ---------------------------------------------------------------------------
# ``nova logout``
# ---------------------------------------------------------------------------


def logout_cmd(
    server: Annotated[
        Optional[str],  # noqa: UP007
        typer.Option(
            "--server",
            help="Server URL to log out of. Omit to log out of all servers.",
        ),
    ] = None,
) -> None:
    """Remove stored credentials for a NovaFabric server.

    Scope: single server.

    \b
    Examples:
      nova logout --server https://nova.example.com
    """
    creds = _load_credentials()
    if not creds:
        typer.echo("No credentials stored.")
        return

    if server:
        server_url = server.rstrip("/")
        if server_url in creds:
            del creds[server_url]
            _save_credentials(creds)
            typer.echo(f"Logged out of {server_url}.")
        else:
            typer.echo(f"No credentials found for {server_url}.")
    else:
        for url in list(creds.keys()):
            del creds[url]
        _save_credentials(creds)
        typer.echo("Logged out of all servers.")
