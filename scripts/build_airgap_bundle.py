#!/usr/bin/env python3
"""Build a signed air-gap bundle (ADR-0249).

Two modes, one signed format (``novafabric.export_blob.airgap``: a tar whose
members are inventoried in a DSSE-signed ``airgap-manifest.json``):

* **members** (slice 1): bundle exactly the files you name with ``--member``.
* **closure** (``--closure-wheel``): on a connected machine, bundle a built
  NovaFabric wheel together with a wheelhouse of every dependency pinned in
  ``uv.lock`` for the extras you choose, so an isolated host can install it
  with ``pip install --no-index``. The bundle then also carries:

  - ``wheels/``: the NovaFabric wheel and every dependency wheel. Binary
    wheels only (``--only-binary=:all:``); ``pip download --require-hashes``
    checks each download against the hash recorded in ``uv.lock``.
  - ``requirements.txt``: the hashed pins, including the NovaFabric wheel's own
    hash, for ``pip install --require-hashes``.
  - ``bundle-info.json``: machine-readable facts: NovaFabric version, extras,
    target Python/platform, source commit, ``uv.lock`` digest, tool versions.
    No host name, user name or path is recorded.
  - ``SHA256SUMS``: ``sha256sum -c`` format, for a check with coreutils alone.
  - ``verify_airgap_bundle.py``: the standard-library offline verifier.
  - ``INSTALL.txt``: the three offline commands.

The bundle is always signed (ed25519, ``--signing-key``); a bundle that
cannot be verified offline defeats its purpose. The signing key is refused as
a member, so it can never travel with the bundle.

Requirements on the connected build machine: ``uv`` on PATH (for
``uv export``) and a Python with ``pip`` (``--pip-python``, default: the
interpreter running this script). By default the wheelhouse targets that
Python's version and platform; ``--python-version`` / ``--platform`` are passed
to ``pip download`` as-is (see docs/ops/air-gapped-install.md for the limits).

Usage:
    uv build --wheel
    uv run python scripts/build_airgap_bundle.py \\
        --out dist/novafabric-airgap-core.tar \\
        --signing-key ~/.novafabric/keys/airgap.pem \\
        --closure-wheel dist/novafabric-0.110.0-py3-none-any.whl \\
        --extras server,query --pip-python /usr/bin/python3.12

    # slice-1 form, still supported:
    uv run python scripts/build_airgap_bundle.py \\
        --out dist/novafabric-airgap.tar \\
        --signing-key ~/.novafabric/keys/evidence.key \\
        --member wheels/novafabric.whl=dist/novafabric-0.100.1-py3-none-any.whl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
VERIFIER = Path(__file__).resolve().with_name("verify_airgap_bundle.py")

INFO_SCHEMA = "novafabric-airgap-bundle-info/0.1"
PROJECT_NAME = "novafabric"
WHEELS_DIR = "wheels"
REQUIREMENTS_NAME = "requirements.txt"
INFO_NAME = "bundle-info.json"
SUMS_NAME = "SHA256SUMS"
VERIFIER_NAME = "verify_airgap_bundle.py"
INSTALL_NAME = "INSTALL.txt"
CLOSURE_NAMES = frozenset({REQUIREMENTS_NAME, INFO_NAME, SUMS_NAME, VERIFIER_NAME, INSTALL_NAME})

_EXTRA = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class ClosureError(Exception):
    """Assembling the dependency closure failed; the message says where."""


# ── pure helpers (unit-tested without network) ─────────────────────────────


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_wheel_filename(wheel: Path) -> tuple[str, str]:
    """``novafabric-0.110.0-py3-none-any.whl`` -> ("novafabric", "0.110.0")."""
    if wheel.suffix != ".whl":
        raise ClosureError(f"{wheel.name} is not a wheel (.whl)")
    parts = wheel.name[: -len(".whl")].split("-")
    if len(parts) not in (5, 6):
        raise ClosureError(f"{wheel.name} is not a valid wheel file name")
    return parts[0].lower().replace("_", "-"), parts[1]


def parse_extras(spec: str, declared: set[str]) -> list[str]:
    """``"server, serve"`` -> sorted unique names; unknown extras are refused."""
    extras = sorted({e.strip().lower() for e in spec.split(",") if e.strip()})
    for extra in extras:
        if not _EXTRA.match(extra):
            raise ClosureError(f"invalid extra name {extra!r}")
        if extra not in declared:
            raise ClosureError(
                f"unknown extra {extra!r}; pyproject.toml declares: {', '.join(sorted(declared))}"
            )
    return extras


def declared_extras(pyproject: Path) -> set[str]:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return set(data.get("project", {}).get("optional-dependencies", {}))


def project_requirement(version: str, extras: list[str], wheel_sha256: str) -> str:
    """The hashed requirement line that pins the bundled NovaFabric wheel itself."""
    name = PROJECT_NAME + (f"[{','.join(extras)}]" if extras else "")
    return f"{name}=={version} \\\n    --hash=sha256:{wheel_sha256}\n"


def render_sha256sums(files: dict[str, Path]) -> str:
    """``sha256sum``-compatible lines, sorted by in-bundle path."""
    return "".join(f"{sha256_file(src)}  {arc}\n" for arc, src in sorted(files.items()))


def bundle_info(
    *,
    nova_version: str,
    wheel_name: str,
    extras: list[str],
    target: dict[str, Any],
    source_commit: str | None,
    source_dirty: bool | None,
    lock_sha256: str,
    tools: dict[str, str],
    wheel_count: int,
) -> dict[str, Any]:
    return {
        "schema": INFO_SCHEMA,
        "nova_version": nova_version,
        "wheel": wheel_name,
        "extras": extras,
        "target": target,
        "source": {
            "commit": source_commit,
            "dirty": source_dirty,
            "uv_lock_sha256": lock_sha256,
        },
        "tools": tools,
        "wheel_count": wheel_count,
        "requirements": REQUIREMENTS_NAME,
        "verifier": VERIFIER_NAME,
    }


def install_text(extras: list[str]) -> str:
    target = PROJECT_NAME + (f"[{','.join(extras)}]" if extras else "")
    return (
        f"NovaFabric air-gap bundle ({target})\n\n"
        "1. Verify with the public key you received out of band (never one\n"
        "   shipped inside this bundle), extracting only verified bytes:\n\n"
        "   tar -xf <bundle>.tar verify_airgap_bundle.py\n"
        "   python3 verify_airgap_bundle.py --bundle <bundle>.tar \\\n"
        "       --public-key <trusted>.pub.pem --extract-to ./nova-airgap\n\n"
        "2. Install with the index disabled and every hash enforced:\n\n"
        "   python3 -m venv ./nova-venv\n"
        "   ./nova-venv/bin/python -m pip install --no-index \\\n"
        "       --find-links ./nova-airgap/wheels --require-hashes \\\n"
        "       -r ./nova-airgap/requirements.txt\n\n"
        "3. Check: ./nova-venv/bin/nova --version\n\n"
        "bundle-info.json records the Python version and platform this bundle\n"
        "was built for; install on a matching host.\n"
    )


# ── steps that run tools ───────────────────────────────────────────────────


def _run(cmd: list[str], *, cwd: Path | None = None) -> str:
    try:
        done = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise ClosureError(f"cannot run {cmd[0]}: {exc}") from exc
    if done.returncode != 0:
        tail = (done.stderr or done.stdout).strip()[-2000:]
        raise ClosureError(f"{' '.join(cmd[:3])} … failed (exit {done.returncode}):\n{tail}")
    return done.stdout


def export_requirements(project_dir: Path, extras: list[str], uv: str) -> str:
    """Hashed pins for the dependency closure, straight from ``uv.lock``.

    ``--locked`` refuses a lock that no longer matches pyproject.toml, so the
    bundle can never silently ship a stale closure.
    """
    cmd = [
        uv, "export", "--locked", "--no-dev", "--no-emit-project",
        "--format", "requirements-txt", "--no-header", "--quiet",
    ]  # fmt: skip
    for extra in extras:
        cmd += ["--extra", extra]
    text = _run(cmd, cwd=project_dir)
    if "--hash=sha256:" not in text:
        raise ClosureError("uv export produced no hashes; refusing an unhashed closure")
    return text


def describe_target(
    pip_python: str, python_version: str | None, platforms: list[str]
) -> dict[str, Any]:
    probe = (
        "import json, sys, sysconfig; print(json.dumps({"
        "'python_version': '%d.%d' % sys.version_info[:2], "
        "'platform': sysconfig.get_platform(), "
        "'implementation': sys.implementation.name}))"
    )
    host = json.loads(_run([pip_python, "-c", probe]))
    return {
        "python_version": python_version or host["python_version"],
        # wheel platform tags handed to pip download; [] = the build host's own tags
        "platform_tags": list(platforms),
        "build_host_platform": host["platform"],
        "implementation": host["implementation"],
        "built_for_build_host": python_version is None and not platforms,
    }


def download_wheels(
    pip_cmd: list[str],
    requirements: Path,
    dest: Path,
    python_version: str | None,
    platforms: list[str],
) -> list[Path]:
    """``pip download`` every pinned dependency as a wheel, hash-checked."""
    cmd = [
        *pip_cmd, "download", "--disable-pip-version-check",
        "--only-binary=:all:", "--require-hashes", "--no-deps",
        "--dest", str(dest), "-r", str(requirements),
    ]  # fmt: skip
    if python_version:
        cmd += ["--python-version", python_version]
    for platform in platforms:
        cmd += ["--platform", platform]
    _run(cmd)
    files = sorted(p for p in dest.iterdir())
    stray = [p.name for p in files if p.suffix != ".whl" or not p.is_file()]
    if stray:
        raise ClosureError(f"pip download left non-wheel files: {', '.join(stray)}")
    return files


def _git(project_dir: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(project_dir), *args], capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def build_closure(
    *,
    wheel: Path,
    extras_spec: str,
    project_dir: Path,
    pip_python: str,
    python_version: str | None,
    platforms: list[str],
    staging: Path,
    uv: str = "uv",
) -> tuple[dict[str, Path], str]:
    """Assemble the closure in *staging*; return ({arcname: file}, nova_version)."""
    name, version = parse_wheel_filename(wheel)
    if name != PROJECT_NAME:
        raise ClosureError(f"--closure-wheel must be a {PROJECT_NAME} wheel, got {wheel.name}")
    if not wheel.is_file():
        raise ClosureError(f"--closure-wheel {wheel} is not a file")
    extras = parse_extras(extras_spec, declared_extras(project_dir / "pyproject.toml"))
    pip_cmd = [pip_python, "-m", "pip"]
    try:
        pip_version = _run([*pip_cmd, "--version"]).split()[1]
    except (ClosureError, IndexError) as exc:
        raise ClosureError(
            f"no usable pip for {pip_python}; pass --pip-python <a python with pip>"
        ) from exc
    uv_version = _run([uv, "--version"]).strip()

    deps = export_requirements(project_dir, extras, uv)
    deps_file = staging / "deps.txt"  # input to pip only; not a bundle member
    deps_file.write_text(deps, encoding="utf-8")
    wheels_dir = staging / WHEELS_DIR
    wheels_dir.mkdir()
    dep_wheels = download_wheels(pip_cmd, deps_file, wheels_dir, python_version, platforms)
    bundled_wheel = wheels_dir / wheel.name
    if bundled_wheel.exists():
        raise ClosureError(f"{wheel.name} was also downloaded as a dependency")
    shutil.copy2(wheel, bundled_wheel)
    wheel_sha = sha256_file(bundled_wheel)

    requirements = staging / REQUIREMENTS_NAME
    requirements.write_text(
        deps.rstrip("\n") + "\n" + project_requirement(version, extras, wheel_sha),
        encoding="utf-8",
    )
    commit = _git(project_dir, "rev-parse", "HEAD")
    status = _git(project_dir, "status", "--porcelain", "--untracked-files=no")
    info = bundle_info(
        nova_version=version,
        wheel_name=wheel.name,
        extras=extras,
        target=describe_target(pip_python, python_version, platforms),
        source_commit=commit,
        source_dirty=None if status is None else bool(status),
        lock_sha256=sha256_file(project_dir / "uv.lock"),
        tools={"uv": uv_version, "pip": pip_version},
        wheel_count=len(dep_wheels) + 1,
    )
    (staging / INFO_NAME).write_text(json.dumps(info, indent=2, sort_keys=True) + "\n")
    (staging / INSTALL_NAME).write_text(install_text(extras), encoding="utf-8")
    shutil.copy2(VERIFIER, staging / VERIFIER_NAME)

    members: dict[str, Path] = {f"{WHEELS_DIR}/{p.name}": p for p in sorted(wheels_dir.iterdir())}
    for arc in (REQUIREMENTS_NAME, INFO_NAME, INSTALL_NAME, VERIFIER_NAME):
        members[arc] = staging / arc
    sums = staging / SUMS_NAME
    sums.write_text(render_sha256sums(members), encoding="utf-8")
    members[SUMS_NAME] = sums
    return members, version


def _created_at() -> float | None:
    """Honour SOURCE_DATE_EPOCH (reproducible-builds.org) when it is set."""
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw is None:
        return None
    try:
        return float(int(raw))
    except ValueError:
        raise ClosureError(f"SOURCE_DATE_EPOCH={raw!r} is not an integer") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--signing-key", required=True, type=Path)
    parser.add_argument(
        "--member",
        action="append",
        default=[],
        metavar="ARCNAME=SRC",
        help="repeatable: in-bundle path = source file",
    )
    closure = parser.add_argument_group("closure mode (wheel + pinned dependency wheelhouse)")
    closure.add_argument("--closure-wheel", type=Path, help="the built novafabric wheel")
    closure.add_argument("--extras", default="", help="comma-separated extras (default: core)")
    closure.add_argument(
        "--pip-python",
        default=sys.executable,
        help="python whose pip downloads the wheels (default: this interpreter)",
    )
    closure.add_argument("--python-version", help="passed to pip download (e.g. 3.12)")
    closure.add_argument(
        "--platform",
        action="append",
        default=[],
        help="repeatable, passed to pip download (e.g. manylinux_2_28_x86_64)",
    )
    closure.add_argument("--project-dir", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)

    members: dict[str, Path] = {}
    for spec in args.member:
        arcname, sep, src = spec.partition("=")
        if not sep or not arcname or not src:
            parser.error(f"--member must be ARCNAME=SRC, got {spec!r}")
        members[arcname] = Path(src)
    closure_only = (args.extras, args.python_version, args.platform)
    if args.closure_wheel is None and any(closure_only):
        parser.error("--extras/--python-version/--platform need --closure-wheel")

    from novafabric.export_blob.airgap import AirgapBundleError, build_bundle

    with tempfile.TemporaryDirectory(prefix="nova-airgap-build-") as tmp:
        try:
            nova_version = pkg_version("novafabric")
            if args.closure_wheel is not None:
                clash = sorted(
                    a for a in members if a in CLOSURE_NAMES or a.startswith(f"{WHEELS_DIR}/")
                )
                if clash:
                    parser.error(f"--member names reserved by closure mode: {', '.join(clash)}")
                closure_members, nova_version = build_closure(
                    wheel=args.closure_wheel,
                    extras_spec=args.extras,
                    project_dir=args.project_dir.resolve(),
                    pip_python=args.pip_python,
                    python_version=args.python_version,
                    platforms=args.platform,
                    staging=Path(tmp),
                )
                members.update(closure_members)
            out = build_bundle(
                args.out,
                members,
                signing_key=args.signing_key,
                nova_version=nova_version,
                created_at=_created_at(),
            )
        except (AirgapBundleError, ClosureError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    print(f"wrote {out} ({len(members)} members + signed manifest)")
    print(
        "verify offline with: python3 verify_airgap_bundle.py "
        f"--bundle {out.name} --public-key <trusted pem> --extract-to <new dir>"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
