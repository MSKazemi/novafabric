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

Privacy-review slice (2026-11-27): every member except ``versions.json`` is passed through the
local-identity scrub (:mod:`novafabric.support_bundle._scrub` — home directory,
user name in a path, this host's name), tar entries carry no owner name or id,
and ``MANIFEST.txt`` tells the human reading the archive what each file is, what
was never collected, and what to check before sharing it.
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
from novafabric.support_bundle._scrub import (
    LOCAL_SCRUB_VERSION,
    LocalIdentity,
    scrub_text,
    scrub_value,
)

__all__ = [
    "ALLOWED_MEMBERS",
    "BUNDLE_SCHEMA_VERSION",
    "LOCAL_SCRUB_VERSION",
    "MANIFEST_TEXT_MEMBER",
    "REDACTION_RULESET_VERSION",
    "SupportBundleResult",
    "build_support_bundle",
    "collect_recent_logs",
]

#: Bundle schema version recorded in ``manifest.json``.
BUNDLE_SCHEMA_VERSION = "1"

#: The human-readable table of contents (what each file is and why it is there).
MANIFEST_TEXT_MEMBER = "MANIFEST.txt"

#: The static allowlist (ADR-0187 D3). A file that is not named here is
#: never added to the tarball, even if present in the staging directory.
#: The only dynamic extension is the bounded-log slice: ``logs/<file>.log``
#: member names returned by :func:`collect_recent_logs` in this build (and
#: nothing else) are additionally allowed.
ALLOWED_MEMBERS: tuple[str, ...] = (
    MANIFEST_TEXT_MEMBER,
    "doctor.json",
    "versions.json",
    "config.redacted.yaml",
    "env.txt",
    "health.json",
    LOGS_README_MEMBER,
    "manifest.json",
)

#: Why each static member is in the bundle, as printed in ``MANIFEST.txt``.
#: A guard test fails if an allowlisted member has no entry here.
MEMBER_PURPOSES: dict[str, str] = {
    MANIFEST_TEXT_MEMBER: "This file: what each file is, what was left out, and what to check.",
    "doctor.json": (
        "Storage health as `nova doctor --check-storage` sees it: backend name, schema "
        "version, migration state, row counts per table, or the error that stopped the check."
    ),
    "versions.json": (
        "NovaFabric, Python and operating-system versions, and which optional extras are "
        "fully installed. Most problems start with a version or a missing extra."
    ),
    "config.redacted.yaml": (
        "The server config file, with every value under a secret-like key replaced by "
        "[REDACTED]. Present only when a server config exists and parses."
    ),
    "env.txt": (
        "The NAMES of the NOVAFABRIC_* and NOVA_* environment variables that are set. "
        "Values are never read into the bundle."
    ),
    "health.json": (
        "Whether the data directory, registry database and capsule directory exist, the "
        "registry size and the number of capsules. No capsule is opened."
    ),
    LOGS_README_MEMBER: (
        "Present when no recent log file was found, saying where the collector looked."
    ),
    "manifest.json": (
        "SHA-256 and size of every other file plus the rule versions in force, so a "
        "recipient can check that nothing was changed after the bundle was made."
    ),
}

_LOG_PURPOSE = (
    "The redacted tail of a recent log file from NOVAFABRIC_LOG_DIR or $NOVAFABRIC_HOME/logs. "
    "Copied text: review it."
)

#: Why a static member can be missing, as printed in ``MANIFEST.txt``.
_ABSENT_REASONS: dict[str, str] = {
    "config.redacted.yaml": "no server config file was found, or it did not parse as YAML",
    LOGS_README_MEMBER: "recent log files were found and are included under logs/",
}


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
    identity: LocalIdentity | None = None,
) -> SupportBundleResult:
    """Build a redacted diagnostics tarball at *output_path*.

    ``log_window_hours`` bounds the ``logs/`` members (only ``*.log`` files
    modified within the window are collected, tail-truncated to a total
    byte budget and line-redacted) and is recorded in the manifest.
    ``config_path`` overrides the default server-config location.
    ``staging_dir`` is exposed for tests; only allowlisted member names
    are ever taken from it (deny-by-default). ``identity`` overrides the
    home directory, user and host names scrubbed from every member (tests);
    by default they are read from the running process.
    """
    resolved_identity = identity or LocalIdentity.current()
    if staging_dir is not None:
        return _build_in(staging_dir, output_path, log_window_hours, config_path, resolved_identity)
    with tempfile.TemporaryDirectory(prefix="nova-support-bundle-") as tmp:
        return _build_in(Path(tmp), output_path, log_window_hours, config_path, resolved_identity)


def _build_in(
    staging: Path,
    output_path: Path,
    log_window_hours: int,
    config_path: Path | None,
    identity: LocalIdentity,
) -> SupportBundleResult:
    staging.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(timezone.utc).isoformat()

    _write_json(staging / "doctor.json", scrub_value(collect_doctor(), identity))
    # Not scrubbed: versions and platform strings hold no path, and a host named
    # like a distribution or a kernel ("ubuntu", "linux") would rewrite them.
    _write_json(staging / "versions.json", collect_versions())
    _write_json(staging / "health.json", scrub_value(collect_health(), identity))
    (staging / "env.txt").write_text(scrub_text(collect_env_names(), identity), encoding="utf-8")
    redacted_config = collect_redacted_config(config_path, identity=identity)
    if redacted_config is not None:
        (staging / "config.redacted.yaml").write_text(redacted_config, encoding="utf-8")

    # Bounded, redacted log members (ADR-0187 follow-on slice). Only the
    # names produced by the collector in THIS build extend the allowlist —
    # a stray file in the staging dir stays out (deny-by-default). A log file
    # name can carry the host name too, so names are scrubbed like content.
    log_members = _scrub_log_members(collect_recent_logs(log_window_hours), identity)
    for name, text in log_members.items():
        member_path = staging / name
        member_path.parent.mkdir(parents=True, exist_ok=True)
        member_path.write_text(text, encoding="utf-8")
    allowed = list(ALLOWED_MEMBERS) + sorted(set(log_members) - set(ALLOWED_MEMBERS))

    # The human-readable table of contents. Written before manifest.json so
    # the manifest hashes it like any other member.
    present = [
        name
        for name in allowed
        if name not in (MANIFEST_TEXT_MEMBER, "manifest.json") and (staging / name).exists()
    ]
    (staging / MANIFEST_TEXT_MEMBER).write_text(
        render_manifest_text(present, created_at=created_at, log_window_hours=log_window_hours),
        encoding="utf-8",
    )

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
        "local_scrub_version": LOCAL_SCRUB_VERSION,
        "created_at": created_at,
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
                tar.add(member, arcname=name, filter=_anonymise_tarinfo)
                included.append(name)

    return SupportBundleResult(
        path=output_path,
        members=included,
        manifest_sha256=manifest_sha256,
    )


def render_manifest_text(present: list[str], *, created_at: str, log_window_hours: int) -> str:
    """The body of ``MANIFEST.txt`` for a bundle holding *present* members.

    Plain text on purpose: the person deciding whether to share the archive
    should not need a JSON viewer to find out what is in it.
    """
    listed = [MANIFEST_TEXT_MEMBER, *present, "manifest.json"]
    width = max(len(name) for name in (*listed, *ALLOWED_MEMBERS)) + 2
    lines = [
        "NovaFabric support bundle - contents",
        "====================================",
        "",
        f"Created {created_at} by `nova support-bundle` (experimental).",
        "It was written to this computer only. Nothing was uploaded or sent.",
        "Review every file below before you decide to share the archive.",
        "",
        "Files in this bundle",
        "--------------------",
    ]
    for name in listed:
        purpose = MEMBER_PURPOSES.get(name, _LOG_PURPOSE)
        lines.append(f"{name.ljust(width)}{purpose}")
    absent = [name for name in ALLOWED_MEMBERS if name not in listed and name in _ABSENT_REASONS]
    if absent:
        lines += ["", "Not in this bundle", "------------------"]
        lines += [f"{name.ljust(width)}{_ABSENT_REASONS[name]}" for name in absent]
    lines += [
        "",
        "Never collected",
        "---------------",
        "- environment-variable values (env.txt lists names only)",
        "- the serve token, issued tokens, signing keys and certificates",
        "- Run Capsule payloads, prompts and responses: no collector opens a capsule",
        "",
        "Redaction applied",
        "-----------------",
        f"- redaction ruleset {REDACTION_RULESET_VERSION}: values under secret-like keys "
        "(token, secret, password, key, dsn, credential), known credential shapes and the "
        "password in a URL (scheme://user:password@host) become [REDACTED]",
        f"- local identity scrub {LOCAL_SCRUB_VERSION}: your home directory becomes ~, the "
        "user name after a home, scratch or tmp directory or in a user= field becomes "
        "[USER], and this computer's host name, as a whole word, becomes [HOSTNAME] "
        "(versions.json is left as is); archive entries carry no owner name or id",
        "",
        "Check before sharing",
        "--------------------",
        "Redaction is pattern-based. It cannot recognise every secret, and it keeps",
        "remote host names, IP addresses and database user names because they are",
        "often the diagnosis. A host name joined to other text by - or _ (for",
        "example serve-<host>.log) and a user name elsewhere in free text are kept.",
    ]
    if any(name.startswith("logs/") and name.endswith(".log") for name in present):
        lines += [
            f"The logs/ files are copied text from the last {log_window_hours} hour(s): they",
            "contain whatever the programs writing them logged, which can include",
            "prompts, responses or personal data. Read them, or delete them from the",
            "archive, before sharing.",
        ]
    lines += [
        "",
        "To inspect:  tar -tzvf <bundle>.tar.gz",
        "To extract:  mkdir review && tar -xzf <bundle>.tar.gz -C review",
        "",
    ]
    return "\n".join(lines)


def _scrub_log_members(members: dict[str, str], identity: LocalIdentity) -> dict[str, str]:
    """Scrub log member names and content; two names that scrub to the same
    value are kept apart with a numeric suffix rather than one overwriting the other."""
    scrubbed: dict[str, str] = {}
    for name, text in members.items():
        new_name = scrub_text(name, identity)
        if new_name in scrubbed:
            stem, dot, ext = new_name.rpartition(".")
            base, suffix = (stem, f".{ext}") if dot else (new_name, "")
            counter = 2
            while f"{base}-{counter}{suffix}" in scrubbed:
                counter += 1
            new_name = f"{base}-{counter}{suffix}"
        scrubbed[new_name] = scrub_text(text, identity)
    return scrubbed


def _anonymise_tarinfo(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Strip the local owner from an archive entry.

    ``tarfile.add`` records the staging file's uid, gid and — looked up from the
    password database — the owner's user and group *names*. Those names are the
    local account the bundle was cut on, which is exactly what the scrub removes
    from the content.
    """
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mode = 0o644 if info.isfile() else 0o755
    return info


def _write_json(path: Path, payload: dict[str, Any]) -> bytes:
    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    path.write_bytes(data)
    return data
