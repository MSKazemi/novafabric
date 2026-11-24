# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""Release-image promotion against two REAL local OCI registries (issue #218, container tier).

``tests/test_release_image.py`` pins the script's contract against a fake docker. This
file proves the claims a fake cannot: that ``docker buildx imagetools`` really copies a
multi-arch index (platform manifests + attestation manifests) by digest with identical
bytes, that a refused promotion leaves the production registry exactly as it was, and
that a tag-based copy WOULD have promoted an image nobody scanned.

Two ``registry:2`` containers stand in for the candidate registry and for GHCR / Docker
Hub. Images are pushed through the registry HTTP API with no build step, so the fixture
needs only Docker, loopback and the ``registry:2`` image (no QEMU, no BuildKit, no
trivy). The scanner half of the gate is asserted on the workflow in
``tests/docs/test_publish_image_gates_promotion_on_scan.py`` and executed against real
trivy by the ``image-gate`` job in ``release-toolchain.yml``.

``oci_registries`` is listed in ``tests/conftest.py::CONTAINER_FIXTURES`` so the
``container`` marker and the ``make test-fast`` exclusion apply automatically.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release_image.py"
INDEX = "application/vnd.oci.image.index.v1+json"
MANIFEST = "application/vnd.oci.image.manifest.v1+json"
AMD64, ARM64 = "linux/amd64", "linux/arm64"


def _sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


@dataclass
class Registry:
    host: str  # 127.0.0.1:<port>

    def _req(self, method: str, path: str, data: bytes | None = None, ctype: str = "") -> bytes:
        req = urllib.request.Request(f"http://{self.host}{path}", data=data, method=method)
        if ctype:
            req.add_header("Content-Type", ctype)
        req.add_header("Accept", f"{INDEX}, {MANIFEST}")
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - loopback test registry
            return resp.read()

    def _blob(self, repo: str, data: bytes) -> dict[str, object]:
        digest = _sha(data)
        req = urllib.request.Request(f"http://{self.host}/v2/{repo}/blobs/uploads/", method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            loc = resp.headers["Location"]
        url = loc if loc.startswith("http") else f"http://{self.host}{loc}"
        url += ("&" if "?" in url else "?") + f"digest={digest}"
        put = urllib.request.Request(url, data=data, method="PUT")
        put.add_header("Content-Type", "application/octet-stream")
        urllib.request.urlopen(put, timeout=30)  # noqa: S310
        return {"digest": digest, "size": len(data)}

    def push_image(
        self,
        repo: str,
        platforms: tuple[str, ...] = (AMD64, ARM64),
        salt: str = "",
        tag: str | None = None,
    ) -> str:
        """Push a multi-arch index with one attestation manifest per platform; return its digest."""
        config = self._blob(repo, b"{}")
        entries = []
        for plat in platforms:
            os_, arch = plat.split("/")
            layer = self._blob(repo, f"layer {plat} {salt}".encode())
            body = json.dumps(
                {
                    "schemaVersion": 2,
                    "mediaType": MANIFEST,
                    "config": {"mediaType": "application/vnd.oci.image.config.v1+json", **config},
                    "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar", **layer}],
                }
            ).encode()
            self._req("PUT", f"/v2/{repo}/manifests/{_sha(body)}", body, MANIFEST)
            entries.append(
                {
                    "mediaType": MANIFEST,
                    "digest": _sha(body),
                    "size": len(body),
                    "platform": {"os": os_, "architecture": arch},
                }
            )
            att = json.dumps(
                {
                    "schemaVersion": 2,
                    "mediaType": MANIFEST,
                    "config": {"mediaType": "application/vnd.oci.image.config.v1+json", **config},
                    "layers": [{"mediaType": "application/vnd.in-toto+json", **layer}],
                }
            ).encode()
            self._req("PUT", f"/v2/{repo}/manifests/{_sha(att)}", att, MANIFEST)
            entries.append(
                {
                    "mediaType": MANIFEST,
                    "digest": _sha(att),
                    "size": len(att),
                    "platform": {"os": "unknown", "architecture": "unknown"},
                    "annotations": {
                        "vnd.docker.reference.type": "attestation-manifest",
                        "vnd.docker.reference.digest": _sha(body),
                    },
                }
            )
        index = json.dumps({"schemaVersion": 2, "mediaType": INDEX, "manifests": entries}).encode()
        digest = _sha(index)
        self._req("PUT", f"/v2/{repo}/manifests/{tag or digest}", index, INDEX)
        if tag:  # also addressable by digest
            self._req("PUT", f"/v2/{repo}/manifests/{digest}", index, INDEX)
        return digest

    def tags(self, repo: str) -> list[str]:
        try:
            return sorted(json.loads(self._req("GET", f"/v2/{repo}/tags/list")).get("tags") or [])
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return []
            raise

    def manifest(self, repo: str, ref: str) -> bytes:
        return self._req("GET", f"/v2/{repo}/manifests/{ref}")

    def has(self, repo: str, ref: str) -> bool:
        try:
            self.manifest(repo, ref)
            return True
        except urllib.error.HTTPError:
            return False


@pytest.fixture(scope="module")
def oci_registries() -> Iterator[tuple[Registry, Registry]]:
    """(candidate, production) - two throw-away registry:2 containers on loopback."""
    if not shutil.which("docker"):
        pytest.skip("docker is not installed")
    started: list[str] = []
    regs: list[Registry] = []
    try:
        for _ in range(2):
            name = f"nf-oci-test-{uuid.uuid4().hex[:8]}"
            run = subprocess.run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--rm",
                    "--name",
                    name,
                    "-p",
                    "127.0.0.1::5000",
                    "registry:2",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=300,
            )
            if run.returncode != 0:
                pytest.skip(f"cannot start registry:2: {run.stderr.strip()[-200:]}")
            started.append(name)
            port = (
                subprocess.run(
                    ["docker", "port", name, "5000/tcp"],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30,
                )
                .stdout.split(":")[-1]
                .strip()
            )
            regs.append(Registry(f"127.0.0.1:{port}"))
        import time

        for reg in regs:
            for _ in range(50):
                try:
                    reg._req("GET", "/v2/")
                    break
                except OSError:
                    time.sleep(0.2)
        yield regs[0], regs[1]
    finally:
        for name in started:
            subprocess.run(
                ["docker", "rm", "-f", name], capture_output=True, check=False, timeout=60
            )


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def _reports(host: str, repo: str, digest: str, directory: Path) -> Path:
    """Clean Trivy-shaped reports for ``host/repo@digest``, one per platform."""
    for plat in (AMD64, ARM64):
        os_, arch = plat.split("/")
        (directory / f"trivy-report-{os_}-{arch}.json").write_text(
            json.dumps(
                {
                    "SchemaVersion": 2,
                    "ArtifactType": "container_image",
                    "ArtifactName": f"{host}/{repo}@{digest}",
                    "Metadata": {"ImageConfig": {"os": os_, "architecture": arch}},
                    "Results": [{"Target": "t", "Vulnerabilities": []}],
                }
            )
        )
    return directory


def _promote(
    cand: Registry,
    prod: Registry,
    repo: str,
    digest: str,
    platforms: tuple[str, ...] = (AMD64, ARM64),
    scanned: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Promote with clean reports for ``scanned`` (default: the digest being promoted)."""
    with tempfile.TemporaryDirectory() as tmp:
        reports = _reports(cand.host, repo, scanned or digest, Path(tmp))
        args = [
            "promote",
            "--src-repo",
            f"{cand.host}/{repo}",
            "--dest-repo",
            f"{prod.host}/{repo}",
            "--digest",
            digest,
            "--report-dir",
            str(reports),
        ]
        for p in platforms:
            args += ["--platform", p]
        return _run(*args)


def _tag(
    prod: Registry, repo: str, digest: str, *tags: str, immutable: str = ""
) -> subprocess.CompletedProcess[str]:
    args = [
        "tag",
        "--repo",
        f"{prod.host}/{repo}",
        "--digest",
        digest,
        "--platform",
        AMD64,
        "--platform",
        ARM64,
    ]
    for t in tags:
        args += ["--tag", t]
    if immutable:
        args += ["--immutable", immutable]
    return _run(*args)


# ---- scenario: both platforms pass -> eligible -> promoted byte-for-byte ----------------------


def test_promotion_copies_every_manifest_and_the_bytes_are_identical(
    oci_registries: tuple[Registry, Registry],
) -> None:
    cand, prod = oci_registries
    digest = cand.push_image("a1", salt="v1", tag="candidate")
    done = _promote(cand, prod, "a1", digest)
    assert done.returncode == 0, done.stderr
    assert prod.manifest("a1", digest) == cand.manifest("a1", digest)
    for child in json.loads(cand.manifest("a1", digest))["manifests"]:  # platforms + attestations
        assert prod.manifest("a1", child["digest"]) == cand.manifest("a1", child["digest"])
    assert prod.tags("a1") == [], "a tagless stage copy leaves no tag behind"
    assert _tag(prod, "a1", digest, "1.0.0", "1.0", "latest", immutable="1.0.0").returncode == 0
    assert prod.tags("a1") == ["1.0", "1.0.0", "latest"]


# ---- scenario: digest changed between scan and promotion --------------------------------------


def test_a_repointed_candidate_tag_cannot_smuggle_an_unscanned_image_through(
    oci_registries: tuple[Registry, Registry],
) -> None:
    """Promotion is by the scanned DIGEST; the tag the attacker moved is never consulted."""
    cand, prod = oci_registries
    scanned = cand.push_image("b1", salt="scanned", tag="candidate")
    swapped = cand.push_image("b1", salt="NOT-SCANNED", tag="candidate")  # same tag, new content
    assert swapped != scanned
    # the new image has no scan evidence: reports name the scanned digest only
    assert _promote(cand, prod, "b1", swapped, scanned=scanned).returncode == 1
    assert prod.tags("b1") == [] and not prod.has("b1", swapped)
    assert _promote(cand, prod, "b1", scanned).returncode == 0
    assert prod.has("b1", scanned) and not prod.has("b1", swapped)


def test_promoting_a_digest_the_candidate_registry_does_not_hold_fails_and_writes_nothing(
    oci_registries: tuple[Registry, Registry],
) -> None:
    cand, prod = oci_registries
    cand.push_image("b2", salt="x", tag="candidate")
    never_pushed = _sha(b"unscanned")
    assert _promote(cand, prod, "b2", never_pushed).returncode == 1
    assert prod.tags("b2") == [] and not prod.has("b2", never_pushed)


# ---- scenario: copy alters the manifest --------------------------------------------------------


def test_a_copy_that_changes_the_index_is_not_a_promotion(
    oci_registries: tuple[Registry, Registry],
) -> None:
    """`imagetools create --annotation` rewrites the index, hence its digest. verify must see it."""
    cand, prod = oci_registries
    digest = cand.push_image("c1", salt="y", tag="candidate")
    altered = subprocess.run(
        [
            "docker",
            "buildx",
            "imagetools",
            "create",
            "--annotation",
            "index:org.example.x=1",
            "-t",
            f"{prod.host}/c1:x",
            f"{cand.host}/c1@{digest}",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert altered.returncode == 0, altered.stderr
    assert _sha(prod.manifest("c1", "x")) != digest, "the fixture failed to alter the manifest"
    done = _run(
        "verify",
        "--repo",
        f"{prod.host}/c1",
        "--digest",
        digest,
        "--platform",
        AMD64,
        "--platform",
        ARM64,
    )
    assert done.returncode == 1


# ---- scenario: gate refused -> existing tags and digests untouched ------------------------------


def test_a_refused_promotion_leaves_existing_release_tags_exactly_as_they_were(
    oci_registries: tuple[Registry, Registry],
) -> None:
    cand, prod = oci_registries
    old = prod.push_image("d1", salt="released", tag="1.0.0")
    prod._req("PUT", "/v2/d1/manifests/latest", prod.manifest("d1", "1.0.0"), INDEX)
    before = (prod.tags("d1"), prod.manifest("d1", "1.0.0"), prod.manifest("d1", "latest"))
    arm_missing = cand.push_image("d1", platforms=(AMD64,), salt="new", tag="candidate")
    assert _promote(cand, prod, "d1", arm_missing).returncode == 1  # one platform missing
    assert (prod.tags("d1"), prod.manifest("d1", "1.0.0"), prod.manifest("d1", "latest")) == before
    assert _tag(prod, "d1", arm_missing, "1.0.0", "latest", immutable="1.0.0").returncode == 1
    assert (prod.tags("d1"), prod.manifest("d1", "1.0.0"), prod.manifest("d1", "latest")) == before
    assert _sha(prod.manifest("d1", "1.0.0")) == old


def test_an_immutable_release_tag_is_not_overwritten_by_a_rebuild(
    oci_registries: tuple[Registry, Registry],
) -> None:
    cand, prod = oci_registries
    released = prod.push_image("e1", salt="first-build", tag="2.0.0")
    rebuilt = cand.push_image("e1", salt="second-build-same-source", tag="candidate")
    assert _promote(cand, prod, "e1", rebuilt).returncode == 0  # the untagged copy is fine
    assert _tag(prod, "e1", rebuilt, "2.0.0", "latest", immutable="2.0.0").returncode == 1
    assert _sha(prod.manifest("e1", "2.0.0")) == released
    assert "latest" not in prod.tags("e1")
