# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""Release-image promotion scenarios against REAL disposable registries (issue #218), container tier.

Two ``registry:2`` containers stand in for GHCR (``a``, which also hosts the candidate) and
Docker Hub (``b``). Candidates are built by the real ``docker buildx`` with SBOM and max
provenance, as ``publish-image.yml`` builds them, and every copy is done by the real
``docker buildx imagetools`` inside ``scripts/release_image.py``.

"Zero production tags" is asserted through the registry's own HTTP API, never through the
code under test. Nothing here touches a real registry or a credential.

Companions: ``tests/test_release_image.py`` (script contract, fake docker),
``tests/test_release_image_scan_evidence.py`` (verdict rules over real Trivy output),
``tests/test_release_image_registry.py`` (hand-pushed images, no BuildKit needed) and
``tests/docs/test_publish_image_gates_promotion_on_scan.py`` (what the workflow may do, and
in which order, which is where signing and attestation failures are pinned).
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest

from . import _oci_registry as oci

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "oci-promotion"
SCRIPT = REPO / "scripts" / "release_image.py"

_spec = importlib.util.spec_from_file_location("release_image_scenarios", SCRIPT)
assert _spec and _spec.loader
ri = importlib.util.module_from_spec(_spec)
sys.modules["release_image_scenarios"] = ri
_spec.loader.exec_module(ri)

PLATFORMS = list(oci.PLATFORMS)
ARM = frozenset({"linux/arm64"})
AMD = frozenset({"linux/amd64"})


class Img:
    def __init__(self, repo: str, tag: str, digest: str) -> None:
        self.repo, self.tag, self.digest = repo, tag, digest


@pytest.fixture(scope="module")
def images(
    oci_registries: oci.Registries, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, Img]:
    work = tmp_path_factory.mktemp("oci-images")
    out: dict[str, Img] = {}
    for name, vuln in {
        "good": frozenset(),
        "good2": frozenset(),
        "good3": frozenset(),
        "armvuln": ARM,
        "amdvuln": AMD,
    }.items():
        digest = oci.build_candidate(
            oci_registries, "candidate", f"cand-{name}", work, vulnerable=vuln
        )
        out[name] = Img("candidate", f"cand-{name}", digest)
    return out


def _dest(host: str) -> str:
    """A fresh production repository on *host*, so tests never see each other's tags."""
    return f"{host}/prod-{uuid.uuid4().hex[:8]}"


def _bare(repo: str) -> str:
    return repo.split("/", 1)[1]


def _reports(
    regs: oci.Registries, tmp: Path, img: Img, vulnerable: frozenset[str] = frozenset()
) -> Path:
    d = tmp / f"reports-{uuid.uuid4().hex[:6]}"
    oci.write_reports(d, FIXTURES, f"{regs.a}/{img.repo}@{img.digest}", vulnerable=vulnerable)
    return d


def _cli(*args: str) -> int:
    return int(ri.main(list(args)))


def _promote(
    regs: oci.Registries, img: Img, reports: Path, dest: str, *extra: str, digest: str | None = None
) -> int:
    plats = [x for p in PLATFORMS for x in ("--platform", p)]
    return _cli(
        "promote",
        "--src-repo",
        f"{regs.a}/{img.repo}",
        "--dest-repo",
        dest,
        "--digest",
        digest or img.digest,
        "--report-dir",
        str(reports),
        *plats,
        *extra,
    )


def _tag(dest: str, digest: str, *tags: str, immutable: str = "") -> int:
    plats = [x for p in PLATFORMS for x in ("--platform", p)]
    args = ["tag", "--repo", dest, "--digest", digest, *plats]
    for t in tags:
        args += ["--tag", t]
    if immutable:
        args += ["--immutable", immutable]
    return _cli(*args)


def _assert_nothing_published(host: str, dest: str, digest: str) -> None:
    repo = _bare(dest)
    assert oci.registry_tags(host, repo) == [], "a production tag exists"
    assert not oci.manifest_exists(host, repo, digest), "the digest was staged"


def _release(
    regs: oci.Registries, img: Img, tmp: Path, dest: str, *tags: str, immutable: str = ""
) -> None:
    """promote then tag, as the workflow does (sign/attest sit between them there)."""
    assert _promote(regs, img, _reports(regs, tmp, img), dest) == 0
    assert _tag(dest, img.digest, *tags, immutable=immutable) == 0


# ------------------------------------------------------------ the happy path, both registries


@pytest.mark.parametrize("registry", ["a", "b"], ids=["ghcr-like", "dockerhub-like"])
def test_a_real_buildx_image_with_sbom_and_provenance_is_promoted_to_both_registries(
    oci_registries: oci.Registries, images: dict[str, Img], tmp_path: Path, registry: str
) -> None:
    host = getattr(oci_registries, registry)
    img, dest = images["good"], _dest(host)
    _release(oci_registries, img, tmp_path, dest, "1.0.0", "1.0", "latest", immutable="1.0.0")

    cand_ref = f"{oci_registries.a}/{img.repo}@{img.digest}"
    assert oci.raw_manifest(f"{dest}@{img.digest}") == oci.raw_manifest(cand_ref), "bytes differ"
    for tag in ("1.0.0", "1.0", "latest"):
        assert oci.tag_digest(host, _bare(dest), tag) == img.digest, tag
    index = json.loads(oci.raw_manifest(f"{dest}@{img.digest}"))
    kinds = [m.get("annotations", {}).get("vnd.docker.reference.type") for m in index["manifests"]]
    assert kinds.count("attestation-manifest") == 2, "SBOM/provenance attestations were dropped"
    platforms = {
        f"{m['platform']['os']}/{m['platform']['architecture']}"
        for m in index["manifests"]
        if "platform" in m and m["platform"]["os"] != "unknown"
    }
    assert platforms == set(PLATFORMS)
    for child in index["manifests"]:
        assert oci.manifest_exists(host, _bare(dest), child["digest"]), child["digest"]


def test_the_tagless_stage_copy_creates_the_digest_and_no_tag(
    oci_registries: oci.Registries, images: dict[str, Img], tmp_path: Path
) -> None:
    """Task d: the first real registry run copies by digest alone; the registry accepts it."""
    img, dest = images["good"], _dest(oci_registries.a)
    assert _promote(oci_registries, img, _reports(oci_registries, tmp_path, img), dest) == 0
    assert oci.manifest_exists(oci_registries.a, _bare(dest), img.digest)
    assert oci.registry_tags(oci_registries.a, _bare(dest)) == []


def test_the_documented_fallback_stage_tag_works_and_is_the_only_tag(
    oci_registries: oci.Registries, images: dict[str, Img], tmp_path: Path
) -> None:
    img, dest = images["good"], _dest(oci_registries.a)
    stage = f"scanned-{img.digest[7:19]}"
    assert (
        _promote(
            oci_registries,
            img,
            _reports(oci_registries, tmp_path, img),
            dest,
            "--stage-tag",
            stage,
        )
        == 0
    )
    assert oci.registry_tags(oci_registries.a, _bare(dest)) == [stage]
    assert oci.tag_digest(oci_registries.a, _bare(dest), stage) == img.digest


# ------------------------------------------------------------------ gate failures publish nothing


@pytest.mark.parametrize("registry", ["a", "b"], ids=["ghcr-like", "dockerhub-like"])
@pytest.mark.parametrize("bad", ["linux/arm64", "linux/amd64"])
def test_one_failing_architecture_publishes_nothing(
    oci_registries: oci.Registries,
    images: dict[str, Img],
    tmp_path: Path,
    registry: str,
    bad: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    host = getattr(oci_registries, registry)
    img, dest = images["good"], _dest(host)
    reports = _reports(oci_registries, tmp_path, img, vulnerable=frozenset({bad}))
    assert _promote(oci_registries, img, reports, dest) == 1
    assert f"{bad}: " in capsys.readouterr().err
    _assert_nothing_published(host, dest, img.digest)


@pytest.mark.parametrize(
    "damage",
    ["missing-arm64", "missing-amd64", "empty-file", "garbage", "wrong-schema"],
)
def test_scan_timeout_crash_or_unknown_verdict_publishes_nothing(
    oci_registries: oci.Registries, images: dict[str, Img], tmp_path: Path, damage: str
) -> None:
    img, dest = images["good"], _dest(oci_registries.a)
    reports = _reports(oci_registries, tmp_path, img)
    name = ri.report_name("linux/amd64" if "amd64" in damage else "linux/arm64")
    target = reports / name
    if damage.startswith("missing"):
        target.unlink()
    elif damage == "empty-file":
        target.write_text("")
    elif damage == "garbage":
        target.write_text("panic: runtime error\n")
    else:
        target.write_text(json.dumps({"SchemaVersion": 1}))
    assert _promote(oci_registries, img, reports, dest) == 1
    _assert_nothing_published(oci_registries.a, dest, img.digest)


def test_an_unscanned_platform_in_the_image_publishes_nothing(
    oci_registries: oci.Registries, images: dict[str, Img], tmp_path: Path
) -> None:
    """The workflow lists platforms by hand; the image's index is the truth.

    Here the caller claims only amd64 was shipped and scanned; the index also holds arm64.
    The script must not copy an index that carries a platform nobody asked to be scanned.
    """
    img, dest = images["good"], _dest(oci_registries.a)
    reports = _reports(oci_registries, tmp_path, img)
    code = _cli(
        "promote",
        "--src-repo",
        f"{oci_registries.a}/{img.repo}",
        "--dest-repo",
        dest,
        "--digest",
        img.digest,
        "--report-dir",
        str(reports),
        "--platform",
        "linux/amd64",
    )
    assert code == 1
    _assert_nothing_published(oci_registries.a, dest, img.digest)


# ------------------------------------------------------------------- candidate digest changes


def test_reports_for_a_different_candidate_digest_publish_nothing(
    oci_registries: oci.Registries,
    images: dict[str, Img],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    img, other, dest = images["good"], images["good2"], _dest(oci_registries.a)
    wrong = _reports(oci_registries, tmp_path, other)  # clean scans, but of ANOTHER digest
    assert _promote(oci_registries, img, wrong, dest) == 1
    assert "different image" in capsys.readouterr().err
    _assert_nothing_published(oci_registries.a, dest, img.digest)


# --------------------------------------------------------------- copy changes the manifests


def _faulty(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``imagetools create`` quietly produce something other than what was asked."""
    real: Callable[..., bytes] = ri._imagetools

    def create(*args: str) -> bytes:
        if args[0] != "create":
            return real(*args)
        target, source = args[args.index("-t") + 1], args[-1]
        repo = target.split("@")[0]
        if kind == "annotated":  # re-wraps the index: same content, different digest
            return real("create", "-t", f"{repo}:rewrapped", "--annotation", "index:note=x", source)
        index = json.loads(oci.raw_manifest(source))  # "dropped-attestations"
        amd64 = next(
            m["digest"]
            for m in index["manifests"]
            if m.get("platform", {}).get("architecture") == "amd64"
        )
        return real("create", "-t", f"{repo}:partial", f"{source.split('@')[0]}@{amd64}")

    monkeypatch.setattr(ri, "_imagetools", create)


@pytest.mark.parametrize("registry", ["a", "b"], ids=["ghcr-like", "dockerhub-like"])
@pytest.mark.parametrize("fault", ["annotated", "dropped-attestations"])
def test_a_cross_registry_copy_that_changes_the_manifests_is_refused(
    oci_registries: oci.Registries,
    images: dict[str, Img],
    tmp_path: Path,
    registry: str,
    fault: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = getattr(oci_registries, registry)
    img, dest = images["good"], _dest(host)
    reports = _reports(oci_registries, tmp_path, img)
    _faulty(fault, monkeypatch)
    assert _promote(oci_registries, img, reports, dest) == 1
    # The faulty copier did write *something*; the scanned digest is not served, and the gate
    # said so, so no tag can ever be moved onto it.
    assert not oci.manifest_exists(host, _bare(dest), img.digest)
    monkeypatch.undo()
    assert _tag(dest, img.digest, "1.0.0", immutable="1.0.0") == 1
    assert oci.tag_digest(host, _bare(dest), "1.0.0") is None


# ----------------------------------------------- existing tags survive a failed gate / reruns


def test_existing_immutable_and_moving_tags_are_untouched_by_a_failed_gate(
    oci_registries: oci.Registries, images: dict[str, Img], tmp_path: Path
) -> None:
    a, dest = oci_registries.a, _dest(oci_registries.a)
    good, bad = images["good"], images["armvuln"]
    _release(oci_registries, good, tmp_path, dest, "1.0.0", "1.0", "latest", immutable="1.0.0")
    before = sorted(oci.registry_tags(a, _bare(dest)))
    assert before == ["1.0", "1.0.0", "latest"]

    reports = _reports(oci_registries, tmp_path, bad, vulnerable=ARM)  # arm64 really is vulnerable
    assert _promote(oci_registries, bad, reports, dest) == 1

    assert sorted(oci.registry_tags(a, _bare(dest))) == before
    for tag in before:
        assert oci.tag_digest(a, _bare(dest), tag) == good.digest, tag
    assert not oci.manifest_exists(a, _bare(dest), bad.digest)


def test_rerunning_the_same_digest_is_idempotent_and_a_passing_release_moves_latest(
    oci_registries: oci.Registries, images: dict[str, Img], tmp_path: Path
) -> None:
    a, dest = oci_registries.a, _dest(oci_registries.a)
    one, two = images["good"], images["good3"]
    _release(oci_registries, one, tmp_path, dest, "1.0.0", "latest", immutable="1.0.0")
    _release(oci_registries, one, tmp_path, dest, "1.0.0", "latest", immutable="1.0.0")  # rerun
    assert oci.tag_digest(a, _bare(dest), "latest") == one.digest
    _release(oci_registries, two, tmp_path, dest, "1.0.1", "latest", immutable="1.0.1")
    assert oci.tag_digest(a, _bare(dest), "latest") == two.digest
    assert oci.tag_digest(a, _bare(dest), "1.0.0") == one.digest


def test_the_immutable_tag_is_bound_before_any_moving_tag(
    oci_registries: oci.Registries,
    images: dict[str, Img],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the second tag write fails, X.Y.Z must already exist and no moving tag may have moved.

    The workflow passes the tags in metadata order (``latest`` and ``X.Y`` before ``X.Y.Z``);
    the script must reorder so the immutable tag is bound first. A half-finished release then
    has its release tag and not a ``latest`` that points at an image without one.
    """
    regs, img = oci_registries, images["good"]
    dest = _dest(regs.a)
    assert _promote(regs, img, _reports(regs, tmp_path, img), dest) == 0
    real: Callable[..., bytes] = ri._imagetools
    creates: list[str] = []

    def second_write_fails(*args: str) -> bytes:
        if args[0] == "create":
            creates.append(args[args.index("-t") + 1])
            if len(creates) == 2:
                raise ri.GateError("exit 1: ERROR: 503 simulated registry failure")
        return real(*args)

    monkeypatch.setattr(ri, "_imagetools", second_write_fails)
    assert _tag(dest, img.digest, "latest", "1.0", "1.0.0", immutable="1.0.0") == 1
    assert creates[0].endswith(":1.0.0"), creates
    assert oci.tag_digest(regs.a, _bare(dest), "1.0.0") == img.digest
    assert oci.tag_digest(regs.a, _bare(dest), "latest") is None
    assert oci.tag_digest(regs.a, _bare(dest), "1.0") is None


# ------------------------------------------------------- GHCR and Docker Hub fail independently


def test_one_registry_failing_does_not_affect_the_other(
    oci_registries: oci.Registries,
    images: dict[str, Img],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    regs, img = oci_registries, images["good"]
    ghcr, hub = _dest(regs.a), _dest(regs.b)
    reports = _reports(regs, tmp_path, img)
    real: Callable[..., bytes] = ri._imagetools

    def hub_is_down(*args: str) -> bytes:
        if args[0] == "create" and regs.b in " ".join(args):
            raise ri.GateError("exit 1: ERROR: 503 Service Unavailable")
        return real(*args)

    assert _promote(regs, img, reports, ghcr) == 0
    assert _tag(ghcr, img.digest, "1.0.0", "latest", immutable="1.0.0") == 0
    monkeypatch.setattr(ri, "_imagetools", hub_is_down)
    assert _promote(regs, img, reports, hub) == 1
    assert "503" in capsys.readouterr().err

    assert oci.tag_digest(regs.a, _bare(ghcr), "1.0.0") == img.digest
    _assert_nothing_published(regs.b, hub, img.digest)


def test_an_unreachable_registry_is_a_refusal_not_an_absent_tag(
    oci_registries: oci.Registries, images: dict[str, Img], tmp_path: Path
) -> None:
    img = images["good"]
    reports = _reports(oci_registries, tmp_path, img)
    assert _promote(oci_registries, img, reports, "127.0.0.1:1/prod") == 1
    assert _tag("127.0.0.1:1/prod", img.digest, "1.0.0", immutable="1.0.0") == 1


# -------------------------------------------------------------------------- the CLI, for real


def test_the_cli_process_exits_1_with_an_error_annotation_and_publishes_nothing(
    oci_registries: oci.Registries, images: dict[str, Img], tmp_path: Path
) -> None:
    img, dest = images["armvuln"], _dest(oci_registries.a)
    reports = _reports(oci_registries, tmp_path, img, vulnerable=ARM)
    argv = [
        sys.executable,
        "-I",
        str(SCRIPT),
        "promote",
        "--src-repo",
        f"{oci_registries.a}/{img.repo}",
        "--dest-repo",
        dest,
        "--digest",
        img.digest,
        "--report-dir",
        str(reports),
        "--platform",
        "linux/amd64",
        "--platform",
        "linux/arm64",
    ]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False)  # noqa: S603
    assert proc.returncode == 1 and "::error" in proc.stderr, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stderr
    _assert_nothing_published(oci_registries.a, dest, img.digest)


# ------------------------------------------------------------- real Trivy

TRIVY_VERSION = "0.76.2"  # the ONE place the tests name the scanner; the pin test below reads it
TRIVY_IMAGE = f"aquasec/trivy:{TRIVY_VERSION}"
TRIVY_TIMEOUT_S = 600  # per platform scan, DB download included


def _trivy_cache() -> Path:
    """A cache that survives runs, so the vulnerability DB is downloaded once, not per test.

    The FIRST run needs network access to fetch the DB (``ghcr.io``). An offline machine must
    set ``NOVA_TEST_TRIVY_CACHE`` to a directory that already holds a DB; without one the
    scan fails (it does not skip). The override is trusted as given.

    The default is ``<tmp>/nf-trivy-cache-<uid>``. A predictable directory in a shared /tmp is
    only reused when it is a real directory owned by this user with mode 0700: anyone else
    could have pre-created it, and a poisoned DB that reports nothing would make every
    scenario pass for the wrong reason.
    """
    override = os.environ.get("NOVA_TEST_TRIVY_CACHE")
    if override:
        path = Path(override)
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        return path
    path = Path(tempfile.gettempdir()) / f"nf-trivy-cache-{os.getuid()}"
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    st = path.lstat()  # lstat: a symlink is refused, never followed
    if (
        not stat.S_ISDIR(st.st_mode)
        or st.st_uid != os.getuid()
        or stat.S_IMODE(st.st_mode) != 0o700
    ):
        pytest.fail(
            f"refusing to reuse the trivy cache {path}: it must be a real directory owned by "
            f"uid {os.getuid()} with mode 0700 (found mode {oct(stat.S_IMODE(st.st_mode))}, "
            f"owner {st.st_uid}). Remove it or set NOVA_TEST_TRIVY_CACHE."
        )
    return path


def _require_trivy_image() -> None:
    """The ONLY reason these tests skip: Docker or the trivy image is not available here.

    A trivy that runs and fails (DB download, scan error, timeout) is a FAILURE of the test,
    never a skip: a skipped scenario would let a broken scanner integration look green.
    """
    from _docker_image import skip_if_image_rate_limited

    skip_if_image_rate_limited(TRIVY_IMAGE)  # skips if the image is absent and not pullable
    if oci.docker("image", "inspect", TRIVY_IMAGE, check=False, timeout=60).returncode != 0:
        pytest.skip(f"{TRIVY_IMAGE} is not available locally and could not be pulled")


def _real_trivy_reports(regs: oci.Registries, img: Img, out: Path, cache: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for platform in PLATFORMS:
        target = out / ri.report_name(platform)
        proc = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "docker",
                "run",
                "--rm",
                "--network",
                "host",
                # run as the invoking user: root-owned files would make pytest's own tmp-dir
                # cleanup fail with PermissionError
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "-e",
                "TRIVY_CACHE_DIR=/cache",
                "-v",
                f"{cache}:/cache",
                "-v",
                f"{out}:/out",
                "-e",
                f"TRIVY_PLATFORM={platform}",
                TRIVY_IMAGE,
                "image",
                "--insecure",
                "--scanners",
                "vuln",
                "--format",
                "json",
                "--quiet",
                "--timeout",
                f"{TRIVY_TIMEOUT_S - 30}s",
                "-o",
                f"/out/{target.name}",
                f"{regs.a}/{img.repo}@{img.digest}",
            ],
            capture_output=True,
            text=True,
            timeout=TRIVY_TIMEOUT_S,  # TimeoutExpired fails the test, it does not skip it
            check=False,
        )
        assert proc.returncode == 0 and target.exists(), (
            f"trivy failed on {platform} (exit {proc.returncode}): {proc.stderr[-500:]}"
        )


def test_with_real_trivy_a_vulnerable_arm64_leg_blocks_and_a_clean_image_promotes(
    oci_registries: oci.Registries,
    images: dict[str, Img],
    tmp_path_factory: pytest.TempPathFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Real trivy 0.76.2 reports (TRIVY_PLATFORM per platform) through the real script.

    Skips only when Docker or the trivy image is unavailable; a trivy error FAILS. The DB
    cache persists between runs (see ``_trivy_cache``) and each scan is time-bounded.
    """
    _require_trivy_image()
    cache = _trivy_cache()
    work = tmp_path_factory.mktemp("trivy-reports")

    bad, good = images["armvuln"], images["good"]
    bad_reports, good_reports = work / "bad", work / "good"
    _real_trivy_reports(oci_registries, bad, bad_reports, cache)
    _real_trivy_reports(oci_registries, good, good_reports, cache)

    ri.check_report(bad_reports / ri.report_name("linux/amd64"), bad.digest, "linux/amd64")
    dest = _dest(oci_registries.a)
    assert _promote(oci_registries, bad, bad_reports, dest) == 1
    assert "CVE-2019-14234" in capsys.readouterr().err
    _assert_nothing_published(oci_registries.a, dest, bad.digest)

    assert _promote(oci_registries, good, good_reports, dest) == 0
    assert _tag(dest, good.digest, "2.0.1", immutable="2.0.1") == 0
    assert oci.tag_digest(oci_registries.a, _bare(dest), "2.0.1") == good.digest


def test_real_trivy_with_the_platform_flag_ignored_is_caught_by_the_evidence_check(
    oci_registries: oci.Registries,
    images: dict[str, Img],
    tmp_path_factory: pytest.TempPathFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Simulate TRIVY_PLATFORM never reaching trivy: both reports come from the host platform.

    This is the workflow's one unproven link (trivy-action forwarding the env var). The
    arm64-vulnerable image then yields a clean-looking arm64 report that is really amd64;
    the architecture check must refuse it instead of promoting.

    Skips only when Docker or the trivy image is unavailable; a trivy error FAILS.
    """
    _require_trivy_image()
    cache = _trivy_cache()
    work = tmp_path_factory.mktemp("trivy-reports-noplat")
    bad = images["armvuln"]
    _real_trivy_reports(oci_registries, bad, work, cache)  # real arm64 report (vulnerable)
    amd64 = work / ri.report_name("linux/amd64")
    (work / ri.report_name("linux/arm64")).write_bytes(amd64.read_bytes())  # the false pass
    dest = _dest(oci_registries.a)
    assert _promote(oci_registries, bad, work, dest) == 1
    assert "architecture" in capsys.readouterr().err
    _assert_nothing_published(oci_registries.a, dest, bad.digest)


# ------------------------------------------------- the scanner pin and the trivy cache (no Docker needed)


def test_the_scan_composite_pins_the_trivy_the_real_trivy_tests_run() -> None:
    """Deleting the pin (the action then installs its own default) or bumping it without the
    tests must fail: the gate and the tests have to scan with the same trivy."""
    import yaml

    composite = yaml.safe_load(
        (REPO / ".github" / "actions" / "scan-candidate" / "action.yml").read_text(encoding="utf-8")
    )
    pins = [
        str(step["with"]["version"])
        for step in composite["runs"]["steps"]
        if str(step.get("uses", "")).startswith("aquasecurity/trivy-action")
        and "version" in (step.get("with") or {})
    ]
    assert pins == [f"v{TRIVY_VERSION}"], (
        f"trivy pin {pins} != v{TRIVY_VERSION}: the first trivy step installs trivy for all later "
        "steps, so it must carry exactly one pin, equal to the version the real-trivy tests use"
    )


def test_a_pre_existing_trivy_cache_that_is_not_private_to_me_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A world-writable or foreign /tmp dir could carry a poisoned vulnerability DB (a DB that
    reports nothing makes every scenario pass for the wrong reason). Fail, never reuse it."""
    monkeypatch.delenv("NOVA_TEST_TRIVY_CACHE", raising=False)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    cache = tmp_path / f"nf-trivy-cache-{os.getuid()}"
    cache.mkdir(mode=0o755)
    cache.chmod(0o755)
    with pytest.raises(pytest.fail.Exception, match="trivy cache"):
        _trivy_cache()
    cache.chmod(0o700)
    assert _trivy_cache() == cache  # mine and private: reused (that is the point of the cache)


def test_a_symlinked_trivy_cache_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOVA_TEST_TRIVY_CACHE", raising=False)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (tmp_path / f"nf-trivy-cache-{os.getuid()}").symlink_to(elsewhere)
    with pytest.raises(pytest.fail.Exception, match="trivy cache"):
        _trivy_cache()


def test_a_new_trivy_cache_is_created_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOVA_TEST_TRIVY_CACHE", raising=False)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    old = os.umask(0)  # even a permissive umask must not widen it
    try:
        cache = _trivy_cache()
    finally:
        os.umask(old)
    assert cache.stat().st_mode & 0o777 == 0o700 and cache.stat().st_uid == os.getuid()


def test_the_trivy_cache_override_is_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_TEST_TRIVY_CACHE", str(tmp_path / "mine"))
    assert _trivy_cache() == tmp_path / "mine"
