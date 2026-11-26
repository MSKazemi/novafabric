# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""Disposable local OCI registries and synthetic multi-arch images for release-image tests.

Two ``registry:2`` containers stand in for GHCR and Docker Hub. Nothing here talks to a real
registry, needs a credential, or pushes anywhere but ``127.0.0.1``.

The images are built with the real ``docker buildx`` (SBOM + max provenance attached, as
``publish-image.yml`` does) from ``FROM scratch`` plus one file per architecture, so no
emulation is needed for arm64. A "vulnerable" leg carries a Python ``dist-info`` for
Django 1.11.0, whose fixable CRITICAL advisories (e.g. CVE-2019-14234) are permanent.

The independent oracle for "no production tag exists" is the registry's own HTTP API,
never the code under test.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

REGISTRY_IMAGE = "registry:2"
PLATFORMS = ("linux/amd64", "linux/arm64")
_ACCEPT = ",".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)
_VULN_METADATA = "Metadata-Version: 2.1\nName: Django\nVersion: 1.11.0\n"
_CLEAN_METADATA = "Metadata-Version: 2.1\nName: harmless\nVersion: 1.0.0\n"
_DOCKERFILE = (
    "FROM scratch\n"
    "ARG TARGETARCH\n"
    "COPY site-${TARGETARCH}/ /usr/local/lib/python3.12/site-packages/\n"
    "COPY salt /salt\n"
)


def docker(*args: str, timeout: int = 600, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=check
    )


@dataclass
class Registries:
    """``a`` plays GHCR (and hosts the candidate repo), ``b`` plays Docker Hub."""

    a: str
    b: str
    builder: str
    _containers: list[str]

    def teardown(self) -> None:
        for cid in self._containers:
            docker("rm", "-f", cid, check=False)
        docker("buildx", "rm", "--force", self.builder, check=False)


def _wait_ready(host: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://{host}/v2/", timeout=2):  # noqa: S310
                return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError(f"registry at {host} never became ready")


def start_registries() -> Registries:
    containers: list[str] = []
    hosts: list[str] = []
    for _ in range(2):
        cid = docker("run", "-d", "--rm", "-p", "127.0.0.1::5000", REGISTRY_IMAGE).stdout.strip()
        containers.append(cid)
        hosts.append(docker("port", cid, "5000/tcp").stdout.splitlines()[0].strip())
    for host in hosts:
        _wait_ready(host)
    builder = f"release-image-test-{uuid.uuid4().hex[:8]}"
    # host networking so buildkit can push to the 127.0.0.1-published registries
    docker(
        "buildx",
        "create",
        "--name",
        builder,
        "--driver",
        "docker-container",
        "--driver-opt",
        "network=host",
    )
    docker("buildx", "inspect", "--bootstrap", "--builder", builder, timeout=900)
    return Registries(hosts[0], hosts[1], builder, containers)


def build_candidate(
    regs: Registries,
    repo: str,
    tag: str,
    workdir: Path,
    *,
    vulnerable: frozenset[str] = frozenset(),
) -> str:
    """Build+push a 2-platform image with SBOM/provenance; return its index digest."""
    ctx = workdir / f"ctx-{uuid.uuid4().hex[:6]}"
    for arch in ("amd64", "arm64"):
        bad = f"linux/{arch}" in vulnerable
        meta_dir = ctx / f"site-{arch}" / ("Django-1.11.0" if bad else "harmless-1.0.0")
        dist = meta_dir.with_name(meta_dir.name + ".dist-info")
        dist.mkdir(parents=True)
        (dist / "METADATA").write_text(_VULN_METADATA if bad else _CLEAN_METADATA)
    (ctx / "salt").write_text(uuid.uuid4().hex)  # distinct digest per candidate
    (ctx / "Dockerfile").write_text(_DOCKERFILE)
    docker(
        "buildx",
        "build",
        "--builder",
        regs.builder,
        "--platform",
        ",".join(PLATFORMS),
        "--provenance=mode=max",
        "--sbom=true",
        "--push",
        "-t",
        f"{regs.a}/{repo}:{tag}",
        str(ctx),
        timeout=900,
    )
    return digest_of(f"{regs.a}/{repo}:{tag}")


def digest_of(ref: str) -> str:
    raw = docker("buildx", "imagetools", "inspect", "--raw", ref).stdout.encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def raw_manifest(ref: str) -> bytes:
    """Bytes exactly as the registry serves them (stdout decoded as latin-1 would corrupt)."""
    proc = subprocess.run(  # noqa: S603
        ["docker", "buildx", "imagetools", "inspect", "--raw", ref],  # noqa: S607
        capture_output=True,
        timeout=120,
        check=True,
    )
    return proc.stdout


def registry_tags(host: str, repo: str) -> list[str]:
    """Tags the registry itself lists for *repo* — ``[]`` for an unknown repository."""
    try:
        with urllib.request.urlopen(f"http://{host}/v2/{repo}/tags/list", timeout=10) as resp:  # noqa: S310
            return list(json.load(resp).get("tags") or [])
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise


def manifest_exists(host: str, repo: str, digest: str) -> bool:
    request = urllib.request.Request(  # noqa: S310
        f"http://{host}/v2/{repo}/manifests/{digest}",
        headers={"Accept": _ACCEPT},
        method="HEAD",
    )
    try:
        with urllib.request.urlopen(request, timeout=10):  # noqa: S310
            return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise


def tag_digest(host: str, repo: str, tag: str) -> str | None:
    request = urllib.request.Request(  # noqa: S310
        f"http://{host}/v2/{repo}/manifests/{tag}",
        headers={"Accept": _ACCEPT},
        method="HEAD",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:  # noqa: S310
            return str(resp.headers["Docker-Content-Digest"])
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def write_reports(
    report_dir: Path,
    template_dir: Path,
    ref: str,
    *,
    vulnerable: frozenset[str] = frozenset(),
    platforms: tuple[str, ...] = PLATFORMS,
) -> None:
    """Reports shaped like real Trivy output, derived from the captured real captures."""
    clean = json.loads((template_dir / "trivy-linux-amd64.clean.json").read_text())
    vuln = json.loads((template_dir / "trivy-linux-arm64.vulnerable.json").read_text())
    report_dir.mkdir(parents=True, exist_ok=True)
    for platform in platforms:
        os_, arch = platform.split("/")
        data = json.loads(json.dumps(vuln if platform in vulnerable else clean))
        data["ArtifactName"] = ref
        data["Metadata"]["ImageConfig"]["architecture"] = arch
        data["Metadata"]["ImageConfig"]["os"] = os_
        (report_dir / ("trivy-report-" + platform.replace("/", "-") + ".json")).write_text(
            json.dumps(data)
        )
