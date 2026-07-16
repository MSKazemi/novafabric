"""Support bundle — redacted, hash-manifested diagnostics tarball (ADR-0187).

**Experimental.** First slice of ADR-0187: :func:`build_support_bundle`
produces a ``.tar.gz`` containing ONLY allowlisted members
(:data:`ALLOWED_MEMBERS`); anything not explicitly allowlisted stays out
(D3, deny-by-default). The normative exclusions of D2 (no tokens/keys/
credentials, no capsule payloads, no prompts/responses, no PII, no
env-var values) are enforced by construction: collectors never read
those surfaces, and every structured payload passes the redaction
pipeline before serialization.

``manifest.json`` records the SHA-256 and size of every member, the
bundle schema version, the redaction ruleset version, and the creation
timestamp (D4) — the bundle is verifiable offline.

Second slice (2026-07-16): bounded log collection — ``logs/*.log``
members are the line-redacted tails of recent log files found via the
opt-in ``NOVAFABRIC_LOG_DIR`` / ``$NOVAFABRIC_HOME/logs`` convention
(:func:`collect_recent_logs`); when none exist, ``logs/README.txt``
records that honestly.
"""

from __future__ import annotations

import hashlib
import json
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from novafabric.support_bundle._collect import (
    LOGS_README_MEMBER,
    collect_doctor,
    collect_env_names,
    collect_health,
    collect_recent_logs,
    collect_redacted_config,
    collect_versions,
)
from novafabric.support_bundle._redact import REDACTION_RULESET_VERSION

__all__ = [
    "ALLOWED_MEMBERS",
    "BUNDLE_SCHEMA_VERSION",
    "REDACTION_RULESET_VERSION",
    "SupportBundleResult",
    "build_support_bundle",
    "collect_recent_logs",
]

#: Bundle schema version recorded in ``manifest.json``.
BUNDLE_SCHEMA_VERSION = "1"

#: The static allowlist (ADR-0187 D3). A file that is not named here is
#: never added to the tarball, even if present in the staging directory.
#: The only dynamic extension is the bounded-log slice: ``logs/<file>.log``
#: member names returned by :func:`collect_recent_logs` in this build (and
#: nothing else) are additionally allowed.
ALLOWED_MEMBERS: tuple[str, ...] = (
    "doctor.json",
    "versions.json",
    "config.redacted.yaml",
    "env.txt",
    "health.json",
    LOGS_README_MEMBER,
    "manifest.json",
)


@dataclass(frozen=True)
class SupportBundleResult:
    """Outcome of :func:`build_support_bundle`."""

    path: Path
    members: list[str]
    manifest_sha256: str


def build_support_bundle(
    output_path: Path,
    *,
    log_window_hours: int = 24,
    config_path: Path | None = None,
    staging_dir: Path | None = None,
) -> SupportBundleResult:
    """Build a redacted diagnostics tarball at *output_path*.

    ``log_window_hours`` bounds the ``logs/`` members (only ``*.log`` files
    modified within the window are collected, tail-truncated to a total
    byte budget and line-redacted) and is recorded in the manifest.
    ``config_path`` overrides the default server-config location.
    ``staging_dir`` is exposed for tests; only allowlisted member names
    are ever taken from it (deny-by-default).
    """
    if staging_dir is not None:
        return _build_in(staging_dir, output_path, log_window_hours, config_path)
    with tempfile.TemporaryDirectory(prefix="nova-support-bundle-") as tmp:
        return _build_in(Path(tmp), output_path, log_window_hours, config_path)


def _build_in(
    staging: Path,
    output_path: Path,
    log_window_hours: int,
    config_path: Path | None,
) -> SupportBundleResult:
    staging.mkdir(parents=True, exist_ok=True)

    _write_json(staging / "doctor.json", collect_doctor())
    _write_json(staging / "versions.json", collect_versions())
    _write_json(staging / "health.json", collect_health())
    (staging / "env.txt").write_text(collect_env_names(), encoding="utf-8")
    redacted_config = collect_redacted_config(config_path)
    if redacted_config is not None:
        (staging / "config.redacted.yaml").write_text(redacted_config, encoding="utf-8")

    # Bounded, redacted log members (ADR-0187 follow-on slice). Only the
    # names produced by the collector in THIS build extend the allowlist —
    # a stray file in the staging dir stays out (deny-by-default).
    log_members = collect_recent_logs(log_window_hours)
    for name, text in log_members.items():
        member_path = staging / name
        member_path.parent.mkdir(parents=True, exist_ok=True)
        member_path.write_text(text, encoding="utf-8")
    allowed = list(ALLOWED_MEMBERS) + sorted(set(log_members) - set(ALLOWED_MEMBERS))

    # Manifest over every allowlisted member present so far (D4).
    members: dict[str, dict[str, Any]] = {}
    for name in allowed:
        member = staging / name
        if name == "manifest.json" or not member.exists():
            continue
        data = member.read_bytes()
        members[name] = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
        }
    manifest = {
        "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
        "redaction_ruleset_version": REDACTION_RULESET_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "log_window_hours": log_window_hours,
        "members": members,
    }
    manifest_bytes = _write_json(staging / "manifest.json", manifest)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()

    # Deny-by-default assembly: only allowlisted names leave the staging dir.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    included: list[str] = []
    with tarfile.open(output_path, "w:gz") as tar:
        for name in allowed:
            member = staging / name
            if member.exists():
                tar.add(member, arcname=name)
                included.append(name)

    return SupportBundleResult(
        path=output_path,
        members=included,
        manifest_sha256=manifest_sha256,
    )


def _write_json(path: Path, payload: dict[str, Any]) -> bytes:
    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    path.write_bytes(data)
    return data
