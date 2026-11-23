#!/usr/bin/env python3
# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""Promote a scanned OCI candidate by digest and prove nothing changed (issue #218).

The release workflow builds a multi-arch candidate into a throw-away registry,
scans that exact digest per platform, and only then calls this script:

* ``verify``  - the bytes behind ``REPO@DIGEST`` hash to DIGEST, the index lists
  every required platform, and every child manifest hashes to its own digest.
* ``promote`` - verify the source, copy it by digest to ``DEST:STAGE`` (a
  non-release staging tag), verify the destination.  Never rebuilds.
* ``tag``     - verify, then move release tags to a digest that is already
  verified, signed and attested.  ``--immutable`` tags refuse to move.

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

DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
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


def _digest_of(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _need_digest(digest: str) -> str:
    if not DIGEST.match(digest or ""):
        raise GateError(f"not a sha256 digest: {digest!r} (an empty build output must fail here)")
    return digest


def verify(repo: str, digest: str, platforms: Sequence[str]) -> None:
    _need_digest(digest)
    body = _imagetools("inspect", "--raw", f"{repo}@{digest}")
    if _digest_of(body) != digest:
        raise GateError(f"{repo}@{digest}: manifest bytes hash to {_digest_of(body)}")
    try:
        children = json.loads(body).get("manifests") or []
        have = {
            f"{c['platform']['os']}/{c['platform']['architecture']}"
            for c in children
            if c.get("platform") and c["platform"].get("os") != "unknown"
        }
        child_digests = [_need_digest(c["digest"]) for c in children]
    except (ValueError, AttributeError, KeyError, TypeError) as exc:
        raise GateError(f"{repo}@{digest}: manifest is not a well-formed image index") from exc
    if not children:
        raise GateError(f"{repo}@{digest}: not an image index (multi-arch is required)")
    missing = sorted(set(platforms) - have)
    if missing:
        raise GateError(f"{repo}@{digest}: index lacks platforms {missing}; has {sorted(have)}")
    for cdig in child_digests:  # attestation manifests are children too
        if _digest_of(_imagetools("inspect", "--raw", f"{repo}@{cdig}")) != cdig:
            raise GateError(f"{repo}@{cdig}: child manifest does not hash to its digest")


def promote(src: str, dest: str, digest: str, stage_tag: str, platforms: Sequence[str]) -> None:
    verify(src, digest, platforms)
    _imagetools("create", "-t", f"{dest}:{stage_tag}", f"{src}@{digest}")
    verify(dest, digest, platforms)
    if _digest_of(_imagetools("inspect", "--raw", f"{dest}:{stage_tag}")) != digest:
        raise GateError(f"{dest}:{stage_tag} does not resolve to {digest} after the copy")


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


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("verify", "promote", "tag"):
        s = sub.add_parser(name)
        s.add_argument("--digest", required=True)
        s.add_argument("--platform", action="append", required=True)
        if name == "verify":
            s.add_argument("--repo", required=True)
        if name == "promote":
            s.add_argument("--src-repo", required=True)
            s.add_argument("--dest-repo", required=True)
            s.add_argument("--stage-tag", required=True)
        if name == "tag":
            s.add_argument("--repo", required=True)
            s.add_argument("--tag", action="append", default=[])
            s.add_argument("--meta-tags", default="")
            s.add_argument("--immutable", action="append", default=[])
    a = p.parse_args(argv)
    try:
        if a.cmd == "verify":
            verify(a.repo, a.digest, a.platform)
        elif a.cmd == "promote":
            promote(a.src_repo, a.dest_repo, a.digest, a.stage_tag, a.platform)
        else:
            names = list(a.tag) + (tags_from_meta(a.meta_tags, a.repo) if a.meta_tags else [])
            if not names:
                raise GateError("no tags to move")
            imm = [i for i in a.immutable if i]
            names.sort(key=lambda n: n not in imm)  # immutable first, stable otherwise
            tag(a.repo, a.digest, names, imm, a.platform)
    except GateError as exc:
        print(f"::error title=Release image gate::{exc}", file=sys.stderr)
        return 1
    print(f"release_image {a.cmd}: ok {a.digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
