"""Air-gap closure bundle + standard-library offline verifier (ADR-0249, #234).

What this pins:

* ``scripts/verify_airgap_bundle.py`` runs with the standard library alone
  (it ships inside the bundle and runs before anything is installed) and
  fails closed, naming the member, on: a tampered, missing, extra, duplicated
  or non-regular member, an unsafe path, a wrong key, a ``SHA256SUMS`` that
  disagrees with the signed manifest, and a host with no usable verifier.
* ``--extract-to`` leaves nothing behind on failure and only verified bytes
  on success.
* the verifier stays byte-compatible with ``novafabric.export_blob.airgap``.
* ``scripts/build_airgap_bundle.py --closure-wheel`` assembles wheel +
  wheelhouse + hashed requirements + bundle-info + SHA256SUMS + verifier,
  exercised here with stub ``uv``/``pip`` executables (no network).
* the signing key can never become a bundle member.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path
from types import ModuleType

import pytest

from novafabric.evidence.intoto import dsse_pae
from novafabric.evidence.signing import generate_keypair
from novafabric.export_blob import airgap
from novafabric.export_blob.airgap import AirgapBundleError, build_bundle

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIER = REPO_ROOT / "scripts" / "verify_airgap_bundle.py"
BUILDER = REPO_ROOT / "scripts" / "build_airgap_bundle.py"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


verifier = _load("airgap_verify_script", VERIFIER)
builder = _load("airgap_build_script", BUILDER)


def _run_verifier(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(VERIFIER), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


@pytest.fixture()
def keys(tmp_path: Path) -> tuple[Path, Path]:
    return generate_keypair(tmp_path / "keys")


def _bundle(tmp_path: Path, priv: Path, files: dict[str, bytes], name: str = "b.tar") -> Path:
    src = tmp_path / f"src-{name}"
    members: dict[str, Path] = {}
    for i, (arc, data) in enumerate(files.items()):
        p = src / f"f{i}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        members[arc] = p
    return build_bundle(
        tmp_path / name, members, signing_key=priv, nova_version="0.0.0-test", created_at=1.0
    )


def _rewrite(src: Path, dst: Path, *, edit=None, drop=(), add=()) -> Path:
    """Copy tar *src* to *dst*, editing member bytes, dropping names, appending entries."""
    with tarfile.open(src) as s, tarfile.open(dst, "w") as d:
        for info in s.getmembers():
            if info.name in drop:
                continue
            data = s.extractfile(info).read()  # type: ignore[union-attr]
            if edit is not None:
                data = edit(info.name, data)
            info.size = len(data)
            d.addfile(info, io.BytesIO(data))
        for info, data in add:
            d.addfile(info, io.BytesIO(data) if data is not None else None)
    return dst


FILES = {"wheels/a-1.0-py3-none-any.whl": b"wheel-a", "docs/readme.txt": b"hello"}


# ── the stdlib verifier, as a subprocess (how an operator runs it) ──────────


def test_round_trip_with_extract(tmp_path: Path, keys: tuple[Path, Path]) -> None:
    priv, pub = keys
    bundle = _bundle(tmp_path, priv, FILES)
    out = tmp_path / "out"
    r = _run_verifier("--bundle", str(bundle), "--public-key", str(pub), "--extract-to", str(out))
    assert r.returncode == 0, r.stderr
    assert "signature valid, 2 members verified" in r.stdout
    for arc, data in FILES.items():
        assert (out / arc).read_bytes() == data
    # Only listed members are extracted; the manifest/envelope stay in the tar.
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()) == sorted(
        FILES
    )


def test_tampered_member_fails_and_extracts_nothing(
    tmp_path: Path, keys: tuple[Path, Path]
) -> None:
    priv, pub = keys
    bundle = _bundle(tmp_path, priv, FILES)
    bad = _rewrite(
        bundle,
        tmp_path / "bad.tar",
        edit=lambda n, d: b"EVIL-a!" if n == "wheels/a-1.0-py3-none-any.whl" else d,
    )
    out = tmp_path / "out"
    r = _run_verifier("--bundle", str(bad), "--public-key", str(pub), "--extract-to", str(out))
    assert r.returncode == 1
    assert "wheels/a-1.0-py3-none-any.whl" in r.stderr and "hash mismatch" in r.stderr
    assert not out.exists()
    assert not list(tmp_path.glob(".airgap-extract-*")), "staging dir left behind"


def test_stowaway_member_fails(tmp_path: Path, keys: tuple[Path, Path]) -> None:
    priv, pub = keys
    bundle = _bundle(tmp_path, priv, FILES)
    stow = tarfile.TarInfo("wheels/evil-6.6-py3-none-any.whl")
    stow.size = 4
    bad = _rewrite(bundle, tmp_path / "bad.tar", add=[(stow, b"boo!")])
    r = _run_verifier("--bundle", str(bad), "--public-key", str(pub))
    assert r.returncode == 1
    assert "evil-6.6" in r.stderr and "not in the signed manifest" in r.stderr


def test_missing_member_fails(tmp_path: Path, keys: tuple[Path, Path]) -> None:
    priv, pub = keys
    bundle = _bundle(tmp_path, priv, FILES)
    bad = _rewrite(bundle, tmp_path / "bad.tar", drop={"docs/readme.txt"})
    r = _run_verifier("--bundle", str(bad), "--public-key", str(pub))
    assert r.returncode == 1
    assert "'docs/readme.txt' is in the manifest but missing" in r.stderr


def test_wrong_key_fails_signature(tmp_path: Path, keys: tuple[Path, Path]) -> None:
    priv, _ = keys
    _, other_pub = generate_keypair(tmp_path / "other")
    bundle = _bundle(tmp_path, priv, FILES)
    r = _run_verifier("--bundle", str(bundle), "--public-key", str(other_pub))
    assert r.returncode == 1
    assert "signature invalid" in r.stderr


def test_resigned_manifest_with_own_key_is_refused(tmp_path: Path, keys: tuple[Path, Path]) -> None:
    """An attacker who swaps a member and re-signs with their own key still fails:
    the key that counts is the one the operator trusts, never one in the bundle."""
    _, pub = keys
    attacker_priv, _ = generate_keypair(tmp_path / "attacker")
    forged = _bundle(tmp_path, attacker_priv, FILES, name="forged.tar")
    r = _run_verifier("--bundle", str(forged), "--public-key", str(pub))
    assert r.returncode == 1
    assert "signature invalid" in r.stderr


def test_integrity_only_warns_and_still_checks_hashes(
    tmp_path: Path, keys: tuple[Path, Path]
) -> None:
    priv, _ = keys
    bundle = _bundle(tmp_path, priv, FILES)
    ok = _run_verifier("--bundle", str(bundle), "--integrity-only")
    assert ok.returncode == 0
    assert "signature NOT checked" in ok.stdout and "WARNING" in ok.stderr
    bad = _rewrite(
        bundle, tmp_path / "bad.tar", edit=lambda n, d: d + b"x" if n.endswith(".txt") else d
    )
    r = _run_verifier("--bundle", str(bad), "--integrity-only")
    assert r.returncode == 1


def test_key_or_integrity_only_is_required(tmp_path: Path, keys: tuple[Path, Path]) -> None:
    priv, pub = keys
    bundle = _bundle(tmp_path, priv, FILES)
    assert _run_verifier("--bundle", str(bundle)).returncode == 2
    both = _run_verifier("--bundle", str(bundle), "--public-key", str(pub), "--integrity-only")
    assert both.returncode == 2


def test_extract_to_existing_dir_is_refused(tmp_path: Path, keys: tuple[Path, Path]) -> None:
    priv, pub = keys
    bundle = _bundle(tmp_path, priv, FILES)
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("mine")
    r = _run_verifier("--bundle", str(bundle), "--public-key", str(pub), "--extract-to", str(out))
    assert r.returncode == 2
    assert (out / "keep.txt").read_text() == "mine"


# ── the verifier in-process (finer-grained failure cases) ────────────────────


def test_duplicate_tar_entry_is_a_finding(tmp_path: Path, keys: tuple[Path, Path]) -> None:
    """tarfile resolves a duplicated name to the LAST entry; a shadow copy must
    not be able to hide behind the verified one."""
    priv, pub = keys
    bundle = _bundle(tmp_path, priv, FILES)
    dup = tarfile.TarInfo("docs/readme.txt")
    dup.size = 5
    bad = _rewrite(bundle, tmp_path / "bad.tar", add=[(dup, b"EVIL!")])
    report = verifier.verify_bundle(bad, public_key_pem=pub.read_bytes())
    assert not report.ok
    assert any("appears more than once" in e for e in report.errors)


def test_symlink_entry_is_refused(tmp_path: Path, keys: tuple[Path, Path]) -> None:
    priv, pub = keys
    bundle = _bundle(tmp_path, priv, FILES)
    link = tarfile.TarInfo("wheels/link.whl")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    bad = _rewrite(bundle, tmp_path / "bad.tar", add=[(link, None)])
    report = verifier.verify_bundle(bad, public_key_pem=pub.read_bytes())
    assert not report.ok
    assert any("wheels/link.whl" in e and "not a regular file" in e for e in report.errors)


@pytest.mark.parametrize("name", ["../escape.txt", "a/../../escape.txt", "./x"])
def test_unsafe_manifest_path_is_refused(
    tmp_path: Path, keys: tuple[Path, Path], name: str
) -> None:
    priv, pub = keys
    bundle = _bundle(tmp_path, priv, {name: b"x"})
    out = tmp_path / "out"
    report = verifier.verify_bundle(bundle, public_key_pem=pub.read_bytes(), extract_to=out)
    assert not report.ok
    assert any("unsafe path" in e for e in report.errors)
    assert not out.exists()
    assert not (tmp_path / "escape.txt").exists()


def test_unsafe_name_reason() -> None:
    assert verifier.unsafe_name_reason("wheels/a.whl") is None
    for bad in ("", "/etc/passwd", "../x", "a//b", "a\\b", "a\x00b", "a/./b"):
        assert verifier.unsafe_name_reason(bad), bad


def test_sha256sums_must_agree_with_manifest(tmp_path: Path, keys: tuple[Path, Path]) -> None:
    priv, pub = keys
    good_sums = "".join(f"{hashlib.sha256(d).hexdigest()}  {a}\n" for a, d in sorted(FILES.items()))
    ok = _bundle(tmp_path, priv, {**FILES, "SHA256SUMS": good_sums.encode()}, name="ok.tar")
    assert verifier.verify_bundle(ok, public_key_pem=pub.read_bytes()).ok

    lying = good_sums.replace(hashlib.sha256(b"hello").hexdigest(), "0" * 64)
    bad = _bundle(tmp_path, priv, {**FILES, "SHA256SUMS": lying.encode()}, name="bad.tar")
    report = verifier.verify_bundle(bad, public_key_pem=pub.read_bytes())
    assert not report.ok
    assert any("disagrees" in e and "docs/readme.txt" in e for e in report.errors)

    short = _bundle(
        tmp_path, priv, {**FILES, "SHA256SUMS": good_sums.splitlines()[0].encode()}, name="s.tar"
    )
    assert not verifier.verify_bundle(short, public_key_pem=pub.read_bytes()).ok


def test_parse_sha256sums_formats() -> None:
    h = "a" * 64
    entries, errors = verifier.parse_sha256sums(f"{h}  x/y.whl\n{h} *bin.dat\n\nnot a line\n")
    assert entries == {"x/y.whl": h, "bin.dat": h}
    assert len(errors) == 1


def test_no_usable_signature_backend_fails_closed(
    tmp_path: Path, keys: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    priv, pub = keys
    bundle = _bundle(tmp_path, priv, FILES)
    monkeypatch.setattr(verifier.shutil, "which", lambda _name: None)
    report = verifier.verify_bundle(bundle, public_key_pem=pub.read_bytes(), backend="openssl")
    assert not report.ok
    assert any("cannot check the signature" in e for e in report.errors)
    assert report.members_verified == 0


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI not installed")
def test_openssl_backend_agrees_with_cryptography(tmp_path: Path, keys: tuple[Path, Path]) -> None:
    priv, pub = keys
    _, other = generate_keypair(tmp_path / "other")
    bundle = _bundle(tmp_path, priv, FILES)
    try:
        good = verifier.verify_bundle(bundle, public_key_pem=pub.read_bytes(), backend="openssl")
    except verifier.SignatureUnavailable:  # pragma: no cover — OpenSSL < 3 has no -rawin
        pytest.skip("openssl lacks pkeyutl -rawin")
    if not good.ok and any("cannot check" in e for e in good.errors):
        pytest.skip("openssl lacks pkeyutl -rawin")
    assert good.ok and good.signature_checked, good.errors
    bad = verifier.verify_bundle(bundle, public_key_pem=other.read_bytes(), backend="openssl")
    assert not bad.ok
    assert any("signature invalid" in e for e in bad.errors)


def test_verifier_agrees_with_library_format() -> None:
    assert verifier.MANIFEST_NAME == airgap.MANIFEST_NAME
    assert verifier.ENVELOPE_NAME == airgap.ENVELOPE_NAME
    assert verifier.PAYLOAD_TYPE == airgap.PAYLOAD_TYPE
    payload = b'{"kind": "x"}\n'
    assert verifier.dsse_pae(airgap.PAYLOAD_TYPE, payload) == dsse_pae(airgap.PAYLOAD_TYPE, payload)


def test_verifier_imports_only_the_standard_library() -> None:
    """It runs before anything is installed: a top-level third-party import would
    make it unusable on exactly the host it exists for. ``cryptography`` is an
    optional, function-local fast path with an ``openssl`` fallback."""
    tree = ast.parse(VERIFIER.read_text(encoding="utf-8"))
    top_level: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.add(node.module.split(".")[0])
    assert top_level - {"__future__"} <= set(sys.stdlib_module_names), top_level
    everywhere: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            everywhere.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            everywhere.add(node.module.split(".")[0])
    assert everywhere - set(sys.stdlib_module_names) - {"__future__"} <= {"cryptography"}


# ── library: the signing key never enters the bundle ───────────────────────


def test_signing_key_member_is_refused(tmp_path: Path, keys: tuple[Path, Path]) -> None:
    priv, _ = keys
    with pytest.raises(AirgapBundleError, match="signing key"):
        build_bundle(tmp_path / "x.tar", {"key.pem": priv}, signing_key=priv, nova_version="0")
    copy = tmp_path / "innocent.txt"
    copy.write_bytes(priv.read_bytes())
    with pytest.raises(AirgapBundleError, match="signing key"):
        build_bundle(tmp_path / "y.tar", {"innocent.txt": copy}, signing_key=priv, nova_version="0")
    assert not (tmp_path / "x.tar").exists() and not (tmp_path / "y.tar").exists()


def test_public_key_member_is_allowed(tmp_path: Path, keys: tuple[Path, Path]) -> None:
    priv, pub = keys
    out = build_bundle(tmp_path / "x.tar", {"k.pub.pem": pub}, signing_key=priv, nova_version="0")
    assert out.is_file()


# ── builder helpers ──────────────────────────────────────────────────────────


def test_parse_wheel_filename() -> None:
    assert builder.parse_wheel_filename(Path("novafabric-0.110.0-py3-none-any.whl")) == (
        "novafabric",
        "0.110.0",
    )
    for bad in ("novafabric-0.110.0.tar.gz", "novafabric.whl"):
        with pytest.raises(builder.ClosureError):
            builder.parse_wheel_filename(Path(bad))


def test_parse_extras() -> None:
    declared = {"server", "serve", "query"}
    assert builder.parse_extras("", declared) == []
    assert builder.parse_extras(" serve, server,serve ", declared) == ["serve", "server"]
    with pytest.raises(builder.ClosureError, match="unknown extra 'nope'"):
        builder.parse_extras("server,nope", declared)
    with pytest.raises(builder.ClosureError, match="invalid extra"):
        builder.parse_extras("a b", declared | {"a b"})


def test_declared_extras_reads_this_repo() -> None:
    extras = builder.declared_extras(REPO_ROOT / "pyproject.toml")
    assert {"server", "serve"} <= extras


def test_project_requirement_pins_the_wheel_hash() -> None:
    line = builder.project_requirement("1.2.3", ["serve", "server"], "f" * 64)
    assert line == f"novafabric[serve,server]==1.2.3 \\\n    --hash=sha256:{'f' * 64}\n"
    assert builder.project_requirement("1.2.3", [], "0" * 64).startswith("novafabric==1.2.3 ")


def test_sha256sums_round_trip(tmp_path: Path) -> None:
    a = tmp_path / "a"
    a.write_bytes(b"1")
    b = tmp_path / "b"
    b.write_bytes(b"2")
    text = builder.render_sha256sums({"z/b": b, "a": a})
    entries, errors = verifier.parse_sha256sums(text)
    assert not errors
    assert list(entries) == ["a", "z/b"]
    assert entries["a"] == hashlib.sha256(b"1").hexdigest()


def test_closure_flags_need_closure_wheel(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        builder.main(["--out", str(tmp_path / "x.tar"), "--signing-key", "k", "--extras", "server"])
    assert exc.value.code == 2


# ── closure assembly end to end, with stub uv / pip (no network) ────────────

_DEP_HASH = hashlib.sha256(b"dep-a wheel").hexdigest()


def _stub_tools(tmp_path: Path, *, hashes: bool = True) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    hash_line = f" \\\n    --hash=sha256:{_DEP_HASH}" if hashes else ""
    requirement = "dep-a==1.0" + hash_line
    uv.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            f"""
            import sys
            if sys.argv[1:] == ["--version"]:
                print("uv 0.0.0-stub")
            elif sys.argv[1] == "export":
                print({requirement!r})
                print("    # via novafabric")
            else:
                sys.exit(3)
            """
        )
    )
    py = bin_dir / "python-with-pip"
    py.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            """
            import json, os, pathlib, sys
            a = sys.argv[1:]
            if a[:1] == ["-c"]:
                print(json.dumps({"python_version": "3.12", "platform": "linux-x86_64",
                                  "implementation": "cpython"}))
            elif a[:3] == ["-m", "pip", "--version"]:
                print("pip 99.0 from /stub (python 3.12)")
            elif a[:3] == ["-m", "pip", "download"]:
                assert "--require-hashes" in a and "--only-binary=:all:" in a, a
                dest = pathlib.Path(a[a.index("--dest") + 1])
                (dest / "dep_a-1.0-py3-none-any.whl").write_bytes(b"dep-a wheel")
                if os.environ.get("STUB_PIP_SDIST"):
                    (dest / "dep_b-1.0.tar.gz").write_bytes(b"sdist")
            else:
                sys.exit(3)
            """
        )
    )
    for p in (uv, py):
        p.chmod(0o755)
    return uv, py


def _project(tmp_path: Path) -> tuple[Path, Path]:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text(
        '[project]\nname = "novafabric"\nversion = "9.9.9"\n'
        '[project.optional-dependencies]\nserver = ["dep-a"]\n'
    )
    (proj / "uv.lock").write_text("version = 1\n")
    wheel = tmp_path / "dist" / "novafabric-9.9.9-py3-none-any.whl"
    wheel.parent.mkdir()
    wheel.write_bytes(b"the novafabric wheel")
    return proj, wheel


def test_closure_bundle_builds_and_verifies(tmp_path: Path, keys: tuple[Path, Path]) -> None:
    priv, pub = keys
    uv, py = _stub_tools(tmp_path)
    proj, wheel = _project(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    members, version = builder.build_closure(
        wheel=wheel,
        extras_spec="server",
        project_dir=proj,
        pip_python=str(py),
        python_version=None,
        platforms=[],
        staging=staging,
        uv=str(uv),
    )
    assert version == "9.9.9"
    assert set(members) == {
        "wheels/dep_a-1.0-py3-none-any.whl",
        "wheels/novafabric-9.9.9-py3-none-any.whl",
        "requirements.txt",
        "bundle-info.json",
        "INSTALL.txt",
        "verify_airgap_bundle.py",
        "SHA256SUMS",
    }
    assert "deps.txt" not in members
    bundle = build_bundle(tmp_path / "closure.tar", members, signing_key=priv, nova_version=version)

    out = tmp_path / "out"
    r = _run_verifier("--bundle", str(bundle), "--public-key", str(pub), "--extract-to", str(out))
    assert r.returncode == 0, r.stderr
    assert "--require-hashes" in r.stdout  # the install hint for a closure bundle

    reqs = (out / "requirements.txt").read_text()
    assert f"--hash=sha256:{_DEP_HASH}" in reqs
    wheel_sha = hashlib.sha256(b"the novafabric wheel").hexdigest()
    assert f"novafabric[server]==9.9.9 \\\n    --hash=sha256:{wheel_sha}" in reqs

    info = json.loads((out / "bundle-info.json").read_text())
    assert info["schema"] == builder.INFO_SCHEMA
    assert info["nova_version"] == "9.9.9" and info["extras"] == ["server"]
    assert info["target"] == {
        "python_version": "3.12",
        "platform_tags": [],
        "build_host_platform": "linux-x86_64",
        "implementation": "cpython",
        "built_for_build_host": True,
    }
    assert info["tools"] == {"uv": "uv 0.0.0-stub", "pip": "99.0"}
    assert info["source"]["uv_lock_sha256"] == hashlib.sha256(b"version = 1\n").hexdigest()
    assert info["wheel_count"] == 2
    assert (out / "verify_airgap_bundle.py").read_bytes() == VERIFIER.read_bytes()

    # coreutils-style check of the extracted tree agrees too
    sums, errors = verifier.parse_sha256sums((out / "SHA256SUMS").read_text())
    assert not errors
    for arc, digest in sums.items():
        assert hashlib.sha256((out / arc).read_bytes()).hexdigest() == digest


def test_closure_refuses_sdists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    uv, py = _stub_tools(tmp_path)
    proj, wheel = _project(tmp_path)
    monkeypatch.setenv("STUB_PIP_SDIST", "1")
    staging = tmp_path / "staging"
    staging.mkdir()
    with pytest.raises(builder.ClosureError, match="non-wheel"):
        builder.build_closure(
            wheel=wheel, extras_spec="", project_dir=proj, pip_python=str(py),
            python_version=None, platforms=[], staging=staging, uv=str(uv),
        )  # fmt: skip


def test_closure_refuses_an_unhashed_export(tmp_path: Path) -> None:
    uv, py = _stub_tools(tmp_path, hashes=False)
    proj, wheel = _project(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    with pytest.raises(builder.ClosureError, match="no hashes"):
        builder.build_closure(
            wheel=wheel, extras_spec="", project_dir=proj, pip_python=str(py),
            python_version=None, platforms=[], staging=staging, uv=str(uv),
        )  # fmt: skip


def test_closure_refuses_unknown_extra_and_foreign_wheel(tmp_path: Path) -> None:
    uv, py = _stub_tools(tmp_path)
    proj, wheel = _project(tmp_path)
    kwargs = {
        "project_dir": proj,
        "pip_python": str(py),
        "python_version": None,
        "platforms": [],
        "staging": tmp_path,
        "uv": str(uv),
    }
    with pytest.raises(builder.ClosureError, match="unknown extra"):
        builder.build_closure(wheel=wheel, extras_spec="nope", **kwargs)
    other = tmp_path / "dist" / "requests-2.0-py3-none-any.whl"
    other.write_bytes(b"x")
    with pytest.raises(builder.ClosureError, match="must be a novafabric wheel"):
        builder.build_closure(wheel=other, extras_spec="", **kwargs)


def test_closure_without_pip_names_the_fix(tmp_path: Path) -> None:
    uv, _ = _stub_tools(tmp_path)
    proj, wheel = _project(tmp_path)
    no_pip = tmp_path / "bin" / "python-no-pip"
    no_pip.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(1)\n")
    no_pip.chmod(0o755)
    with pytest.raises(builder.ClosureError, match="--pip-python"):
        builder.build_closure(
            wheel=wheel, extras_spec="", project_dir=proj, pip_python=str(no_pip),
            python_version=None, platforms=[], staging=tmp_path, uv=str(uv),
        )  # fmt: skip


def test_source_date_epoch_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    assert builder._created_at() == 1700000000.0
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "yesterday")
    with pytest.raises(builder.ClosureError):
        builder._created_at()
    monkeypatch.delenv("SOURCE_DATE_EPOCH")
    assert builder._created_at() is None


def test_scripts_are_executable() -> None:
    for script in (VERIFIER, BUILDER):
        assert os.access(script, os.X_OK), script
