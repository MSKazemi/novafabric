#!/usr/bin/env python3
"""Generate the secrets the bundled Docker Compose stack requires.

``deploy/docker/docker-compose.yml`` carries no password defaults: every
credential is a required variable (``${VAR:?...}``), so ``docker compose``
refuses to start until each one is set. This script fills them in.

What it does:

* writes ``deploy/docker/.env`` (the file Compose reads automatically for
  variable interpolation; git-ignored and excluded from the Docker build
  context) with a strong random value for every required secret;
* creates the file with mode ``0600`` and tightens an existing file to
  ``0600``;
* **never overwrites a value that is already set** — re-running it is safe,
  and it never rotates a password silently (rotating a Postgres password needs
  an ``ALTER ROLE`` inside the database as well, see docs/installation.md);
* **never prints a secret** — only variable names and whether each was
  generated or kept.

``--check`` writes nothing and exits 1 when a secret is missing, still holds
one of the old demonstration values, or the file is readable by anyone but its
owner.

Standard library only. Usage::

    python3 scripts/compose_bootstrap.py            # create / complete the file
    python3 scripts/compose_bootstrap.py --check    # verify, change nothing
    python3 scripts/compose_bootstrap.py --env-file /path/to/.env
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import stat
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = REPO_ROOT / "deploy" / "docker" / ".env"

# Every secret the compose file requires. Keep in sync with
# deploy/docker/docker-compose.yml; tests/docs/test_compose_no_literal_secrets.py
# fails if the two drift apart.
REQUIRED_SECRETS: tuple[str, ...] = (
    "NOVA_POSTGRES_PASSWORD",
    "NOVA_CLICKHOUSE_PASSWORD",
    "NOVA_AGE_PASSWORD",
)

# 32 random bytes -> 64 hex characters (256 bits). Hex is safe unquoted in a
# .env file, inside a URL's userinfo, in a libpq DSN and on a command line.
SECRET_BYTES = 32

# The demonstration values earlier versions of the compose file hardcoded. A
# volume initialised by such a version still uses one, so the script keeps it
# (changing the variable alone would lock the stack out of its own database)
# but warns, and ``--check`` fails until it is rotated.
KNOWN_DEMO_VALUES = frozenset({"nova", "novapass"})

_ASSIGNMENT_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


class BootstrapError(Exception):
    """The env file cannot be written or verified safely."""


def generate_secret() -> str:
    return secrets.token_hex(SECRET_BYTES)


def parse_env(text: str) -> dict[str, str]:
    """Return KEY -> raw value for each ``KEY=value`` line (comments skipped)."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = _ASSIGNMENT_RE.match(line)
        if match:
            values[match.group(1)] = match.group(2).strip().strip("'\"")
    return values


def _read_existing(path: Path) -> str:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return ""
    if stat.S_ISLNK(info.st_mode):
        raise BootstrapError(f"{path} is a symlink; refusing to write through it")
    if not stat.S_ISREG(info.st_mode):
        raise BootstrapError(f"{path} is not a regular file")
    return path.read_text(encoding="utf-8")


def _mode_is_private(path: Path) -> bool:
    return stat.S_IMODE(os.lstat(path).st_mode) & 0o077 == 0


def missing_secrets(values: dict[str, str]) -> list[str]:
    return [name for name in REQUIRED_SECRETS if not values.get(name)]


def demo_secrets(values: dict[str, str]) -> list[str]:
    return [name for name in REQUIRED_SECRETS if values.get(name) in KNOWN_DEMO_VALUES]


def _write_atomically(path: Path, content: str) -> None:
    """Write *content* to *path* via a 0600 temp file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)  # mkstemp already uses 0600; make it explicit
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    os.chmod(path, 0o600)


def bootstrap(path: Path) -> tuple[list[str], list[str]]:
    """Fill in missing secrets. Returns (generated names, kept names)."""
    existing = _read_existing(path)
    values = parse_env(existing)
    to_generate = missing_secrets(values)
    kept = [name for name in REQUIRED_SECRETS if name not in to_generate]

    if to_generate:
        # Drop empty placeholders (``NAME=``) for the keys being generated, so
        # the file never holds two assignments for one name.
        lines = [
            line
            for line in existing.splitlines()
            if not (
                (m := _ASSIGNMENT_RE.match(line))
                and not line.lstrip().startswith("#")
                and m.group(1) in to_generate
            )
        ]
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(
            "# Generated by scripts/compose_bootstrap.py. Keep this file private "
            "(mode 0600); never commit it."
        )
        lines.extend(f"{name}={generate_secret()}" for name in to_generate)
        _write_atomically(path, "\n".join(lines) + "\n")
    elif not _mode_is_private(path):
        os.chmod(path, 0o600)
    return to_generate, kept


def check(path: Path) -> list[str]:
    """Return a list of problems (names and modes only, never values)."""
    try:
        existing = _read_existing(path)
    except BootstrapError as exc:
        return [str(exc)]
    if not existing and not path.exists():
        return [f"{path} does not exist; run scripts/compose_bootstrap.py"]
    values = parse_env(existing)
    problems = [f"{name} is not set" for name in missing_secrets(values)]
    problems += [
        f"{name} still holds a known demonstration value; rotate it"
        for name in demo_secrets(values)
    ]
    if not _mode_is_private(path):
        mode = stat.S_IMODE(os.lstat(path).st_mode)
        problems.append(f"{path} has mode {mode:04o}; expected 0600")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate the Docker Compose stack's secrets into a private .env file."
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help=f"env file to create or complete (default: {DEFAULT_ENV_FILE})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the file has every secret and mode 0600; write nothing",
    )
    args = parser.parse_args(argv)
    path: Path = args.env_file

    if args.check:
        problems = check(path)
        for problem in problems:
            print(f"compose-bootstrap: {problem}", file=sys.stderr)
        if not problems:
            print(f"compose-bootstrap: {path} is complete (mode 0600)")
        return 1 if problems else 0

    try:
        generated, kept = bootstrap(path)
    except (BootstrapError, OSError) as exc:
        print(f"compose-bootstrap: {exc}", file=sys.stderr)
        return 1
    for name in generated:
        print(f"compose-bootstrap: generated {name}")
    for name in kept:
        print(f"compose-bootstrap: kept existing {name}")
    for name in demo_secrets(parse_env(path.read_text(encoding="utf-8"))):
        print(
            f"compose-bootstrap: WARNING {name} still holds a known demonstration "
            "value; rotate it (docs/installation.md)",
            file=sys.stderr,
        )
    print(f"compose-bootstrap: {path} (mode 0600); values are not printed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
