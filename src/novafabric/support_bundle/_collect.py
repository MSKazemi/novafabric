"""Collectors for the support bundle members (ADR-0187 D1).

Every collector is best-effort: a failing check is recorded as an error
string inside the member, never raised, so a broken install can still
produce a bundle (that is the whole point of the bundle).

All collected payloads pass through the redaction pipeline
(:mod:`novafabric.support_bundle._redact`) before serialization.
"""

from __future__ import annotations

import dataclasses
import os
import platform
import re
import time
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

import yaml

from novafabric.support_bundle._redact import redact_line, redact_value

#: Default server config location (ADR-0029; mirrors
#: ``novafabric.server.config._DEFAULT_CONFIG_PATH`` without importing the
#: server stack, which requires the ``server`` extra).
DEFAULT_SERVER_CONFIG_PATH = Path.home() / ".config" / "novafabric" / "server.yaml"

#: Env-var name prefixes included (names only, never values) in ``env.txt``.
_ENV_NAME_PREFIXES = ("NOVAFABRIC_", "NOVA_")

_EXTRA_MARKER_RE = re.compile(r"""extra\s*==\s*['"]([^'"]+)['"]""")
_DIST_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")

#: Total byte budget for log content in the bundle (ADR-0187 bounded-log slice).
MAX_LOG_BYTES = 5 * 1024 * 1024

#: Bundle member recorded when no log files are found (honest empty state).
LOGS_README_MEMBER = "logs/README.txt"


def collect_doctor() -> dict[str, Any]:
    """Programmatic ``nova doctor --check-storage``: backend, schema version,
    migration state, row counts."""
    try:
        from novafabric.storage import get_backend

        info = get_backend()
        storage: dict[str, Any] = {"ok": True}
        storage.update(dataclasses.asdict(info.info()))
    except Exception as exc:  # noqa: BLE001 — best-effort diagnostics
        storage = {"ok": False, "error": str(exc)}
    result: dict[str, Any] = redact_value({"storage": storage})
    return result


def collect_versions() -> dict[str, Any]:
    """App/Python/platform versions plus installed optional extras."""
    try:
        nova_version = importlib_metadata.version("novafabric")
    except importlib_metadata.PackageNotFoundError:
        nova_version = "unknown"
    return {
        "novafabric": nova_version,
        "python": platform.python_version(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "installed_extras": _installed_extras(),
    }


def _installed_extras() -> list[str]:
    """Extras of the ``novafabric`` distribution whose requirements are all
    importable-installed, derived from ``importlib.metadata`` only."""
    try:
        requires = importlib_metadata.requires("novafabric") or []
    except importlib_metadata.PackageNotFoundError:
        return []
    extra_deps: dict[str, list[str]] = {}
    for req in requires:
        if ";" not in req:
            continue
        spec, marker = req.split(";", 1)
        extra_match = _EXTRA_MARKER_RE.search(marker)
        if not extra_match:
            continue
        name_match = _DIST_NAME_RE.match(spec.strip())
        if not name_match:
            continue
        extra_deps.setdefault(extra_match.group(1), []).append(name_match.group(0))
    installed: list[str] = []
    for extra, dists in sorted(extra_deps.items()):
        if all(_dist_installed(dist) for dist in dists):
            installed.append(extra)
    return installed


def _dist_installed(name: str) -> bool:
    try:
        importlib_metadata.version(name)
    except importlib_metadata.PackageNotFoundError:
        return False
    return True


def collect_env_names() -> str:
    """``env.txt`` body: NOVAFABRIC_*/NOVA_* environment-variable *names*
    only, one per line. Values are never read into the bundle (ADR-0187 D2)."""
    names = sorted(name for name in os.environ if name.startswith(_ENV_NAME_PREFIXES))
    return "\n".join(names) + ("\n" if names else "")


def collect_health() -> dict[str, Any]:
    """Best-effort local install health: data-dir and registry presence,
    capsule count. No capsule payloads (ADR-0187 D2)."""
    health: dict[str, Any] = {"ok": True}
    try:
        from novafabric._paths import default_capsule_dir, nova_home, registry_db_path

        home = nova_home()
        db = registry_db_path()
        capsules = default_capsule_dir()
        health["nova_home_exists"] = home.exists()
        health["registry_db_exists"] = db.exists()
        health["registry_db_size_bytes"] = db.stat().st_size if db.exists() else 0
        health["capsule_dir_exists"] = capsules.exists()
        health["capsule_count"] = (
            sum(1 for entry in capsules.iterdir() if entry.is_dir()) if capsules.exists() else 0
        )
    except Exception as exc:  # noqa: BLE001 — best-effort diagnostics
        health = {"ok": False, "error": str(exc)}
    result: dict[str, Any] = redact_value(health)
    return result


def collect_recent_logs(
    window_hours: int,
    max_bytes: int = MAX_LOG_BYTES,
    log_dir: Path | None = None,
) -> dict[str, str]:
    """Bounded, redacted tails of recent ``*.log`` files (ADR-0187 follow-on).

    NovaFabric writes no log files by default, so collection is opt-in:
    the collector looks in *log_dir* if given, else ``$NOVAFABRIC_LOG_DIR``
    (read here only — no logging handlers are installed anywhere), else the
    ``$NOVAFABRIC_HOME/logs`` convention. Only ``*.log`` files modified
    within the last *window_hours* are considered (newest first); content
    is tail-truncated so the total raw bytes read stay within *max_bytes*;
    every line passes :func:`novafabric.support_bundle._redact.redact_line`.

    Returns a mapping of bundle member name (``logs/<file>.log``) to text.
    When no eligible log files exist, the single member
    ``logs/README.txt`` records "no log files found" (honest empty state).
    """
    if log_dir is None:
        env_dir = os.environ.get("NOVAFABRIC_LOG_DIR")
        if env_dir:
            log_dir = Path(env_dir)
        else:
            from novafabric._paths import nova_home

            log_dir = nova_home() / "logs"

    cutoff = time.time() - window_hours * 3600
    candidates: list[tuple[float, Path]] = []
    try:
        if log_dir.is_dir():
            for entry in log_dir.glob("*.log"):
                try:
                    stat = entry.stat()
                    if entry.is_file() and stat.st_mtime >= cutoff:
                        candidates.append((stat.st_mtime, entry))
                except OSError:
                    continue
    except OSError:
        candidates = []

    members: dict[str, str] = {}
    budget = max_bytes
    for _mtime, entry in sorted(candidates, key=lambda item: item[0], reverse=True):
        if budget <= 0:
            break
        try:
            body, used = _redacted_tail(entry, budget)
        except OSError:
            continue
        members[f"logs/{entry.name}"] = body
        budget -= used
    if not members:
        return {LOGS_README_MEMBER: _no_logs_readme(log_dir, window_hours)}
    return members


def _no_logs_readme(log_dir: Path, window_hours: int) -> str:
    return (
        "no log files found\n"
        "\n"
        f"Searched: {log_dir}/*.log, modified within the last "
        f"{window_hours} hour(s).\n"
        "NovaFabric writes no log files by default; set "
        "NOVAFABRIC_LOG_DIR to point the support-bundle collector "
        "at a directory of *.log files.\n"
    )


def _redacted_tail(path: Path, budget: int) -> tuple[str, int]:
    """Last ``budget`` raw bytes of *path*, line-redacted, with a truncation
    marker when the file did not fit. Returns ``(text, raw_bytes_read)``."""
    size = path.stat().st_size
    truncated = size > budget
    with path.open("rb") as handle:
        if truncated:
            handle.seek(size - budget)
        data = handle.read(budget)
    text = data.decode("utf-8", errors="replace")
    if truncated and "\n" in text:
        # Drop the (possibly partial) first line after seeking mid-file.
        text = text.split("\n", 1)[1]
    lines = [redact_line(line) for line in text.splitlines()]
    body = "\n".join(lines) + ("\n" if lines else "")
    if truncated:
        body = f"[truncated: showing the last {len(data)} of {size} bytes]\n" + body
    return body, len(data)


def collect_redacted_config(config_path: Path | None = None) -> str | None:
    """Redacted server config YAML, or ``None`` if no config exists.

    Every value whose key matches the deny patterns is replaced by
    ``[REDACTED]``. An unparseable config is omitted entirely rather than
    included raw (deny-by-default, ADR-0187 D3).
    """
    path = config_path or DEFAULT_SERVER_CONFIG_PATH
    if not path.exists():
        return None
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        return None
    if not isinstance(raw, dict):
        return None
    return yaml.safe_dump(redact_value(raw), sort_keys=True)
