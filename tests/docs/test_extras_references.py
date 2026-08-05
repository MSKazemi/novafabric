"""Guard: every ``novafabric[<extra>]`` string in the tree names a real extra.

A 2026-07 packaging audit found 9 "phantom extras" — install hints like
``pip install novafabric[collector-cffi]`` or ``novafabric[eval]`` scattered
across docs, UI strings, and error messages that do not correspond to any
``[project.optional-dependencies]`` entry in ``pyproject.toml``. Some were
fixed by adding the missing extra (S1: ``clickhouse``/``nats``/``avro``/
``energy-gpu``); others were fixed by correcting the reference itself to the
extra's real name (S3: ``collector-cffi`` -> plain prose, ``eval`` -> removed
entirely since eval suites are core, ``server-saml`` -> ``saml``). This test
is the permanent guard against that drift recurring — it fails the moment a
new ``novafabric[<name>]`` reference is added whose ``<name>`` does not exist
in ``[project.optional-dependencies]``.

Mirrors the ``REPO_ROOT`` derivation and assertion style of
``tests/docs/test_cli_reference_coverage.py``.

Scope: ``src/``, ``docs/`` (excluding ``docs/releases/``, which is immutable
release history and may legitimately describe an extra as it existed, or was
promised, at that historical release), ``web/``, and ``design/`` (excluding
``design/adr/``, which is an immutable decision-record archive that may
reference extra names that were proposed, renamed, or never shipped — see
e.g. ADR-0138, which still says ``novafabric[server-saml]``, and ADR-0128,
which still says ``novafabric[schema-validation]``). Everything else under
``design/`` — specs, architecture notes, research output — is live
documentation just like ``docs/``, not a frozen record, so a phantom extra
there is a real bug (S3 found and fixed exactly one:
``design/spec/toolcall-schema-validation-v0.md`` claimed a
``novafabric[schema-validation]`` extra that was never implemented; the
feature's dependency, ``jsonschema``, was unconditional core all along).
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Directories scanned for `novafabric[...]` references. Each is walked
# recursively; `docs/releases/` and `design/adr/` are excluded below.
_SCAN_ROOTS = ("src", "docs", "web", "design")
_EXCLUDED_DIRS = (
    REPO_ROOT / "docs" / "releases",
    # design/adr/ is an immutable decision-record archive (see module
    # docstring) — ADRs may reference extra names that were proposed,
    # renamed, or never shipped, and are never edited to "fix" that.
    REPO_ROOT / "design" / "adr",
)

# Only text-ish files are worth scanning; skip binaries and lockfiles.
_SCAN_SUFFIXES = {
    ".py",
    ".md",
    ".mdx",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".json",
    ".rst",
    ".txt",
}

# `novafabric[a,b-c]` — the bracket body is a comma-separated list of extra
# names; extra names in this project are lowercase alnum + hyphen/underscore.
# A stricter charset (no `{`, `<`, `&`, spaces, ...) means template
# placeholders like `novafabric[<extra>]` or `novafabric[{extra}]` (used in
# generic help text) never match, so they can't produce a false positive.
_REFERENCE_RE = re.compile(r"novafabric\[([a-z0-9_,\-]+)\]")


def _known_extras() -> set[str]:
    with PYPROJECT.open("rb") as f:
        data = tomllib.load(f)
    optional_deps = data["project"]["optional-dependencies"]
    assert optional_deps, "pyproject.toml has no [project.optional-dependencies]"
    return set(optional_deps)


def _tracked_paths() -> set[Path] | None:
    """Absolute paths of git-tracked files under the scan roots, or None.

    Only *tracked* files are in scope. Walking the filesystem instead would
    also scan gitignored build output — `web/dist/` (Astro) and
    `packages/*/dist/` (Vite) both contain content-hash-named bundles that
    embed whatever strings the sources had *at the time of the last local
    build*. A developer holding a stale `web/dist/` from before a fix would
    then get a failure describing a phantom reference that no longer exists
    in any source file and that no commit can remove — a false positive on
    untracked local junk. A guard that cries wolf gets deleted, so it must
    only ever fail on something a commit can actually fix.

    Returns None if git is unavailable (not a checkout, no git binary), in
    which case the caller falls back to walking the filesystem — a scan that
    is occasionally noisy beats silently scanning nothing.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--", *_SCAN_ROOTS],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git
        return None
    if result.returncode != 0:  # pragma: no cover - not a git checkout
        return None
    names = result.stdout.decode("utf-8", errors="ignore").split("\0")
    return {REPO_ROOT / name for name in names if name}


def _iter_scanned_files() -> list[Path]:
    tracked = _tracked_paths()
    files: list[Path] = []
    for root_name in _SCAN_ROOTS:
        root = REPO_ROOT / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in _SCAN_SUFFIXES:
                continue
            if tracked is not None and path not in tracked:
                continue
            if any(
                excluded == path or excluded in path.parents
                for excluded in _EXCLUDED_DIRS
            ):
                continue
            files.append(path)
    return files


def _find_phantom_references(
    files: list[Path], known_extras: set[str]
) -> dict[str, set[str]]:
    """Map relative file path -> set of unknown extra names it references."""
    offenders: dict[str, set[str]] = {}
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        unknown: set[str] = set()
        for match in _REFERENCE_RE.finditer(text):
            for name in match.group(1).split(","):
                if name not in known_extras:
                    unknown.add(name)
        if unknown:
            offenders[str(path.relative_to(REPO_ROOT))] = unknown
    return offenders


def test_pyproject_has_optional_dependencies() -> None:
    assert _known_extras()


def test_scan_finds_the_known_evidence_fabric_extras() -> None:
    """Sanity check the scanner itself: it must actually see real references.

    A regex that silently matches nothing (e.g. because a scan root moved)
    would make the "no phantom extras" assertion below pass vacuously. Assert
    the scan positively finds at least the extras the evidence_fabric module
    is known to reference, so an empty-result false pass can't hide.
    """
    files = _iter_scanned_files()
    assert files, f"no files found under {_SCAN_ROOTS} — scan roots may be wrong"
    seen: set[str] = set()
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in _REFERENCE_RE.finditer(text):
            seen.update(match.group(1).split(","))
    for expected in ("scale", "nats", "clickhouse", "avro", "saml", "serve"):
        assert expected in seen, (
            f"expected to see at least one novafabric[{expected}] reference "
            "somewhere in src/docs/web/design — the scanner may be broken"
        )


def test_no_phantom_extra_references_in_src_docs_web() -> None:
    """Every ``novafabric[<name>]`` reference under src/, docs/, web/, design/
    must name a real ``[project.optional-dependencies]`` entry.

    docs/releases/ and design/adr/ are excluded (both immutable historical
    archives — see module docstring); everything else is live documentation
    in scope.
    """
    known_extras = _known_extras()
    files = _iter_scanned_files()
    offenders = _find_phantom_references(files, known_extras)
    assert not offenders, (
        "found novafabric[<extra>] reference(s) naming an extra that does not "
        f"exist in [project.optional-dependencies]: {offenders!r}. Either add "
        "the extra to pyproject.toml (if it should exist) or fix the "
        "reference to a real extra name / remove the phantom install hint."
    )


def test_evidence_fabric_import_error_hints_name_real_extras() -> None:
    """Every ``pip install novafabric[...]`` hint raised by evidence_fabric's
    lazy-import ``ImportError`` handlers must name a real, resolvable extra.

    This is the acceptance-criteria-mandated, non-eyeballed check: it reads
    the actual source of each evidence_fabric module and asserts on the
    literal text of the install hints they raise at runtime.
    """
    known_extras = _known_extras()
    evidence_fabric_dir = REPO_ROOT / "src" / "novafabric" / "evidence_fabric"
    py_files = sorted(evidence_fabric_dir.glob("*.py"))
    assert py_files, f"expected .py files under {evidence_fabric_dir}"

    found_any_hint = False
    offenders: dict[str, set[str]] = {}
    for path in py_files:
        text = path.read_text(encoding="utf-8")
        hints = re.findall(r"pip install novafabric\[([a-z0-9_,\-{}]+)\]", text)
        for hint in hints:
            if "{" in hint:
                # An f-string template (e.g. `novafabric[{extra}]`) — the
                # runtime value is data-driven (the `extra` variable), not a
                # static literal this test can check without executing the
                # module; skip it, the static hints below cover every
                # concrete extra name that actually ships in this file set.
                continue
            found_any_hint = True
            for name in hint.split(","):
                if name not in known_extras:
                    offenders.setdefault(str(path.relative_to(REPO_ROOT)), set()).add(
                        name
                    )

    assert found_any_hint, (
        "found no 'pip install novafabric[...]' ImportError hints under "
        f"{evidence_fabric_dir} — expected at least one static hint "
        "(e.g. avro_serializer.py's novafabric[avro]); the check may be broken"
    )
    assert not offenders, (
        "evidence_fabric ImportError hint(s) name a phantom extra: "
        f"{offenders!r}"
    )


def test_scan_ignores_untracked_build_output() -> None:
    """A stale gitignored build artifact must not fail the guard.

    Regression: `web/dist/_astro/EvalTab.*.js` (Astro build output, gitignored)
    still embedded the `novafabric[eval]` hint that S3 removed from
    `web/src/.../EvalTab.tsx`, because the local bundle predated the fix. The
    filesystem walk scanned it and failed the guard on a file no commit can
    fix. Only git-tracked files are in scope now; this test pins that.
    """
    scanned = _iter_scanned_files()
    tracked = _tracked_paths()
    if tracked is None:  # pragma: no cover - git unavailable
        pytest.skip("git unavailable; scanner fell back to a filesystem walk")

    dist_hits = [p for p in scanned if "dist" in p.parts or "node_modules" in p.parts]
    assert not dist_hits, (
        "scanner picked up build-output paths that are not git-tracked: "
        f"{[str(p.relative_to(REPO_ROOT)) for p in dist_hits[:5]]}"
    )
    assert all(p in tracked for p in scanned), (
        "scanner returned a file that git does not track"
    )
