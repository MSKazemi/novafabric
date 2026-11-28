"""Every external build input is pinned, and every fetched binary is verified (#220).

A build that names its inputs by a moving reference (``@v4``, ``:latest``, an unpinned
``uvx tool``) or downloads an executable on the strength of TLS alone can produce a
different artifact from the same source tree, and nobody can tell afterwards which one
shipped. This guard fails when one of those references comes back.

What it checks, over the files the public git tracks:

1. **Actions.** Every ``uses:`` of a third-party action in ``.github/workflows/*.yml`` and
   in a composite action (``.github/actions/<name>/action.yml``) names a full 40-hex commit
   SHA and carries a ``# vX.Y.Z`` comment. A ``docker://`` action names an image digest.
   Local ``./`` actions and reusable workflows are exempt.
2. **Dependabot reaches every composite action**, so those SHA pins keep moving: the
   ``github-actions`` entry's ``directories`` must match each composite action's folder.
3. **Images in Dockerfiles.** Every ``FROM`` and ``COPY --from=`` that names an image (not
   an earlier stage, not ``scratch``) carries both a tag and an ``@sha256:`` digest: the
   digest is what is pulled, the tag is what Dependabot reads to propose the next one.
4. **Downloads.** A ``curl``/``wget`` of a non-loopback URL — in a workflow or composite
   ``run:`` step, a Dockerfile ``RUN``, a tracked shell script or the Makefile — is never
   piped into a shell, and the same step verifies a checksum or signature
   (``sha256sum -c``, ``sha512sum -c``, ``shasum -c``, ``cosign verify-blob``,
   ``gh attestation verify``).
5. **Checksum build args are real.** A Dockerfile ``ARG`` whose name contains ``SHA256``
   defaults to 64 lowercase hex characters, so the verification in (4) cannot be emptied.
6. **Tool runners name a version.** ``uvx`` names ``tool==X.Y.Z`` (or ``tool@X.Y.Z``);
   ``go install`` names an exact ``@vX.Y.Z``; npm installs come from the lockfile
   (``npm ci``), never ``npm install``.

Exceptions go in :data:`ALLOWED`, each with the reason it is safe. An entry that no
longer matches anything fails :func:`test_every_allowlist_entry_is_still_needed`, so the
list cannot outlive its reasons.

How to bump each pinned input: ``docs/ci.md``, "Pinned build inputs".
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
DEPENDABOT = REPO / ".github" / "dependabot.yml"

#: Justified exceptions: ``"<repo-relative path>::<exact violation text>"`` -> reason.
#: Keep the reason specific enough that a reviewer can check it.
ALLOWED: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Rule 1: actions are pinned to a commit SHA with a release comment.
# ---------------------------------------------------------------------------

_USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>[^\s#]+)\s*(?:#\s*(?P<comment>.*))?$")
_SHA_PINNED = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
_DOCKER_DIGEST = re.compile(r"^docker://[^@\s]+@sha256:[0-9a-f]{64}$")
_VERSION_COMMENT = re.compile(r"^v?\d+(?:\.\d+){0,2}\b")


def uses_violations(text: str) -> list[str]:
    """``uses:`` lines that name a third-party action by a moving reference."""
    out = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        match = _USES.match(line)
        if not match:
            continue
        ref = match.group("ref").strip("'\"")
        if ref.startswith("./"):
            continue
        if ref.startswith("docker://"):
            if not _DOCKER_DIGEST.match(ref):
                out.append(f"line {lineno}: {ref} is not pinned to an image digest")
            continue
        if not _SHA_PINNED.match(ref):
            out.append(f"line {lineno}: {ref} is not pinned to a full commit SHA")
            continue
        comment = (match.group("comment") or "").strip()
        if not _VERSION_COMMENT.match(comment):
            out.append(f"line {lineno}: {ref} has no '# vX.Y.Z' comment naming its release")
    return out


# ---------------------------------------------------------------------------
# Rule 3 and 5: Dockerfile images carry a digest; checksum args are real.
# ---------------------------------------------------------------------------

_IMAGE_PINNED = re.compile(r"^[^@\s:]+(?::\d+)?(?:/[^@\s:]+)*:[^@\s/]+@sha256:[0-9a-f]{64}$")
_SHA_ARG = re.compile(r"^ARG\s+(?P<name>\w*SHA256\w*)(?:=(?P<value>\S*))?$", re.IGNORECASE)


def _instructions(text: str) -> list[tuple[int, str]]:
    """Logical Dockerfile instructions (continuations joined), with their first line."""
    out: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 0
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not buf and (not line or line.startswith("#")):
            continue
        if buf and line.startswith("#"):
            continue  # a comment inside a continued instruction is dropped by Docker
        if not buf:
            start = lineno
        if line.endswith("\\"):
            buf.append(line[:-1])
            continue
        buf.append(line)
        out.append((start, " ".join(part.strip() for part in buf)))
        buf = []
    if buf:
        out.append((start, " ".join(buf)))
    return out


def _image_ok(image: str, stages: set[str]) -> bool:
    return image == "scratch" or image.lower() in stages or bool(_IMAGE_PINNED.match(image))


def dockerfile_violations(text: str) -> list[str]:
    """Unpinned images, unverified downloads and empty checksum args in a Dockerfile."""
    out = []
    stages: set[str] = set()
    for lineno, instruction in _instructions(text):
        keyword, _, rest = instruction.partition(" ")
        keyword = keyword.upper()
        if keyword == "FROM":
            args = [a for a in rest.split() if not a.startswith("--")]
            image = args[0] if args else ""
            if not _image_ok(image, stages):
                out.append(f"line {lineno}: FROM {image} is not pinned by tag and digest")
            if len(args) >= 3 and args[1].upper() == "AS":
                stages.add(args[2].lower())
        elif keyword == "COPY":
            for arg in rest.split():
                if arg.startswith("--from="):
                    source = arg.removeprefix("--from=")
                    if not source.isdigit() and not _image_ok(source, stages):
                        out.append(
                            f"line {lineno}: COPY --from={source} is not pinned by tag and digest"
                        )
        elif keyword == "ARG":
            match = _SHA_ARG.match(instruction)
            if match and not re.fullmatch(r"[0-9a-f]{64}", match.group("value") or ""):
                out.append(
                    f"line {lineno}: ARG {match.group('name')} has no 64-hex default checksum"
                )
        elif keyword == "RUN":
            out.extend(f"line {lineno}: {v}" for v in script_violations(rest))
    return out


# ---------------------------------------------------------------------------
# Rules 4 and 6: downloads are verified; tool runners name a version.
# ---------------------------------------------------------------------------

_FETCH = re.compile(r"(?<![\w-])(?:curl|wget)\b")
_URL = re.compile(r"""https?://(?P<host>[^/\s"'$:]+)""")
_LOOPBACK = {"localhost", "127.0.0.1", "[::1]", "0.0.0.0"}
_PIPE_TO_SHELL = re.compile(r"\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b")
_VERIFIED = re.compile(
    r"\bsha(?:256|512)sum\b[^\n|;&]*\s(?:-c|--check)\b"
    r"|\bshasum\b[^\n|;&]*\s(?:-c|--check)\b"
    r"|\bcosign\s+verify-blob\b"
    r"|\bgh\s+attestation\s+verify\b"
)
_UVX = re.compile(r"(?<![\w-])uvx\s+(?P<args>[^\n;&|]*)")
_GO_INSTALL = re.compile(r"(?<![\w-])go\s+install\s+(?P<target>\S+)")
_NPM_INSTALL = re.compile(r"(?<![\w-])npm\s+(?:install|i|add)\b")


def _logical_lines(script: str) -> list[str]:
    joined = re.sub(r"\\\n", " ", script)
    return [ln for ln in joined.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def _uvx_pinned(args: str) -> bool:
    tokens = args.split()
    for i, token in enumerate(tokens):
        if token == "--from" and i + 1 < len(tokens):
            return "==" in tokens[i + 1]
        if token.startswith("--from="):
            return "==" in token
    positional = [t for t in tokens if not t.startswith("-")]
    return bool(positional) and ("==" in positional[0] or "@" in positional[0])


def script_violations(script: str) -> list[str]:
    """Unverified downloads and unversioned tool runs in one shell script or step."""
    out = []
    verified = bool(_VERIFIED.search(script))
    for line in _logical_lines(script):
        if _FETCH.search(line):
            hosts = [m.group("host") for m in _URL.finditer(line)]
            external = [h for h in hosts if h not in _LOOPBACK]
            if external and _PIPE_TO_SHELL.search(line):
                out.append(f"download from {external[0]} is piped into a shell")
            elif external and not verified:
                out.append(f"download from {external[0]} is not checksum- or signature-verified")
        for match in _UVX.finditer(line):
            if not _uvx_pinned(match.group("args")):
                out.append(f"uvx {match.group('args').strip()} names no exact version")
        for match in _GO_INSTALL.finditer(line):
            if not re.search(r"@v\d+\.\d+\.\d+$", match.group("target")):
                out.append(f"go install {match.group('target')} names no exact @vX.Y.Z")
        if _NPM_INSTALL.search(line):
            out.append("npm install resolves outside the lockfile; use npm ci")
    return out


def _steps(doc: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    """(location, step) for every step of a workflow or a composite action."""
    for job_name, job in (doc.get("jobs") or {}).items():
        for i, step in enumerate((job or {}).get("steps") or []):
            yield f"job {job_name} step {step.get('name') or i}", step
    runs = doc.get("runs") or {}
    for i, step in enumerate(runs.get("steps") or [] if isinstance(runs, dict) else []):
        yield f"step {step.get('name') or i}", step


def workflow_violations(text: str) -> list[str]:
    """Rules 1, 4 and 6 for one workflow or composite action file."""
    out = uses_violations(text)
    doc = yaml.safe_load(text) or {}
    for where, step in _steps(doc):
        run = step.get("run")
        if isinstance(run, str):
            out.extend(f"{where}: {v}" for v in script_violations(run))
    return out


# ---------------------------------------------------------------------------
# The files, from the public git.
# ---------------------------------------------------------------------------


def _tracked() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    )
    return [p for p in result.stdout.splitlines() if p and (REPO / p).is_file()]


TRACKED = _tracked()
WORKFLOW_FILES = sorted(p for p in TRACKED if re.fullmatch(r"\.github/workflows/[^/]+\.ya?ml", p))
COMPOSITE_ACTIONS = sorted(
    p for p in TRACKED if re.fullmatch(r"\.github/actions/[^/]+/action\.ya?ml", p)
)
DOCKERFILES = sorted(p for p in TRACKED if "Dockerfile" in Path(p).name)
SHELL_FILES = sorted(p for p in TRACKED if p.endswith(".sh") or p == "Makefile")


def _violations_for(path: str) -> list[str]:
    text = (REPO / path).read_text(encoding="utf-8")
    if path in DOCKERFILES:
        return dockerfile_violations(text)
    if path in SHELL_FILES:
        return script_violations(text)
    return workflow_violations(text)


def _unallowed(path: str) -> list[str]:
    return [v for v in _violations_for(path) if f"{path}::{v}" not in ALLOWED]


# ---------------------------------------------------------------------------
# The checks catch what they exist to catch.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("      - uses: actions/checkout@v7\n", "not pinned to a full commit SHA"),
        ("      - uses: actions/checkout@main\n", "not pinned to a full commit SHA"),
        ("      - uses: actions/checkout@3d3c42e5\n", "not pinned to a full commit SHA"),
        ("        uses: docker://alpine:3.20\n", "not pinned to an image digest"),
        ("      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n", "no '# vX"),
        (
            "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # main\n",
            "no '# vX",
        ),
    ],
)
def test_a_moving_action_reference_is_caught(line: str, expected: str) -> None:
    found = uses_violations("steps:\n" + line)
    assert len(found) == 1 and expected in found[0], found


def test_pinned_local_and_digest_actions_pass() -> None:
    text = (
        "steps:\n"
        "  - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n"
        "  - uses: ./.github/actions/capture\n"
        "  - uses: docker://alpine:3.20@sha256:" + "a" * 64 + "\n"
    )
    assert uses_violations(text) == []


@pytest.mark.parametrize(
    ("dockerfile", "expected"),
    [
        ("FROM python:3.12-slim\n", "FROM python:3.12-slim is not pinned"),
        ("FROM python@sha256:" + "a" * 64 + "\n", "is not pinned by tag and digest"),
        ("FROM python:3.12-slim AS b\nCOPY --from=ghcr.io/astral-sh/uv:latest /uv /uv\n", "FROM"),
        (
            "FROM python:3.12-slim@sha256:"
            + "a" * 64
            + "\nCOPY --from=ghcr.io/astral-sh/uv:latest /uv /u\n",
            "COPY --from=ghcr.io/astral-sh/uv:latest",
        ),
        ("FROM scratch\nARG TOOL_SHA256=\n", "ARG TOOL_SHA256 has no 64-hex"),
        ("FROM scratch\nARG TOOL_SHA256\n", "ARG TOOL_SHA256 has no 64-hex"),
        (
            "FROM scratch\nRUN curl -fsSL -o /usr/local/bin/opa \\\n"
            "    https://github.com/open-policy-agent/opa/releases/download/v1/opa && \\\n"
            "    chmod +x /usr/local/bin/opa\n",
            "download from github.com is not checksum- or signature-verified",
        ),
    ],
)
def test_an_unpinned_dockerfile_input_is_caught(dockerfile: str, expected: str) -> None:
    found = dockerfile_violations(dockerfile)
    assert found and expected in found[0], found


def test_a_pinned_dockerfile_passes() -> None:
    digest = "@sha256:" + "b" * 64
    text = (
        f"FROM ghcr.io/astral-sh/uv:0.13.10{digest} AS uv\n"
        f"FROM --platform=$BUILDPLATFORM python:3.12-slim{digest} AS builder\n"
        "COPY --from=uv /uv /usr/local/bin/uv\n"
        "FROM builder\n"
        "COPY --from=0 /x /x\n"
        f"ARG TOOL_SHA256={'c' * 64}\n"
        "# curl https://example.com/in-a-comment | sh\n"
        "RUN curl -fsSL -o /tmp/t https://example.com/t \\\n"
        '    && echo "${TOOL_SHA256}  /tmp/t" | sha256sum -c -\n'
    )
    assert dockerfile_violations(text) == []


def test_a_registry_with_a_port_is_still_a_pinned_image() -> None:
    assert dockerfile_violations(f"FROM localhost:5000/x:1.0@sha256:{'d' * 64}\n") == []


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("curl -fsSL https://get.example.sh | sh\n", "piped into a shell"),
        ("wget -qO- https://get.example.sh | sudo bash\n", "piped into a shell"),
        ("curl -fsSL -o t.tgz https://get.helm.sh/t.tgz\ntar xzf t.tgz\n", "not checksum"),
        ("uvx twine check dist/*\n", "uvx twine check dist/* names no exact version"),
        ('uvx --from "pip-audit" pip-audit\n', "names no exact version"),
        ("go install golang.org/x/vuln/cmd/govulncheck@latest\n", "no exact @vX.Y.Z"),
        ("go install golang.org/x/vuln/cmd/govulncheck\n", "no exact @vX.Y.Z"),
        ("npm install\n", "use npm ci"),
        ("npm i left-pad\n", "use npm ci"),
    ],
)
def test_an_unverified_download_or_unversioned_tool_is_caught(script: str, expected: str) -> None:
    found = script_violations(script)
    assert len(found) == 1 and expected in found[0], found


@pytest.mark.parametrize(
    "script",
    [
        'curl -fsSL -o "$T/h.tgz" "https://get.helm.sh/h.tgz"\n'
        'echo "${HELM_SHA256}  $T/h.tgz" | sha256sum -c -\n',
        "curl -fsSL -o b https://example.com/b && cosign verify-blob --bundle b.json b\n",
        "curl -fsSL -o b https://github.com/o/r/releases/download/v1/b\n"
        "gh attestation verify b --repo o/r\n",
        "if curl -fsS http://localhost:9000/healthz >/dev/null; then :; fi\n",
        'curl "${args[@]}" "http://127.0.0.1:${PORT}/livez"\n',
        'uvx --from "pip-audit==2.10.1" pip-audit -r r.txt\n',
        "uvx ruff@0.9.0 check\n",
        "go install golang.org/x/vuln/cmd/govulncheck@v1.9.0\n",
        "npm ci\nnpx tsc --noEmit\n",
        "# curl https://get.example.sh | sh\n",
    ],
)
def test_verified_loopback_and_versioned_commands_pass(script: str) -> None:
    assert script_violations(script) == []


def test_run_steps_of_workflows_and_composite_actions_are_scanned() -> None:
    workflow = "jobs:\n  a:\n    steps:\n      - name: get\n        run: uvx twine check\n"
    composite = "runs:\n  using: composite\n  steps:\n    - name: get\n      run: npm install\n"
    assert workflow_violations(workflow) == [
        "job a step get: uvx twine check names no exact version"
    ]
    assert workflow_violations(composite) == [
        "step get: npm install resolves outside the lockfile; use npm ci"
    ]


# ---------------------------------------------------------------------------
# The repository satisfies them.
# ---------------------------------------------------------------------------


def test_the_scan_has_files_to_check() -> None:
    """Guard the guard: an empty file set would make every assertion vacuous."""
    assert len(WORKFLOW_FILES) >= 10, WORKFLOW_FILES
    assert COMPOSITE_ACTIONS, "found no .github/actions/*/action.yml"
    assert "deploy/docker/Dockerfile" in DOCKERFILES, DOCKERFILES


@pytest.mark.parametrize("path", WORKFLOW_FILES + COMPOSITE_ACTIONS)
def test_workflow_and_action_inputs_are_pinned(path: str) -> None:
    found = _unallowed(path)
    assert not found, f"{path}:\n  " + "\n  ".join(found)


@pytest.mark.parametrize("path", DOCKERFILES)
def test_dockerfile_inputs_are_pinned(path: str) -> None:
    found = _unallowed(path)
    assert not found, f"{path}:\n  " + "\n  ".join(found)


@pytest.mark.parametrize("path", SHELL_FILES)
def test_shell_script_downloads_are_verified(path: str) -> None:
    found = _unallowed(path)
    assert not found, f"{path}:\n  " + "\n  ".join(found)


def test_the_runtime_image_verifies_its_opa_download() -> None:
    """The one binary the runtime image fetches is checked for each shipped platform."""
    text = (REPO / "deploy" / "docker" / "Dockerfile").read_text(encoding="utf-8")
    for arch in ("AMD64", "ARM64"):
        assert re.search(rf"^ARG OPA_SHA256_{arch}=[0-9a-f]{{64}}$", text, re.M), arch
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert 'echo "${OPA_SHA256}  /tmp/opa" | sha256sum -c -' in code
    assert ":latest" not in code


def _actions_directories() -> list[str]:
    config = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))
    dirs: list[str] = []
    for entry in config.get("updates", []):
        if entry.get("package-ecosystem") == "github-actions":
            dirs.extend(entry.get("directories") or [])
            if entry.get("directory"):
                dirs.append(entry["directory"])
    return ["/" + d.strip("/") for d in dirs]


@pytest.mark.parametrize("path", COMPOSITE_ACTIONS)
def test_dependabot_updates_every_composite_action(path: str) -> None:
    """A composite action outside Dependabot's reach keeps its pins forever."""
    folder = "/" + str(Path(path).parent)
    patterns = _actions_directories()
    assert any(fnmatch.fnmatchcase(folder, pattern) for pattern in patterns), (
        f"{path}: no github-actions `directories` entry in .github/dependabot.yml "
        f"matches {folder!r} (have {patterns})"
    )


def test_every_allowlist_entry_is_still_needed() -> None:
    """An exception that matches nothing is a stale reason waiting to hide a new problem."""
    live = {
        f"{path}::{v}"
        for path in WORKFLOW_FILES + COMPOSITE_ACTIONS + DOCKERFILES + SHELL_FILES
        for v in _violations_for(path)
    }
    stale = sorted(set(ALLOWED) - live)
    assert not stale, "remove these ALLOWED entries; nothing matches them:\n  " + "\n  ".join(stale)
