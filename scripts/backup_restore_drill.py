#!/usr/bin/env python3
"""Single-host backup -> wipe -> restore drill, driven through the real ``nova`` CLI.

Every step is a ``nova`` subprocess in a disposable host directory, so the drill
exercises the same commands an operator types. Nothing here calls the backup
library in-process; that is what ``tests/backup/`` already does. No Docker, no
Postgres, no network, no provider key: the captured workload is a one-line
``python -c`` print.

Steps (each must pass, or the drill exits 1 naming the step):

1. ``nova seal init`` -> local sealing; the PUBLIC CA certificate is copied
   to a "vault" outside the host (the private keys are not).
2. ``nova register`` a synthetic asset, ``nova capture`` a synthetic run; the
   SHA-256 of every capsule file is recorded; ``nova validate`` + ``nova verify``.
3. ``nova backup create`` -> the manifest must carry NO key material (no
   ``keys/`` path, ``*.pem``/``*.key``, ``novaseal.yaml``, or key_material member).
4. ``nova backup verify`` passes; a one-byte-tampered copy FAILS verify and
   FAILS restore without writing a capsule (fail-closed, no misleading success).
5. The whole host (home, audit log, XDG dirs, keys) is deleted.
6. ``nova restore`` into the empty home; every capsule byte is identical to the
   pre-backup record; ``nova validate`` and ``nova verify --ca-bundle`` pass with
   no private key on the host; ``nova inspect`` + ``nova list`` show the asset.
7. Restoring again over the now non-empty home is refused (exit 1) and the
   restored capsule is unchanged.

Upgrade drill: ``--backup-nova`` names the ``nova`` that seals, captures and backs
up (for example the previous PyPI release in its own venv); ``--nova`` names the
one that verifies and restores (the build under test). Both default to the
``nova`` next to this interpreter.

The report (``--report``) holds only synthetic data: commands, exit codes,
versions, hashes and temporary paths. It is safe to publish as a CI artifact.

Usage:
    python scripts/backup_restore_drill.py --report drill.json
    python scripts/backup_restore_drill.py --backup-nova old/bin/nova --nova new/bin/nova
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

ASSET_NAME = "backup-drill-model"
ASSET_VERSION = "1.0.0"
ASSET_REF = f"{ASSET_NAME}@{ASSET_VERSION}"
WORKLOAD_MARKER = "novafabric backup-restore drill workload"

#: Inherited from the caller; everything else is built from scratch, so the real
#: user's HOME, NovaFabric state, audit log, API keys and proxies never reach nova.
PASSTHROUGH_ENV = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")
MANIFEST_NAME = "manifest.json"
CLI_TIMEOUT_S = 180.0
CAPTURE_TIMEOUT_S = 300.0
_TAIL = 2000


class DrillError(Exception):
    """A drill step failed; ``step`` names it in the report."""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(f"[{step}] {message}")
        self.step = step
        self.message = message


@dataclass
class Layout:
    """Where the drill keeps things. ``host`` is deleted in step 5; ``vault`` survives."""

    root: Path
    host: Path = field(init=False)
    home: Path = field(init=False)
    audit_log: Path = field(init=False)
    vault: Path = field(init=False)
    logs: Path = field(init=False)
    work: Path = field(init=False)

    def __post_init__(self) -> None:
        self.host = self.root / "host"
        self.home = self.host / ".novafabric"
        self.audit_log = self.host / "audit" / "audit.jsonl"
        self.vault = self.root / "vault"
        self.logs = self.root / "logs"
        self.work = self.root / "work"


def build_env(parent: Mapping[str, str], layout: Layout) -> dict[str, str]:
    """An environment for ``nova`` built from an allowlist, never a filtered copy."""
    env = {k: parent[k] for k in PASSTHROUGH_ENV if k in parent}
    host = layout.host
    env.update(
        {
            "HOME": str(host),
            "XDG_CONFIG_HOME": str(host / ".config"),
            "XDG_DATA_HOME": str(host / ".local" / "share"),
            "XDG_CACHE_HOME": str(host / ".cache"),
            "XDG_STATE_HOME": str(host / ".local" / "state"),
            "NOVAFABRIC_HOME": str(layout.home),
            "NOVAFABRIC_AUDIT_LOG_PATH": str(layout.audit_log),
            "NOVAFABRIC_SUGGEST": "0",
            "NO_COLOR": "1",
            "COLUMNS": "200",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return env


def hash_tree(directory: Path) -> dict[str, str]:
    """``{relative posix path: sha256}`` for every regular file under *directory*."""
    hashes: dict[str, str] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise DrillError("hash", f"unexpected symbolic link in capsule: {path}")
        if path.is_file():
            rel = path.relative_to(directory).as_posix()
            hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def diff_hashes(expected: Mapping[str, str], actual: Mapping[str, str]) -> list[str]:
    """Human-readable differences between two :func:`hash_tree` results (empty = equal)."""
    problems = [f"missing: {p}" for p in sorted(set(expected) - set(actual))]
    problems += [f"unexpected: {p}" for p in sorted(set(actual) - set(expected))]
    problems += [
        f"changed: {p}" for p in sorted(set(expected) & set(actual)) if expected[p] != actual[p]
    ]
    return problems


def find_single_capsule(capsules_dir: Path) -> Path:
    """The one capsule directory the capture wrote; anything else is a drill failure."""
    if not capsules_dir.is_dir():
        raise DrillError("capture", f"no capsule store at {capsules_dir}")
    found = sorted(p for p in capsules_dir.iterdir() if p.is_dir() and not p.name.startswith("."))
    if len(found) != 1:
        names = [p.name for p in found]
        raise DrillError("capture", f"expected exactly one capsule, found {len(found)}: {names}")
    return found[0]


def read_manifest(set_path: Path) -> dict[str, Any]:
    """The backup set's ``manifest.json``, read straight from the archive."""
    with tarfile.open(set_path, "r:gz") as tar:
        member = tar.extractfile(MANIFEST_NAME)
        if member is None:
            raise DrillError("inspect-set", f"{MANIFEST_NAME} is not a regular file in the set")
        data = json.loads(member.read().decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("members"), list):
        raise DrillError("inspect-set", "manifest has no members list")
    return data


def key_material_members(manifest: Mapping[str, Any]) -> list[str]:
    """Member paths that look like signing-key material (must be empty by default)."""
    flagged: list[str] = []
    for member in manifest.get("members", []):
        path = PurePosixPath(str(member.get("path", "")))
        if (
            member.get("kind") == "key_material"
            or "keys" in path.parts
            or path.suffix in (".pem", ".key")
            or path.name == "novaseal.yaml"
        ):
            flagged.append(path.as_posix())
    return flagged


def capsule_member_paths(manifest: Mapping[str, Any], run_id: str) -> list[str]:
    """In-archive paths of the drill capsule's files (home or external capsule store)."""
    prefixes = (f"capsules/{run_id}/", f"external/capsule-dir/{run_id}/")
    return sorted(
        str(m["path"])
        for m in manifest.get("members", [])
        if str(m.get("path", "")).startswith(prefixes)
    )


def tamper_set(source: Path, dest: Path, member_path: str) -> None:
    """Copy the set to *dest* with the last byte of *member_path* flipped, nothing else."""
    tampered = False
    with tarfile.open(source, "r:gz") as src, tarfile.open(dest, "w:gz") as out:
        for info in src.getmembers():
            if not info.isfile():
                out.addfile(info)
                continue
            fileobj = src.extractfile(info)
            data = fileobj.read() if fileobj is not None else b""
            if info.name == member_path and data:
                data = data[:-1] + bytes([data[-1] ^ 0x01])
                tampered = True
            info.size = len(data)
            out.addfile(info, io.BytesIO(data))
    if not tampered:
        raise DrillError("tamper", f"member {member_path!r} not found or empty in {source}")


def files_under(directory: Path) -> list[str]:
    """Regular files under *directory* (empty when it does not exist)."""
    if not directory.exists():
        return []
    return sorted(p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file())


class Runner:
    """Runs ``nova`` subprocesses, saves each output, and records them in the report."""

    def __init__(self, env: Mapping[str, str], layout: Layout, report: dict[str, Any]) -> None:
        self.env = dict(env)
        self.layout = layout
        self.report = report

    def run(
        self, step: str, argv: Sequence[str], *, timeout: float = CLI_TIMEOUT_S
    ) -> tuple[int, str]:
        self.layout.work.mkdir(parents=True, exist_ok=True)
        try:
            done = subprocess.run(
                list(argv),
                env=self.env,
                cwd=self.layout.work,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                timeout=timeout,
                check=False,
            )
            code, output = done.returncode, done.stdout
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout
            text = out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")
            code, output = 124, text + f"\n[drill] timed out after {timeout:.0f}s\n"
        self.layout.logs.mkdir(parents=True, exist_ok=True)
        log = self.layout.logs / f"{len(self.report['commands']):02d}-{step}.log"
        log.write_text(output, encoding="utf-8")
        self.report["commands"].append(
            {"step": step, "argv": list(argv), "exit_code": code, "log": log.name}
        )
        print(f"[drill] {step}: exit {code}", flush=True)
        return code, output

    def expect(self, step: str, argv: Sequence[str], *, code: int = 0, **kw: Any) -> str:
        got, output = self.run(step, argv, **kw)
        if got != code:
            raise DrillError(step, f"expected exit {code}, got {got}:\n{output[-_TAIL:]}")
        return output


def _python_beside(nova: str) -> str:
    """The interpreter of *nova*'s venv (the workload runs under the capturing install)."""
    candidate = Path(nova).parent / "python"
    return str(candidate) if candidate.is_file() else sys.executable


def _absolute(command: str) -> str:
    """A path-like command made absolute (symbolic links kept); a bare name stays for PATH."""
    return os.path.abspath(command) if os.sep in command else command


def _default_nova() -> str:
    beside = Path(sys.executable).parent / "nova"
    return str(beside) if beside.is_file() else (shutil.which("nova") or "nova")


def run_drill(layout: Layout, *, nova: str, backup_nova: str, report: dict[str, Any]) -> None:
    """Steps 1-7 of the module docstring. Raises :class:`DrillError` on the first failure."""
    for d in (layout.vault, layout.logs, layout.work):
        d.mkdir(parents=True, exist_ok=True)
    layout.host.mkdir(parents=True)
    runner = Runner(build_env(os.environ, layout), layout, report)
    home = layout.home

    report["versions"] = {
        "backup_nova": runner.expect("version-backup", [backup_nova, "--version"]).strip(),
        "restore_nova": runner.expect("version-restore", [nova, "--version"]).strip(),
    }

    # 1. Sealing, with only the public CA certificate kept off-host.
    runner.expect("seal-init", [backup_nova, "seal", "init", "--home", str(home)])
    ca_src = home / "keys" / "novaseal" / "ca.crt.pem"
    if not ca_src.is_file():
        raise DrillError("seal-init", f"no local seal CA certificate at {ca_src}")
    ca_bundle = layout.vault / "local-seal-ca.crt.pem"
    shutil.copyfile(ca_src, ca_bundle)

    # 2. Synthetic asset + synthetic run.
    spec = layout.work / "asset.yaml"
    spec.write_text(
        'novafabric_spec_version: "1"\n'
        "asset_type: model\n"
        f"name: {ASSET_NAME}\n"
        f"version: {ASSET_VERSION}\n"
        "status: development\n"
        'description: "Synthetic asset of the backup-restore drill"\n'
        "tags:\n"
        '  synthetic: "true"\n'
        "spec:\n"
        "  framework: none\n"
        "  artifact_path: drill/model.bin\n",
        encoding="utf-8",
    )
    runner.expect("register", [backup_nova, "register", str(spec)])
    runner.expect(
        "capture",
        [
            backup_nova,
            "capture",
            "--",
            _python_beside(backup_nova),
            "-c",
            f"print({WORKLOAD_MARKER!r})",
        ],
        timeout=CAPTURE_TIMEOUT_S,
    )
    capsule = find_single_capsule(home / "capsules")
    run_id = capsule.name
    report["run_id"] = run_id
    runner.expect("validate-before", [backup_nova, "validate", str(capsule)])
    runner.expect(
        "verify-before",
        [backup_nova, "verify", "--json", "--ca-bundle", str(ca_bundle), str(capsule)],
    )
    # The byte-level record is taken last, right before the backup.
    before = hash_tree(capsule)
    if not before:
        raise DrillError("capture", f"capsule {run_id} holds no files")
    report["capsule_files"] = len(before)

    # 3. Back up; the set must carry no key material by default.
    set_path = layout.vault / "drill-set.tar.gz"
    runner.expect(
        "backup-create",
        [backup_nova, "backup", "create", "--home", str(home), "--output", str(set_path)],
    )
    if not set_path.is_file():
        raise DrillError("backup-create", f"no backup set written at {set_path}")
    manifest = read_manifest(set_path)
    report["set"] = {
        "members": len(manifest["members"]),
        "signing_status": manifest.get("signing_status"),
        "includes_keys": manifest.get("includes_keys", False),
        "manifest_schema_version": manifest.get("manifest_schema_version"),
    }
    leaked = key_material_members(manifest)
    if leaked or manifest.get("includes_keys"):
        raise DrillError("no-keys-in-set", f"default set carries key material: {leaked}")
    in_set = capsule_member_paths(manifest, run_id)
    in_set_rel = {p.split(f"{run_id}/", 1)[1] for p in in_set}
    if in_set_rel != set(before):
        raise DrillError(
            "inspect-set", "set vs capsule: " + "; ".join(sorted(set(before) ^ in_set_rel))
        )

    # 4. The set verifies; a tampered copy is refused by verify AND by restore.
    runner.expect("backup-verify", [nova, "backup", "verify", str(set_path)])
    tampered = layout.vault / "drill-set-tampered.tar.gz"
    target = next(
        (
            str(m["path"])
            for m in manifest["members"]
            if str(m.get("path", "")) in in_set and int(m.get("size_bytes") or 0) > 0
        ),
        None,
    )
    if target is None:
        raise DrillError("tamper", "the capsule has no non-empty file in the set")
    tamper_set(set_path, tampered, target)
    report["tampered_member"] = target
    runner.expect("backup-verify-tampered", [nova, "backup", "verify", str(tampered)], code=1)
    scratch_home = layout.root / "tamper-target" / ".novafabric"
    code, _ = runner.run(
        "restore-tampered", [nova, "restore", str(tampered), "--home", str(scratch_home)]
    )
    if code == 0:
        raise DrillError("restore-tampered", "a tampered set restored with exit 0")
    written = files_under(scratch_home / "capsules")
    if written:
        raise DrillError(
            "restore-tampered", f"a refused restore still wrote capsule files: {written}"
        )

    # 5. Lose the host: home, audit log, XDG dirs and the private keys.
    shutil.rmtree(layout.host)
    if layout.host.exists():
        raise DrillError("wipe", f"{layout.host} still exists")
    layout.host.mkdir()

    # 6. Restore into the empty home and prove the evidence came back byte-for-byte.
    out = runner.expect("restore", [nova, "restore", str(set_path), "--home", str(home)])
    if "Restore complete" not in out:
        raise DrillError("restore", "exit 0 without the 'Restore complete' line")
    restored = home / "capsules" / run_id
    if not restored.is_dir():
        raise DrillError("restore", f"capsule {run_id} not restored at {restored}")
    problems = diff_hashes(before, hash_tree(restored))
    if problems:
        raise DrillError("byte-identical", "; ".join(problems))
    if (home / "keys").exists() or (home / "novaseal.yaml").exists():
        raise DrillError("no-keys-restored", "key material is present after a default restore")
    runner.expect("validate-after", [nova, "validate", str(restored)])
    runner.expect(
        "verify-after", [nova, "verify", "--json", "--ca-bundle", str(ca_bundle), str(restored)]
    )
    inspect = runner.expect("inspect-after", [nova, "inspect", ASSET_REF])
    if ASSET_NAME not in inspect:
        raise DrillError("inspect-after", f"{ASSET_REF} missing from nova inspect output")
    listing = runner.expect("list-after", [nova, "list"])
    if ASSET_NAME not in listing:
        raise DrillError("list-after", f"{ASSET_NAME} missing from nova list output")

    # 7. A second restore over live data is refused and changes nothing.
    settled = hash_tree(restored)
    runner.expect(
        "restore-non-empty", [nova, "restore", str(set_path), "--home", str(home)], code=1
    )
    problems = diff_hashes(settled, hash_tree(restored))
    if problems:
        raise DrillError(
            "restore-non-empty", "refused restore changed the capsule: " + "; ".join(problems)
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Single-host backup -> wipe -> restore drill through the real nova CLI."
    )
    parser.add_argument(
        "--nova", default=None, help="nova that verifies and restores (default: beside this python)"
    )
    parser.add_argument(
        "--backup-nova",
        default=None,
        help="nova that seals, captures and backs up (default: --nova)",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="Empty directory to work in (default: a temp dir, removed on success)",
    )
    parser.add_argument("--report", type=Path, default=None, help="Write the JSON report here")
    args = parser.parse_args(argv)

    # Commands run with cwd=<work dir>, so a relative path to nova must be made absolute.
    nova = _absolute(args.nova or _default_nova())
    backup_nova = _absolute(args.backup_nova) if args.backup_nova else nova
    keep = args.work_dir is not None
    if args.work_dir is not None:
        if args.work_dir.exists() and any(args.work_dir.iterdir()):
            print(f"[drill] --work-dir {args.work_dir} is not empty", file=sys.stderr)
            return 2
        args.work_dir.mkdir(parents=True, exist_ok=True)
        root = args.work_dir.resolve()
    else:
        root = Path(tempfile.mkdtemp(prefix="nova-backup-drill-"))

    report: dict[str, Any] = {
        "drill": "backup-restore",
        "synthetic_data_only": True,
        "commands": [],
    }
    layout = Layout(root)
    try:
        run_drill(layout, nova=nova, backup_nova=backup_nova, report=report)
    except DrillError as exc:
        report.update(ok=False, failed_step=exc.step, error=exc.message[-_TAIL:])
        print(f"[drill] FAILED at {exc.step}: {exc.message[-_TAIL:]}", file=sys.stderr)
        keep = True
    else:
        report["ok"] = True
        print(f"[drill] OK: run {report['run_id']} restored byte-identical and verified")
    finally:
        # An unexpected exception (not a DrillError) still leaves a failing report behind.
        if "ok" not in report:
            report.update(ok=False, failed_step="unexpected-error")
            keep = True
        if args.report is not None:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if keep:
            print(f"[drill] work directory kept: {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
