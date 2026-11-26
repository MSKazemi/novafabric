# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""``scripts/release_image.py`` promotes a scanned digest and refuses everything else (#218).

The release workflow scans the candidate by digest, then calls this script to copy THAT
digest to a production registry, prove the copy is byte-identical, and move release tags
onto it. Every refusal below must (a) exit non-zero and (b) have written nothing to the
destination: a gate that fails after it pushed is the defect this script exists to end.

The tests run the real CLI as a subprocess against a fake ``docker`` on PATH. The fake
speaks only ``docker buildx imagetools inspect --raw`` and ``... create -t``; any other
docker verb it is asked for (build, push, login) fails the test, which is how "the
promotion never rebuilds and never pushes by another route" is asserted.

No daemon, no network, no registry: this runs in ``make test-fast``. The same contract
against real registries is ``tests/test_release_image_registry.py``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release_image.py"

AMD64, ARM64 = "linux/amd64", "linux/arm64"
CAND, PROD = "localhost:5000/novafabric", "ghcr.io/mskazemi/novafabric"

FAKE_DOCKER = textwrap.dedent(
    """\
    #!{python}
    import base64, hashlib, json, os, sys
    state_path = os.environ["FAKE_STATE"]
    state = json.load(open(state_path))
    args = sys.argv[1:]
    state["log"].append(args)
    def save():
        json.dump(state, open(state_path, "w"))
    if args[:3] != ["buildx", "imagetools", "inspect"] and args[:3] != ["buildx", "imagetools", "create"]:
        save(); print("fake docker: unexpected verb " + " ".join(args), file=sys.stderr); sys.exit(97)
    if args[2] == "inspect":
        ref = args[-1]
        if state.get("hang"): save(); import time; time.sleep(60)
        body = state["refs"].get(ref)
        err = state.get("errors", {{}}).get(ref)
        save()
        if err:
            print("ERROR: " + err, file=sys.stderr); sys.exit(1)
        if body is None:
            print("ERROR: " + ref + ": not found", file=sys.stderr); sys.exit(1)
        sys.stdout.buffer.write(base64.b64decode(body)); sys.exit(0)
    # create -t DEST SRC
    dest, src = args[args.index("-t") + 1], args[-1]
    body = state["refs"].get(src)
    if body is None:
        save(); print("ERROR: " + src + ": not found", file=sys.stderr); sys.exit(1)
    raw = base64.b64decode(body)
    if state.get("mutate_on_create"):
        raw = raw + b" "
    repo = dest.split("@")[0] if "@" in dest else dest.rsplit(":", 1)[0]
    enc = base64.b64encode(raw).decode()
    state["refs"][dest] = enc
    src_repo = src.split("@")[0] if "@" in src else src.rsplit(":", 1)[0]
    for ref, val in list(state["refs"].items()):  # a real copy brings the children along
        if ref.startswith(src_repo + "@"):
            state["refs"][repo + ref[len(src_repo):]] = val
    state["refs"][repo + "@sha256:" + hashlib.sha256(raw).hexdigest()] = enc
    if state.get("mutate_on_create") and "@" in src:
        # a real registry cannot serve different bytes under the source digest
        state["refs"].pop(repo + "@" + src.split("@")[1], None)
    save()
    """
)


def _sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _image(
    platforms: tuple[str, ...] = (AMD64, ARM64), salt: str = ""
) -> tuple[bytes, dict[str, bytes]]:
    """A multi-arch index with one attestation child per platform. Returns (index, children).

    ``salt`` makes a second, equally valid image with a different digest (a rebuild).
    """
    children: dict[str, bytes] = {}
    entries = []
    for p in platforms:
        os_, arch = p.split("/")
        body = json.dumps({"schemaVersion": 2, "layers": [], "x": p, "salt": salt}).encode()
        children[_sha(body)] = body
        entries.append(
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": _sha(body),
                "size": len(body),
                "platform": {"os": os_, "architecture": arch},
            }
        )
        att = json.dumps({"schemaVersion": 2, "attestation-for": p, "salt": salt}).encode()
        children[_sha(att)] = att
        entries.append(
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": _sha(att),
                "size": len(att),
                "platform": {"os": "unknown", "architecture": "unknown"},
                "annotations": {"vnd.docker.reference.type": "attestation-manifest"},
            }
        )
    index = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": entries,
        },
        indent=1,
    ).encode()
    return index, children


class Fake:
    """Registry state behind the fake docker, plus a CLI runner."""

    def __init__(self, tmp: Path) -> None:
        self.state_path = tmp / "state.json"
        self.state: dict[str, Any] = {"refs": {}, "log": [], "mutate_on_create": False}
        bindir = tmp / "bin"
        bindir.mkdir()
        shim = bindir / "docker"
        shim.write_text(FAKE_DOCKER.format(python=sys.executable))
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
        self.bindir = bindir
        self.reports = tmp / "reports"
        self.flush()

    def write_reports(self, digest: str, bad: tuple[str, ...] = ()) -> Path:
        """Trivy-shaped JSON reports for ``digest``; platforms in ``bad`` carry a fixable CRITICAL."""
        self.reports.mkdir(exist_ok=True)
        for plat in (AMD64, ARM64):
            os_, arch = plat.split("/")
            vulns = (
                [
                    {
                        "VulnerabilityID": "CVE-1",
                        "PkgName": "p",
                        "InstalledVersion": "1",
                        "FixedVersion": "2",
                        "Severity": "CRITICAL",
                    }
                ]
                if plat in bad
                else []
            )
            (self.reports / f"trivy-report-{os_}-{arch}.json").write_text(
                json.dumps(
                    {
                        "SchemaVersion": 2,
                        "ArtifactType": "container_image",
                        "ArtifactName": f"{CAND}@{digest}",
                        "Metadata": {"ImageConfig": {"os": os_, "architecture": arch}},
                        "Results": [{"Target": "t", "Vulnerabilities": vulns}],
                    }
                )
            )
        return self.reports

    def flush(self) -> None:
        self.state_path.write_text(json.dumps(self.state))

    def put(
        self, repo: str, index: bytes, children: dict[str, bytes], tag: str | None = None
    ) -> str:
        digest = _sha(index)
        enc = lambda b: base64.b64encode(b).decode()  # noqa: E731
        self.state["refs"][f"{repo}@{digest}"] = enc(index)
        for d, b in children.items():
            self.state["refs"][f"{repo}@{d}"] = enc(b)
        if tag:
            self.state["refs"][f"{repo}:{tag}"] = enc(index)
        self.flush()
        return digest

    def reload(self) -> dict[str, Any]:
        self.state = json.loads(self.state_path.read_text())
        return self.state

    def creates(self) -> list[list[str]]:
        return [a for a in self.reload()["log"] if a[:3] == ["buildx", "imagetools", "create"]]

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        self.flush()
        env = {
            "PATH": f"{self.bindir}{os.pathsep}/usr/bin:/bin",
            "FAKE_STATE": str(self.state_path),
        }
        done = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )
        self.reload()  # the fake docker wrote its effects to the state file
        return done


@pytest.fixture
def fake(tmp_path: Path) -> Fake:
    return Fake(tmp_path)


def _promote(
    fake: Fake, digest: str, *platforms: str, stage: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Promote with clean scan reports for ``digest`` unless the test wrote its own."""
    if not fake.reports.exists():
        fake.write_reports(digest)
    plats = [x for p in (platforms or (AMD64, ARM64)) for x in ("--platform", p)]
    staging = ["--stage-tag", stage] if stage else []
    return fake.run(
        "promote",
        "--src-repo",
        CAND,
        "--dest-repo",
        PROD,
        "--digest",
        digest,
        "--report-dir",
        str(fake.reports),
        *staging,
        *plats,
    )


def _tag(fake: Fake, digest: str, *extra: str) -> subprocess.CompletedProcess[str]:
    return fake.run(
        "tag", "--repo", PROD, "--digest", digest, "--platform", AMD64, "--platform", ARM64, *extra
    )


# ---------------------------------------------------------------- the happy path


def test_promote_copies_the_digest_and_the_bytes_are_identical(fake: Fake) -> None:
    index, kids = _image()
    digest = fake.put(CAND, index, kids)
    result = _promote(fake, digest)
    assert result.returncode == 0, result.stderr
    refs = fake.reload()["refs"]
    assert refs[f"{PROD}@{digest}"] == base64.b64encode(index).decode()
    assert not [r for r in refs if r.startswith(f"{PROD}:")], "a tagless stage creates no tag"


def test_the_script_only_ever_speaks_imagetools(fake: Fake) -> None:
    """No build, no push, no login: promotion cannot rebuild or take another route."""
    index, kids = _image()
    digest = fake.put(CAND, index, kids)
    assert _promote(fake, digest).returncode == 0
    release = ("--immutable", "1.2.3")
    assert _tag(fake, digest, "--tag", "1.2.3", "--tag", "latest", *release).returncode == 0
    for call in fake.reload()["log"]:
        assert call[:2] == ["buildx", "imagetools"] and call[2] in {"inspect", "create"}, call


# ---------------------------------------------------------------- refusals leave no trace


@pytest.mark.parametrize(
    "bad", ["", "sha256:abc", "latest", "sha256:" + "G" * 64, "sha1:" + "0" * 40]
)
def test_a_missing_or_malformed_digest_is_refused_before_any_registry_call(
    fake: Fake, bad: str
) -> None:
    """An empty build output (`outputs.digest`) must stop the release, not become `repo@`."""
    result = _promote(fake, bad)
    assert result.returncode == 1
    assert fake.reload()["log"] == []


def test_a_source_whose_bytes_do_not_hash_to_the_digest_is_refused_and_nothing_is_written(
    fake: Fake,
) -> None:
    index, kids = _image()
    digest = fake.put(CAND, index, kids)
    fake.state["refs"][f"{CAND}@{digest}"] = base64.b64encode(index + b" ").decode()
    result = _promote(fake, digest)
    assert result.returncode == 1
    assert fake.creates() == []


def test_a_source_missing_a_shipped_platform_is_refused(fake: Fake) -> None:
    index, kids = _image(platforms=(AMD64,))
    digest = fake.put(CAND, index, kids)
    result = _promote(fake, digest)
    assert result.returncode == 1 and "linux/arm64" in result.stderr
    assert fake.creates() == []


def test_an_index_with_a_platform_nobody_scanned_is_refused(fake: Fake) -> None:
    """The caller's platform list is what was scanned; the index is the truth about what ships."""
    index, kids = _image(platforms=(AMD64, ARM64, "linux/s390x"))
    digest = fake.put(CAND, index, kids)
    result = _promote(fake, digest)
    assert result.returncode == 1 and "unscanned" in result.stderr and "s390x" in result.stderr
    assert fake.creates() == []
    assert _tag(fake, digest, "--tag", "1.0.0").returncode == 1


def test_a_single_image_that_is_not_an_index_is_refused(fake: Fake) -> None:
    body = json.dumps({"schemaVersion": 2, "layers": []}).encode()
    digest = fake.put(CAND, body, {})
    assert _promote(fake, digest).returncode == 1
    assert fake.creates() == []


@pytest.mark.parametrize(
    "body",
    [
        b"[]",  # JSON, but not an object
        b'{"manifests": 7}',  # manifests is not a list
        b'{"manifests": [{"platform": {"os": "linux", "architecture": "amd64"}}]}',  # no digest
        b'{"manifests": [{"digest": "sha256:zz", "platform": {"os": "linux", "architecture": "amd64"}}]}',
        b"not json",
    ],
)
def test_a_malformed_index_is_a_refusal_not_a_traceback(fake: Fake, body: bytes) -> None:
    digest = fake.put(CAND, body, {})
    result = _promote(fake, digest)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr, result.stderr
    assert fake.creates() == []


def test_a_corrupt_child_manifest_is_refused(fake: Fake) -> None:
    index, kids = _image()
    digest = fake.put(CAND, index, kids)
    victim = next(iter(kids))
    fake.state["refs"][f"{CAND}@{victim}"] = base64.b64encode(b"{}").decode()
    assert _promote(fake, digest).returncode == 1
    assert fake.creates() == []


def test_a_digest_that_is_not_in_the_candidate_registry_is_refused(fake: Fake) -> None:
    """The scanned digest vanished / was never there: refuse, never fall back to a tag."""
    index, kids = _image()
    fake.put(CAND, index, kids, tag="candidate")
    result = _promote(fake, _sha(index + b"x"))
    assert result.returncode == 1
    assert fake.creates() == []


def test_a_copy_that_alters_the_manifest_fails_verification(fake: Fake) -> None:
    index, kids = _image()
    digest = fake.put(CAND, index, kids)
    fake.state["mutate_on_create"] = True
    result = _promote(fake, digest)
    assert result.returncode == 1, "a destination whose bytes differ must not count as promoted"


def test_docker_missing_is_a_gate_failure_not_a_traceback(fake: Fake, tmp_path: Path) -> None:
    env = {"PATH": str(tmp_path / "empty")}
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(SCRIPT),
            "verify",
            "--repo",
            CAND,
            "--digest",
            "sha256:" + "0" * 64,
            "--platform",
            AMD64,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 1 and "Traceback" not in result.stderr


def test_a_hung_registry_call_times_out_as_a_failure(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("release_image", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def hang(*a: Any, **k: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="docker", timeout=1)

    monkeypatch.setattr(mod.subprocess, "run", hang)
    assert (
        mod.main(["verify", "--repo", CAND, "--digest", "sha256:" + "0" * 64, "--platform", AMD64])
        == 1
    )


# ---------------------------------------------------------------- scan evidence gates the copy


def test_the_tagless_stage_copy_targets_the_digest_not_a_tag(fake: Fake) -> None:
    """First choice (#218 task d): copy to ``DEST@digest`` so no staging tag is ever visible."""
    index, kids = _image()
    digest = fake.put(CAND, index, kids)
    assert _promote(fake, digest).returncode == 0
    (create,) = fake.creates()
    assert create[create.index("-t") + 1] == f"{PROD}@{digest}"


def test_a_stage_tag_is_the_documented_fallback_and_still_verifies(fake: Fake) -> None:
    """One line in the workflow (`--stage-tag scanned-<hex12>`) if a registry refuses tagless."""
    index, kids = _image()
    digest = fake.put(CAND, index, kids)
    assert _promote(fake, digest, stage="scanned-abc").returncode == 0
    (create,) = fake.creates()
    assert create[create.index("-t") + 1] == f"{PROD}:scanned-abc"
    assert fake.reload()["refs"][f"{PROD}:scanned-abc"] == base64.b64encode(index).decode()


@pytest.mark.parametrize(
    "bad", [(ARM64,), (AMD64,), (AMD64, ARM64)], ids=["arm64", "amd64", "both"]
)
def test_a_platform_with_a_fixable_critical_means_no_copy_at_all(
    fake: Fake, bad: tuple[str, ...]
) -> None:
    index, kids = _image()
    digest = fake.put(CAND, index, kids)
    fake.write_reports(digest, bad=bad)
    result = _promote(fake, digest)
    assert result.returncode == 1 and "fixable CRITICAL" in result.stderr
    assert fake.creates() == [], "a failed scan must write nothing to production"
    assert not [r for r in fake.reload()["refs"] if r.startswith(PROD)]


@pytest.mark.parametrize("victim", [AMD64, ARM64])
def test_a_missing_scan_report_means_no_copy(fake: Fake, victim: str) -> None:
    """Timeout, crash or cancel leaves no report; the unknown verdict is a refusal."""
    index, kids = _image()
    digest = fake.put(CAND, index, kids)
    fake.write_reports(digest)
    (fake.reports / f"trivy-report-{victim.replace('/', '-')}.json").unlink()
    result = _promote(fake, digest)
    assert result.returncode == 1 and "no scan report" in result.stderr
    assert fake.creates() == []


@pytest.mark.parametrize("junk", ["", "not json", "{}", "[]", '{"SchemaVersion": 2}'])
def test_an_unreadable_scan_report_means_no_copy(fake: Fake, junk: str) -> None:
    index, kids = _image()
    digest = fake.put(CAND, index, kids)
    fake.write_reports(digest)
    (fake.reports / "trivy-report-linux-arm64.json").write_text(junk)
    assert _promote(fake, digest).returncode == 1
    assert fake.creates() == []


def test_reports_for_another_digest_mean_no_copy(fake: Fake) -> None:
    """The candidate changed after it was scanned: reports no longer describe this digest."""
    index, kids = _image()
    digest = fake.put(CAND, index, kids)
    fake.write_reports("sha256:" + "1" * 64)
    result = _promote(fake, digest)
    assert result.returncode == 1 and "different image" in result.stderr
    assert fake.creates() == []


def test_an_arm64_report_that_silently_scanned_amd64_means_no_copy(fake: Fake) -> None:
    """TRIVY_PLATFORM that never reached trivy: a clean, well-formed, WRONG-arch report."""
    index, kids = _image()
    digest = fake.put(CAND, index, kids)
    fake.write_reports(digest)
    amd = fake.reports / "trivy-report-linux-amd64.json"
    (fake.reports / "trivy-report-linux-arm64.json").write_bytes(amd.read_bytes())
    result = _promote(fake, digest)
    assert result.returncode == 1 and "architecture" in result.stderr
    assert fake.creates() == []


def test_a_tagless_copy_the_registry_refuses_is_a_failure_with_nothing_tagged(fake: Fake) -> None:
    """Fake docker fails the create: exit 1, no tag, no 'warn and continue'."""
    index, kids = _image()
    digest = fake.put(CAND, index, kids)
    fake.state["errors"] = {f"{CAND}@{digest}": "boom"}  # inspect of the source now fails
    result = _promote(fake, digest)
    assert result.returncode == 1
    assert fake.creates() == []


# ---------------------------------------------------------------- moving the release tags


@pytest.mark.parametrize("stored", [False, True], ids=["digest-absent", "extra-platform"])
def test_tags_move_only_onto_a_digest_that_verifies(fake: Fake, stored: bool) -> None:
    """``tag`` must verify the image itself; the tag-name rules refuse nothing here.

    The names are valid for a release (``--immutable`` given), so only ``verify`` can refuse:
    either the digest is not in the registry at all, or it is there but its index carries a
    platform nobody scanned.
    """
    if stored:
        index, kids = _image(platforms=(AMD64, ARM64, "linux/s390x"))
        digest = fake.put(PROD, index, kids)
    else:
        index, _ = _image()
        digest = _sha(index)  # never stored in PROD
    result = _tag(fake, digest, "--tag", "1.2.3", "--tag", "latest", "--immutable", "1.2.3")
    assert result.returncode == 1 and "::error" in result.stderr
    assert "release namespace" not in result.stderr, "refused for the wrong reason"
    assert fake.creates() == []


def test_tags_resolve_to_the_digest_after_moving(fake: Fake) -> None:
    index, kids = _image()
    digest = fake.put(PROD, index, kids)
    release = ("--immutable", "1.2.3")
    assert (
        _tag(fake, digest, "--tag", "1.2.3", "--tag", "1.2", "--tag", "latest", *release).returncode
        == 0
    )
    refs = fake.reload()["refs"]
    assert all(
        refs[f"{PROD}:{t}"] == base64.b64encode(index).decode() for t in ("1.2.3", "1.2", "latest")
    )


def test_an_immutable_tag_never_moves_to_a_different_digest_and_no_other_tag_moves_first(
    fake: Fake,
) -> None:
    old_index, old_kids = _image()
    old = fake.put(PROD, old_index, old_kids, tag="2.0.0")
    fake.state["refs"][f"{PROD}:latest"] = fake.state["refs"][f"{PROD}:2.0.0"]
    new_index, new_kids = _image(
        salt="rebuild"
    )  # valid: verify passes, so the immutable check runs
    new = fake.put(PROD, new_index, new_kids)
    assert new != old
    result = _tag(fake, new, "--tag", "latest", "--tag", "2.0.0", "--immutable", "2.0.0")
    assert result.returncode == 1 and "immutable" in result.stderr, result.stderr
    assert fake.creates() == [], "the moving tag moved before the immutable tag was checked"
    assert fake.reload()["refs"][f"{PROD}:latest"] == base64.b64encode(old_index).decode()


def test_an_unreadable_immutable_tag_is_not_treated_as_absent(fake: Fake) -> None:
    """A registry error while reading the immutable tag must stop the release, not fall through.

    Only a 404 means "this release tag does not exist yet". A 500, a timeout or an auth
    failure says nothing about where X.Y.Z points, and moving it blind could overwrite a
    published release.
    """
    index, kids = _image()
    digest = fake.put(PROD, index, kids)
    fake.state["errors"] = {f"{PROD}:2.0.0": "failed to do request: 503 Service Unavailable"}
    result = _tag(fake, digest, "--tag", "2.0.0", "--tag", "latest", "--immutable", "2.0.0")
    assert result.returncode == 1 and "503" in result.stderr
    assert fake.creates() == []


def test_re_tagging_the_same_digest_is_idempotent(fake: Fake) -> None:
    index, kids = _image()
    digest = fake.put(PROD, index, kids, tag="2.0.0")
    assert _tag(fake, digest, "--tag", "2.0.0", "--immutable", "2.0.0").returncode == 0


def test_an_empty_immutable_value_is_ignored_for_branch_dispatches(fake: Fake) -> None:
    index, kids = _image()
    digest = fake.put(PROD, index, kids)
    assert _tag(fake, digest, "--tag", "main", "--immutable", "").returncode == 0


def test_a_tag_move_that_lands_on_another_digest_fails(fake: Fake) -> None:
    index, kids = _image()
    digest = fake.put(PROD, index, kids)
    fake.state["mutate_on_create"] = True
    assert _tag(fake, digest, "--tag", "latest").returncode == 1


def test_meta_tags_are_filtered_to_the_registry_being_tagged(fake: Fake) -> None:
    index, kids = _image()
    digest = fake.put(PROD, index, kids)
    meta = "\n".join(
        [
            "ghcr.io/MSKazemi/novafabric:1.2.3",
            "ghcr.io/MSKazemi/novafabric:latest",
            "docker.io/kazemi/novafabric:1.2.3",
        ]
    )
    assert _tag(fake, digest, "--meta-tags", meta, "--immutable", "1.2.3").returncode == 0
    dests = [a[a.index("-t") + 1] for a in fake.creates()]
    assert dests == [f"{PROD}:1.2.3", f"{PROD}:latest"], (
        "docker.io tags must not be applied to GHCR, and the immutable tag moves first"
    )


def test_no_tags_for_this_registry_is_a_failure(fake: Fake) -> None:
    index, kids = _image()
    digest = fake.put(PROD, index, kids)
    result = _tag(fake, digest, "--meta-tags", "docker.io/kazemi/novafabric:1.2.3")
    assert result.returncode == 1 and fake.creates() == []


# ---------------------------------------------------------------- a crafted index is not the scanned image

INDEX_MT = "application/vnd.oci.image.index.v1+json"
MANIFEST_MT = "application/vnd.oci.image.manifest.v1+json"
ATTESTATION = {"vnd.docker.reference.type": "attestation-manifest"}
P_AMD = {"os": "linux", "architecture": "amd64"}
P_ARM = {"os": "linux", "architecture": "arm64"}
P_UNKNOWN = {"os": "unknown", "architecture": "unknown"}


def _crafted(
    entries: list[tuple[dict[str, str] | None, dict[str, str] | None]],
) -> tuple[bytes, dict[str, bytes]]:
    """An index built from raw (platform, annotations) entries, valid or not."""
    children: dict[str, bytes] = {}
    manifests: list[dict[str, Any]] = []
    for n, (platform, annot) in enumerate(entries):
        body = json.dumps({"schemaVersion": 2, "layers": [], "crafted": n}).encode()
        children[_sha(body)] = body
        m: dict[str, Any] = {"mediaType": MANIFEST_MT, "digest": _sha(body), "size": len(body)}
        if platform is not None:
            m["platform"] = platform
        if annot:
            m["annotations"] = annot
        manifests.append(m)
    index = json.dumps({"schemaVersion": 2, "mediaType": INDEX_MT, "manifests": manifests}).encode()
    return index, children


_GOOD = [(P_AMD, None), (P_ARM, None), (P_UNKNOWN, ATTESTATION)]


@pytest.mark.parametrize(
    ("entries", "why"),
    [
        ([*_GOOD, (P_ARM, None)], "duplicate"),
        ([*_GOOD, (None, None)], "platform"),
        ([*_GOOD, ({"os": "unknown", "architecture": "arm64"}, None)], "unknown"),
        ([*_GOOD, ({"os": "linux", "architecture": "unknown"}, None)], "unknown"),
        ([*_GOOD, (P_UNKNOWN, None)], "attestation"),
        ([*_GOOD, (P_UNKNOWN, {"vnd.docker.reference.type": "something-else"})], "attestation"),
        ([(P_AMD, None), ({**P_ARM, "variant": "v7"}, None), (P_UNKNOWN, ATTESTATION)], "lacks"),
        ([(P_AMD, None), (P_ARM, None), ({**P_ARM, "variant": "v7"}, None)], "unscanned"),
    ],
    ids=[
        "duplicate-arm64",
        "platform-less-child",
        "unknown-os-arm64",
        "linux-unknown-arch",
        "unknown-without-annotation",
        "unknown-with-wrong-annotation",
        "arm64-variant-v7-not-scanned",
        "extra-arm64-v7-child",
    ],
)
def test_a_crafted_index_is_refused_and_nothing_is_written(
    fake: Fake, entries: list[Any], why: str
) -> None:
    """Only real image children and real attestation manifests may sit in the scanned index.

    A child the scan never looked at (a second arm64, another variant, a platform-less
    entry, an "unknown" child that is not an attestation) would ship under the scanned digest.
    """
    index, kids = _crafted(entries)
    digest = fake.put(CAND, index, kids)
    result = _promote(fake, digest)
    assert result.returncode == 1, result.stdout
    assert why in result.stderr, result.stderr
    assert fake.creates() == []
    assert _tag(fake, digest, "--tag", "main").returncode == 1


def test_a_real_attestation_manifest_and_arm64_v8_are_accepted(fake: Fake) -> None:
    """arm64 and arm64/v8 are the same platform (containerd normalises v8 away); a variant is not scanned separately."""
    entries = [(P_AMD, None), ({**P_ARM, "variant": "v8"}, None), (P_UNKNOWN, ATTESTATION)]
    index, kids = _crafted(entries)
    digest = fake.put(CAND, index, kids)
    assert _promote(fake, digest).returncode == 0


# ---------------------------------------------------------------- tag namespace of a non-release run


@pytest.mark.parametrize("name", ["0.109.0", "1.2", "1", "latest", "v1.2.3", "2.0.0-rc.1"])
def test_a_non_release_run_cannot_write_a_release_looking_tag(fake: Fake, name: str) -> None:
    """A manual dispatch from a branch named like a version must not re-point X.Y.Z, X.Y or latest."""
    index, kids = _image()
    digest = fake.put(PROD, index, kids, tag="9.9.9")
    result = _tag(fake, digest, "--tag", "main", "--tag", name)  # no --immutable: not a tag push
    assert result.returncode == 1 and "release" in result.stderr, result.stderr
    assert fake.creates() == [], "even the harmless tag must not move when one name is refused"


def test_a_non_release_run_cannot_write_release_names_via_the_metadata_tags(fake: Fake) -> None:
    index, kids = _image()
    digest = fake.put(PROD, index, kids)
    meta = "ghcr.io/MSKazemi/novafabric:0.109.0\nghcr.io/MSKazemi/novafabric:sha-abc1234"
    result = _tag(fake, digest, "--meta-tags", meta)
    assert result.returncode == 1 and fake.creates() == []


@pytest.mark.parametrize("name", ["main", "feature-x", "sha-abc1234", "release-1.2", "vnext"])
def test_a_non_release_run_may_still_write_branch_and_sha_tags(fake: Fake, name: str) -> None:
    index, kids = _image()
    digest = fake.put(PROD, index, kids)
    assert _tag(fake, digest, "--tag", name).returncode == 0


def test_the_immutable_tag_must_be_one_of_the_tags_being_moved(fake: Fake) -> None:
    """A tag push whose metadata derived no X.Y.Z (a non-semver v* tag) must stop the release."""
    index, kids = _image()
    digest = fake.put(PROD, index, kids)
    meta = "ghcr.io/MSKazemi/novafabric:latest"
    result = _tag(fake, digest, "--meta-tags", meta, "--immutable", "1.2.3")
    assert result.returncode == 1 and "1.2.3" in result.stderr
    assert fake.creates() == []


def _check_tags(fake: Fake, *args: str) -> subprocess.CompletedProcess[str]:
    return fake.run("check-tags", "--repo", "ghcr.io/MSKazemi/novafabric", *args)


def test_check_tags_passes_a_real_release_and_a_plain_branch_dispatch(fake: Fake) -> None:
    release = "ghcr.io/MSKazemi/novafabric:1.2.3\nghcr.io/MSKazemi/novafabric:1.2\nghcr.io/MSKazemi/novafabric:latest"
    assert _check_tags(fake, "--meta-tags", release, "--immutable", "1.2.3").returncode == 0
    branch = "ghcr.io/MSKazemi/novafabric:main\nghcr.io/MSKazemi/novafabric:sha-abc1234"
    assert _check_tags(fake, "--meta-tags", branch).returncode == 0
    assert fake.reload()["log"] == [], "check-tags is a pure check: it never talks to a registry"


@pytest.mark.parametrize(
    ("meta", "immutable"),
    [
        ("", ""),
        ("", "1.2.3"),
        ("docker.io/other/novafabric:1.2.3", "1.2.3"),
        ("ghcr.io/MSKazemi/novafabric:latest", "1.2.3"),
        ("ghcr.io/MSKazemi/novafabric:0.109.0\nghcr.io/MSKazemi/novafabric:sha-abc1234", ""),
    ],
    ids=[
        "no-tags",
        "tag-push-no-tags",
        "wrong-registry",
        "no-semver",
        "branch-named-like-a-version",
    ],
)
def test_check_tags_fails_early_on_an_unusable_tag_set(
    fake: Fake, meta: str, immutable: str
) -> None:
    """Cheap and credential-free: fail before the build, the copy and the signature."""
    result = _check_tags(fake, "--meta-tags", meta, "--immutable", immutable)
    assert result.returncode == 1 and "::error" in result.stderr
    assert fake.reload()["log"] == []


# ---------------------------------------------------------------- hostile text must not forge workflow commands


def test_registry_error_text_cannot_forge_workflow_commands(fake: Fake) -> None:
    """GitHub parses every stderr/stdout line starting with ``::`` as a workflow command."""
    index, kids = _image()
    digest = fake.put(CAND, index, kids)
    fake.state["errors"] = {f"{CAND}@{digest}": "boom\n::warning::forged\r::add-mask::s3cret 100%"}
    result = _promote(fake, digest)
    assert result.returncode == 1
    lines = result.stderr.splitlines()
    assert [ln for ln in lines if ln.startswith("::")] == [lines[-1]], result.stderr
    assert lines[-1].startswith("::error title=Release image gate::")
    assert "%0A" in lines[-1] and "%0D" in lines[-1] and "%25" in lines[-1]
    assert "\r" not in result.stderr


def test_tag_names_must_be_valid_tags_and_cannot_forge_workflow_commands(fake: Fake) -> None:
    index, kids = _image()
    digest = fake.put(PROD, index, kids)
    result = _tag(fake, digest, "--tag", "x\n::set-output name=a::b")
    assert result.returncode == 1 and fake.creates() == []
    lines = result.stderr.splitlines()
    assert [ln for ln in lines if ln.startswith("::")] == [lines[-1]]


# ---------------------------------------------------------------- hostile JSON fails closed, cleanly


def test_a_deeply_nested_index_is_a_clean_refusal(fake: Fake) -> None:
    body = b"[" * 400_000 + b"]" * 400_000
    digest = fake.put(CAND, body, {})
    result = _promote(fake, digest)
    assert result.returncode == 1 and "Traceback" not in result.stderr, result.stderr[-300:]
    assert fake.creates() == []


def test_a_deeply_nested_scan_report_is_a_clean_refusal(fake: Fake) -> None:
    index, kids = _image()
    digest = fake.put(CAND, index, kids)
    fake.write_reports(digest)
    (fake.reports / "trivy-report-linux-arm64.json").write_text("[" * 400_000 + "]" * 400_000)
    result = _promote(fake, digest)
    assert result.returncode == 1 and "Traceback" not in result.stderr, result.stderr[-300:]
    assert fake.creates() == []
