#!/usr/bin/env python3
# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""Promote a scanned OCI candidate by digest and prove nothing changed (issue #218).

The release workflow builds a multi-arch candidate into a throw-away registry,
scans that exact digest per platform, and only then calls this script:

* ``verify``  - the bytes behind ``REPO@DIGEST`` hash to DIGEST, the index lists
  every required platform, and every child manifest hashes to its own digest.
* ``check-reports`` - a Trivy JSON report exists for every platform, names this
  digest and the right architecture, and has no fixable CRITICAL finding.
* ``promote`` - check the reports, verify the source, copy it by digest to
  ``DEST@DIGEST`` (no tag; ``--stage-tag`` adds a non-release staging tag for a
  registry that refuses a tagless copy), verify the destination.  Never rebuilds.
* ``tag``     - verify, then move release tags to a digest that is already
  verified, signed and attested.  ``--immutable`` tags refuse to move; a run with no
  immutable tag (a branch dispatch) may not write a name in the release namespace
  (``latest`` or anything starting with a digit, optionally after ``v``).
* ``check-tags`` - the tag-set rules of ``tag`` with no registry and no credential, so
  an unusable tag set fails before the build.

Every failure path exits non-zero (1 = gate refused, 2 = usage); there is no
"warn and continue".  Only ``docker buildx imagetools`` is used, so no tool beyond
what setup-buildx-action already installs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
TAG_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
RELEASE_NAME = re.compile(r"^(latest|v?\d.*)$")
ATTESTATION = "attestation-manifest"
TIMEOUT_S = 300


class GateError(Exception):
    """The image is not provably the scanned candidate. Never swallowed."""


class NotFound(GateError):
    """The registry answered, and the reference does not exist (as opposed to any other failure)."""


def _imagetools(*args: str) -> bytes:
    cmd = ["docker", "buildx", "imagetools", *args]
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT_S, check=False)
    except FileNotFoundError as exc:
        raise GateError(f"docker is not available: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GateError(f"timed out after {TIMEOUT_S}s: {' '.join(cmd)}") from exc
    if done.returncode != 0:
        tail = done.stderr.decode("utf-8", "replace").strip()[-300:]
        # imagetools prints "ERROR: <ref>: not found" when the registry says 404.
        kind = NotFound if tail.endswith(": not found") else GateError
        raise kind(f"exit {done.returncode}: {' '.join(cmd)}: {tail}")
    return done.stdout


def report_name(platform: str) -> str:
    """File the scan step writes for one platform, e.g. ``trivy-report-linux-arm64.json``."""
    return "trivy-report-" + platform.replace("/", "-") + ".json"


def check_report_data(data: object, digest: str, platform: str, *, source: str) -> None:
    """Decide one platform's verdict from a parsed Trivy JSON report. Pass = return.

    A report is evidence only if it names THIS digest and THIS architecture: Trivy
    scans the runner's platform (amd64) when it cannot honour ``--platform`` (e.g. on an
    OCI layout) and then reports a clean, well-formed, wrong-architecture result.
    """
    if not isinstance(data, dict):
        raise GateError(f"{source}: report is not a JSON object")
    if data.get("SchemaVersion") != 2 or data.get("ArtifactType") != "container_image":
        raise GateError(f"{source}: not a Trivy container-image report")
    name = str(data.get("ArtifactName", ""))
    if not name.endswith("@" + digest):
        raise GateError(
            f"{source}: report is for a different image ({name!r}), not the candidate "
            f"digest {digest}; a scan by tag or of another image is not evidence for this digest"
        )
    config = (data.get("Metadata") or {}).get("ImageConfig") or {}
    got = f"{config.get('os')}/{config.get('architecture')}"
    if got != platform:
        raise GateError(
            f"{source}: scanned architecture {got!r} but this report must cover {platform!r} "
            "(trivy silently falls back to the runner's platform when it cannot honour --platform)"
        )
    blocking = [
        f"{v.get('VulnerabilityID')} in {v.get('PkgName')} {v.get('InstalledVersion')}"
        f" (fixed in {v.get('FixedVersion')})"
        for result in data.get("Results") or []
        for v in result.get("Vulnerabilities") or []
        if v.get("Severity") == "CRITICAL" and v.get("FixedVersion")
    ]
    if blocking:
        shown = "; ".join(blocking[:5]) + (" ..." if len(blocking) > 5 else "")
        raise GateError(f"{platform}: {len(blocking)} fixable CRITICAL finding(s): {shown}")


def check_report(path: Path, digest: str, platform: str) -> None:
    if not path.is_file():
        raise GateError(
            f"no scan report for {platform} at {path.name}: the scan did not complete "
            "(timeout, crash or cancellation); absence is never a pass"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise GateError(f"{path.name}: unreadable report: {type(exc).__name__}: {exc}") from exc
    check_report_data(data, digest, platform, source=path.name)


def check_reports(report_dir: Path, digest: str, platforms: Sequence[str]) -> None:
    """Every platform is judged; all failures are reported together."""
    _need_digest(digest)
    failures: list[str] = []
    for platform in platforms:
        try:
            check_report(report_dir / report_name(platform), digest, platform)
        except GateError as exc:
            failures.append(str(exc))
    if failures:
        raise GateError("; ".join(failures))


def _digest_of(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _need_digest(digest: str) -> str:
    if not DIGEST.match(digest or ""):
        raise GateError(f"not a sha256 digest: {digest!r} (an empty build output must fail here)")
    return digest


def _platform_key(os_: str, arch: str, variant: str = "") -> str:
    """``os/arch[/variant]``; arm64/v8 is arm64 (containerd normalises the variant away)."""
    if arch == "arm64" and variant == "v8":
        variant = ""
    return f"{os_}/{arch}" + (f"/{variant}" if variant else "")


def _required_key(platform: str) -> str:
    parts = platform.split("/")
    if len(parts) != 2 or not all(parts):
        raise GateError(f"not a platform (os/arch): {platform!r}")
    return _platform_key(*parts)


def _index_platforms(repo: str, digest: str, children: list[object]) -> tuple[set[str], list[str]]:
    """Image platforms and child digests of an index; every child must be accounted for.

    Only two kinds of child may sit in a scanned index: an image manifest with a real,
    unique platform, and a BuildKit attestation manifest (``unknown/unknown`` carrying the
    ``vnd.docker.reference.type`` annotation). Anything else would ship under the scanned
    digest without anyone having looked at it.
    """
    where = f"{repo}@{digest}"
    have: set[str] = set()
    child_digests: list[str] = []
    for child in children:
        if not isinstance(child, dict):
            raise GateError(f"{where}: manifest is not a well-formed image index")
        child_digests.append(_need_digest(str(child.get("digest"))))
        platform = child.get("platform")
        if not isinstance(platform, dict):
            raise GateError(f"{where}: child {child.get('digest')} has no platform")
        os_, arch = str(platform["os"]), str(platform["architecture"])
        if os_ == "unknown" and arch == "unknown":
            annotations = child.get("annotations")
            kind = (
                annotations.get("vnd.docker.reference.type")
                if isinstance(annotations, dict)
                else None
            )
            if kind != ATTESTATION:
                raise GateError(
                    f"{where}: child {child.get('digest')} is unknown/unknown but not an "
                    f"{ATTESTATION}"
                )
            continue
        if "unknown" in (os_, arch):
            raise GateError(f"{where}: child {child.get('digest')} has platform {os_}/{arch}")
        key = _platform_key(os_, arch, str(platform.get("variant") or ""))
        if key in have:
            raise GateError(f"{where}: duplicate child for platform {key}")
        have.add(key)
    return have, child_digests


def verify(repo: str, digest: str, platforms: Sequence[str]) -> None:
    _need_digest(digest)
    body = _imagetools("inspect", "--raw", f"{repo}@{digest}")
    if _digest_of(body) != digest:
        raise GateError(f"{repo}@{digest}: manifest bytes hash to {_digest_of(body)}")
    try:
        children = json.loads(body).get("manifests") or []
        have, child_digests = _index_platforms(repo, digest, children)
    except (ValueError, AttributeError, KeyError, TypeError, RecursionError) as exc:
        raise GateError(f"{repo}@{digest}: manifest is not a well-formed image index") from exc
    if not children:
        raise GateError(f"{repo}@{digest}: not an image index (multi-arch is required)")
    required = {_required_key(p) for p in platforms}
    missing = sorted(required - have)
    if missing:
        raise GateError(f"{repo}@{digest}: index lacks platforms {missing}; has {sorted(have)}")
    extra = sorted(have - required)
    if extra:
        raise GateError(f"{repo}@{digest}: index carries unscanned platform(s) {extra}")
    for cdig in child_digests:  # attestation manifests are children too
        if _digest_of(_imagetools("inspect", "--raw", f"{repo}@{cdig}")) != cdig:
            raise GateError(f"{repo}@{cdig}: child manifest does not hash to its digest")


def promote(
    src: str,
    dest: str,
    digest: str,
    stage_tag: str | None,
    platforms: Sequence[str],
    report_dir: Path,
) -> None:
    """Copy the scanned digest to ``dest``. Scan evidence is checked before anything else."""
    check_reports(report_dir, digest, platforms)
    verify(src, digest, platforms)
    target = f"{dest}:{stage_tag}" if stage_tag else f"{dest}@{digest}"
    _imagetools("create", "-t", target, f"{src}@{digest}")
    verify(dest, digest, platforms)
    if stage_tag and _digest_of(_imagetools("inspect", "--raw", target)) != digest:
        raise GateError(f"{target} does not resolve to {digest} after the copy")


def tag(
    repo: str, digest: str, tags: Sequence[str], immutable: Sequence[str], platforms: Sequence[str]
) -> None:
    verify(repo, digest, platforms)
    for name in tags:  # immutable first, then moving, in the order given
        if name in immutable:
            try:
                existing = _digest_of(_imagetools("inspect", "--raw", f"{repo}:{name}"))
            except NotFound:
                existing = ""  # the tag does not exist yet; any other failure aborts
            if existing and existing != digest:
                raise GateError(f"{repo}:{name} is immutable and already points at {existing}")
        _imagetools("create", "-t", f"{repo}:{name}", f"{repo}@{digest}")
        if _digest_of(_imagetools("inspect", "--raw", f"{repo}:{name}")) != digest:
            raise GateError(f"{repo}:{name} does not resolve to {digest} after tagging")


def plan_tags(
    meta_tags: str, repo: str, extra: Sequence[str], immutable: Sequence[str]
) -> tuple[list[str], list[str]]:
    """The tag names to move (immutable first) and the immutable subset. No registry access.

    Rules, all fail-closed: at least one tag; every name a valid tag; every immutable tag
    among the names; and without an immutable tag (a run that is not a ``v*`` tag push)
    no name in the release namespace, so a branch called ``0.109.0`` cannot re-point it.
    """
    names = list(extra) + (tags_from_meta(meta_tags, repo) if meta_tags else [])
    if not names:
        raise GateError("no tags to move")
    for name in names:
        if not TAG_NAME.match(name):
            raise GateError(f"not a valid image tag: {name!r}")
    imm = [i for i in immutable if i]
    if not imm:
        release_like = [n for n in names if RELEASE_NAME.match(n)]
        if release_like:
            raise GateError(
                f"{release_like}: a run without an immutable release tag (a branch dispatch) "
                "may not write a name in the release namespace (latest, or starting with a digit)"
            )
    for i in imm:
        if i not in names:
            raise GateError(f"immutable tag {i!r} is not among the tags to move {names}")
    return sorted(names, key=lambda n: n not in imm), imm


def tags_from_meta(meta_tags: str, repo: str) -> list[str]:
    """Tag names that docker/metadata-action derived for ``repo`` (newline list of refs)."""
    prefix = repo.lower() + ":"
    names = [
        ln.strip()[len(prefix) :]
        for ln in meta_tags.splitlines()
        if ln.strip().lower().startswith(prefix)
    ]
    if not names:
        raise GateError(f"metadata-action produced no tag for {repo}")
    return names


def _annotation(message: str) -> str:
    """GitHub workflow-command data escaping: a message must stay on one line."""
    return message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("verify", "check-reports", "promote", "tag", "check-tags"):
        s = sub.add_parser(name)
        if name != "check-tags":
            s.add_argument("--digest", required=True)
            s.add_argument("--platform", action="append", required=True)
        if name in ("verify", "tag", "check-tags"):
            s.add_argument("--repo", required=True)
        if name in ("check-reports", "promote"):
            s.add_argument("--report-dir", type=Path, required=True)
        if name == "promote":
            s.add_argument("--src-repo", required=True)
            s.add_argument("--dest-repo", required=True)
            s.add_argument("--stage-tag", default="")
        if name in ("tag", "check-tags"):
            s.add_argument("--tag", action="append", default=[])
            s.add_argument("--meta-tags", default="")
            s.add_argument("--immutable", action="append", default=[])
    a = p.parse_args(argv)
    try:
        if a.cmd == "verify":
            verify(a.repo, a.digest, a.platform)
        elif a.cmd == "check-reports":
            check_reports(a.report_dir, a.digest, a.platform)
        elif a.cmd == "promote":
            promote(
                a.src_repo, a.dest_repo, a.digest, a.stage_tag or None, a.platform, a.report_dir
            )
        elif a.cmd == "check-tags":
            plan_tags(a.meta_tags, a.repo, a.tag, a.immutable)
        else:
            names, imm = plan_tags(a.meta_tags, a.repo, a.tag, a.immutable)
            tag(a.repo, a.digest, names, imm, a.platform)
    except GateError as exc:
        print(f"::error title=Release image gate::{_annotation(str(exc))}", file=sys.stderr)
        return 1
    print(f"release_image {a.cmd}: ok" + (f" {a.digest}" if a.cmd != "check-tags" else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
