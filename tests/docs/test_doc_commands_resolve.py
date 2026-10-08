"""Every ``nova …`` invocation in the user-facing docs resolves against the real CLI.

``test_cli_reference_coverage`` proves the *other* direction — every registered
command is mentioned somewhere. Nothing proved that what the docs tell a reader
to type actually exists. On 2026-10-08 this check found 54 invocations that did
not — ``nova promote direct --actor``, ``nova api-proxy --port``,
``nova evidence export``, ``nova eval <name@version>``, whole reference sections
written against an earlier interface — each of which a reader copying it gets
``No such command`` or ``No such option`` for, on the first keystroke.

What this checks, for every ``nova`` (or ``novafabric``) invocation found in a
fenced code block or an inline code span:

1. each sub-command word resolves, group by group, against the Typer/Click tree
   imported from :mod:`novafabric.cli.main`;
2. each ``--flag`` / ``-f`` handed to the resolved command is one of that
   command's declared options (``--help`` always is).

What it deliberately does not check: positional-argument arity (doc synopses use
``A / B / C`` shorthand and placeholders), option values, and anything after a
``--`` separator or after the first positional of a pass-through command such as
``nova capture`` — those flags belong to the wrapped program, not to ``nova``.

Scope: ``README.md``, ``llms.txt``, ``examples/**/*.md`` and ``docs/**/*.md`` except
``docs/releases/**`` — historical release notes describe the CLI as it was on the day they shipped
and stay historical, as does ``CHANGELOG.md`` (not scanned).

An invocation that is *intentionally* not runnable (a planned command, or a
sentence saying a command does not exist) goes in :data:`ALLOWLIST` with a
reason. The entry only holds while the prose around it still carries that label,
and an entry that no longer matches anything fails the suite, so the list cannot
rot or quietly excuse a real instruction.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from novafabric.cli.introspect import root_command, subcommands

REPO_ROOT = Path(__file__).resolve().parents[2]

_PLANNED = "labelled planned / future design in the same sentence (docs honesty rule)"
_ABSENT = "the sentence states that this command or flag does not exist"

#: (path relative to the repo root, invocation text) -> why it is not a doc bug.
#: An entry excuses a finding only while the surrounding prose still carries an
#: honesty label (:data:`_HONESTY`) — so re-adding the same invocation to that
#: file as a plain instruction fails again. Every entry must also still match a
#: finding (see the stale-entry test below).
ALLOWLIST: dict[tuple[str, str], str] = {
    ("docs/cli-reference.md", "nova memstore mutation show|verify"): _PLANNED,
    ("docs/cli-reference.md", "nova lineage memstore"): _PLANNED,
    ("docs/cli-reference.md", "nova replay --check-equivalence"): _ABSENT,
    ("docs/cli-reference.md", "nova assure-baseline list"): _PLANNED,
    ("docs/cli-reference.md", "nova federate"): _PLANNED,
    ("docs/cli-reference.md", "nova trust-anchor add"): _PLANNED,
    ("docs/cli-reference.md", "nova embodied sensors|actuation|verify"): _PLANNED,
    ("docs/cli-reference.md", "nova export-dssad"): _PLANNED,
    ("docs/cli-reference.md", "nova lineage embodied"): _PLANNED,
    ("docs/cli-reference.md", "nova preserve"): _PLANNED,
    ("docs/cli-reference.md", "nova fixity"): _PLANNED,
    ("docs/cli-reference.md", "nova export-preservation"): _PLANNED,
    (
        "docs/cli-reference.md",
        "nova settlement bind|show|verify|reconcile|finality|reversals",
    ): _PLANNED,
    ("docs/cli-reference.md", "nova dispute"): _PLANNED,
    ("docs/cli-reference.md", "nova export-settlement"): _PLANNED,
    ("docs/cli-reference.md", "nova policy budget set|list|show"): _PLANNED,
    ("docs/dashboard.md", "nova policy check"): _ABSENT,
    ("docs/dashboard.md", "nova store status"): _ABSENT,
    ("docs/dashboard.md", "nova db status"): _ABSENT,
    ("docs/dashboard.md", "nova eval <name@version>"): _ABSENT,
    ("docs/frontier-safety.md", "nova safety verify"): _PLANNED,
    ("docs/integrations/writing-a-hook-plugin.md", "nova plugins list / disable / enable"): (
        _ABSENT
    ),
    ("docs/ops/air-gapped-install.md", "nova doctor --check-cves"): _PLANNED,
    ("docs/ops/cluster-scale-migration.md", "nova lineage store"): _ABSENT,
    ("docs/ops/data-residency.md", "nova lineage --jurisdiction"): _PLANNED,
    ("docs/ops/data-residency.md", "nova storage relocate"): _PLANNED,
    ("docs/ops/server-deployment.md", "nova server issue-scim-token"): _PLANNED,
}

#: Prose that marks an invocation as not runnable today, searched in the three
#: lines before and two after it (a planned list often wraps across lines).
_HONESTY = re.compile(
    r"planned|future design|future work|not yet|not implemented|not in this slice"
    r"|do(es)? not exist|there is no|no direct cli|no `nova",
    re.IGNORECASE,
)

_PROGRAMS = frozenset({"nova", "novafabric"})
_WORD = re.compile(r"^[a-z][a-z0-9_-]*$")
_SHORT_FLAG = re.compile(r"^-[A-Za-z]$")
_LONG_FLAG = re.compile(r"^--[a-z0-9][a-z0-9-]*$")
#: A token standing in for a sub-command rather than naming one.
_PLACEHOLDER = re.compile(r"^([<\[{$]|\.\.\.|…|[A-Z][A-Z0-9_-]*$)")
_FENCE = re.compile(r"^\s*(```+|~~~+)")
_INLINE_CODE = re.compile(r"(`+)(.+?)\1")
_SUBSHELL = re.compile(r"\$\([^()]*\)")
#: A preceding token that makes the next ``nova`` a name, not the program:
#: a compose service / Helm release (``docker compose exec nova sh``,
#: ``helm install nova …``) or an option value (``--keyring novafabric``).
_NAME_CONTEXT = frozenset(
    {"exec", "logs", "restart", "stop", "start", "up", "rm", "install", "upgrade"}
)
#: Tokens that end one shell command: pipes, redirects, separators, comments.
_STOP = re.compile(r"^(\||\|\||&&|;|&|>|>>|2>|2>&1|<|#.*|\)|`)$")


@dataclass(frozen=True)
class Invocation:
    path: str
    line: int
    text: str
    tokens: tuple[str, ...]


def scanned_files() -> list[Path]:
    files = [REPO_ROOT / "README.md", REPO_ROOT / "llms.txt"]
    releases = REPO_ROOT / "docs" / "releases"
    files += sorted(
        p for p in (REPO_ROOT / "docs").rglob("*.md") if releases not in p.parents
    )
    # The examples are the one tree a reader copies verbatim, so their READMEs
    # are held to the same bar as the docs.
    files += sorted((REPO_ROOT / "examples").rglob("*.md"))
    return [p for p in files if p.is_file()]


def _tokenize(text: str) -> list[str]:
    text = text.strip()
    if text.startswith("$ "):
        text = text[2:]
    # A command substitution is an argument value, not more `nova` flags.
    text = _SUBSHELL.sub("SUBSHELL", text)
    try:
        tokens = shlex.split(text, comments=False, posix=True)
    except ValueError:  # unbalanced quote in a prose-ish span
        tokens = text.split()
    for index, token in enumerate(tokens):
        if token.startswith("#"):  # shell comment: prose, not a command
            return tokens[:index]
    return tokens


def _invocations_in(tokens: list[str]) -> Iterator[tuple[str, ...]]:
    """Every ``nova …`` sub-sequence of a tokenized command line."""
    for index, token in enumerate(tokens):
        if token not in _PROGRAMS:
            continue
        before = tokens[index - 1] if index else ""
        if before in _NAME_CONTEXT or before.startswith("--"):
            continue
        rest = tokens[index + 1 :]
        # `helm install nova ./chart`, a bare mention, `nova` as a release
        # name: only treat it as an invocation when a command word or an
        # option follows.
        if rest and (_WORD.match(rest[0]) or rest[0].startswith("-")):
            yield tuple(rest)


def extract(path: Path, text: str) -> Iterator[Invocation]:
    """Yield invocations from fenced blocks and inline code spans of ``text``."""
    rel = path.relative_to(REPO_ROOT).as_posix() if path.is_absolute() else str(path)
    in_fence = False
    fence_marker = ""
    pending: list[str] = []
    pending_start = 0
    for lineno, raw in enumerate(text.splitlines(), start=1):
        fence = _FENCE.match(raw)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence, fence_marker = True, marker[0] * 3
                continue
            if marker.startswith(fence_marker):
                in_fence, pending = False, []
                continue
        if in_fence:
            if not pending:
                pending_start = lineno
            stripped = raw.rstrip()
            if stripped.endswith("\\"):
                pending.append(stripped[:-1])
                continue
            pending.append(stripped)
            logical = " ".join(pending)
            pending = []
            for tokens in _invocations_in(_tokenize(logical)):
                yield Invocation(rel, pending_start, logical.strip(), tokens)
            continue
        for match in _INLINE_CODE.finditer(raw):
            span = match.group(2)
            for tokens in _invocations_in(_tokenize(span)):
                yield Invocation(rel, lineno, span.strip(), tokens)


def _option_names(command: Any) -> set[str]:
    names = set()
    for param in getattr(command, "params", []):
        names.update(getattr(param, "opts", []))
        names.update(getattr(param, "secondary_opts", []))
    settings = getattr(command, "context_settings", None) or {}
    names.update(settings.get("help_option_names", ["--help"]))
    return names


def _takes_value(command: Any, flag: str) -> bool:
    for param in getattr(command, "params", []):
        if flag in getattr(param, "opts", []) or flag in getattr(
            param, "secondary_opts", []
        ):
            return not (getattr(param, "is_flag", False) or getattr(param, "count", False))
    return False


def _candidate_flags(token: str) -> list[str]:
    """Normalise a doc token to the option name(s) it names, or ``[]``."""
    token = token.strip("[](),;…")
    token = token.split("=", 1)[0]
    parts = re.split(r"[/|]", token)
    if len(parts) > 1 and all(p.startswith("-") for p in parts):
        return [p for p in parts if _LONG_FLAG.match(p) or _SHORT_FLAG.match(p)]
    if _LONG_FLAG.match(token) or _SHORT_FLAG.match(token):
        return [token]
    return []


def problems_for(tokens: tuple[str, ...], root: Any) -> list[str]:
    """Resolve one invocation; return human-readable problems (empty = fine)."""
    command, path = root, ["nova"]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _STOP.match(token):
            return []
        children: Mapping[str, Any] | None = subcommands(command)
        if children is None:
            break
        if token.lstrip("[(").startswith("-"):
            for flag in _candidate_flags(token):
                if flag not in _option_names(command):
                    return [f"unknown option {flag} for {' '.join(path)}"]
            flags = _candidate_flags(token)
            if "=" not in token and len(flags) == 1 and _takes_value(command, flags[0]):
                index += 1
            index += 1
            continue
        token = token.rstrip(",.;:")  # prose punctuation: `nova seal verify,`
        if not _WORD.match(token):
            alternatives = re.split(r"[|/]", token)
            if len(alternatives) > 1 and all(_WORD.match(a) for a in alternatives):
                # `nova seal verify|log`, `nova backup create/verify`:
                # shorthand for several commands, and every one must be real.
                return [
                    f"unknown command {' '.join([*path, alt])}"
                    for alt in alternatives
                    if alt not in children
                ]
            if _PLACEHOLDER.match(token) and "@" not in token:
                # <command>, COMMAND, …, $SUB stand in for a sub-command. An
                # `@` makes it an asset reference (`nova eval <name@version>`),
                # which can never be one.
                return []
            # `nova eval my-agent@v1`: a literal argument where a sub-command
            # must go is a real defect, not a placeholder.
            return [f"unknown command {' '.join([*path, token])}"]
        if token not in children:
            return [f"unknown command {' '.join([*path, token])}"]
        command = children[token]
        path.append(token)
        index += 1
    else:
        return []

    settings = getattr(command, "context_settings", None) or {}
    pass_through = settings.get("allow_interspersed_args") is False
    known = _option_names(command)
    found: list[str] = []
    for token in tokens[index:]:
        if _STOP.match(token) or token == "--":
            break
        bare = token.lstrip("[(")
        if bare.startswith("-") and len(bare) > 1 and not bare[1].isdigit():
            for flag in _candidate_flags(token):
                if flag not in known:
                    found.append(f"unknown option {flag} for {' '.join(path)}")
        elif pass_through:
            break
    return found


#: (path, line, invocation text, problem, surrounding prose)
Finding = tuple[str, int, str, str, str]


def collect_problems() -> tuple[list[Finding], int]:
    root = root_command()
    found: list[Finding] = []
    count = 0
    for path in scanned_files():
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        for inv in extract(path, text):
            count += 1
            window = lines[max(0, inv.line - 4) : inv.line + 2]
            context = " ".join(line.lstrip("> ").strip() for line in window)
            for problem in problems_for(inv.tokens, root):
                found.append((inv.path, inv.line, inv.text, problem, context))
    return found, count


@pytest.fixture(scope="module")
def scan() -> tuple[list[Finding], int]:
    return collect_problems()


def test_the_scan_is_not_vacuous(scan: tuple[list[Finding], int]) -> None:
    """Guard the guard: an extractor that finds nothing passes trivially."""
    _, count = scan
    assert count > 500, f"only {count} `nova` invocations extracted from the docs"


@pytest.mark.parametrize(
    ("snippet", "expected"),
    [
        ("```bash\nnova db status\n```", "unknown command nova db status"),
        ("Run `nova policy check` first.", "unknown command nova policy check"),
        ("```bash\nnova doctor --check-cves\n```", "unknown option --check-cves"),
        ("`nova api-proxy --port 9900`", "unknown option --port"),
        ("```bash\nnova eval my-agent@1.0.0\n```", "unknown command nova eval my-agent@1.0.0"),
        ("```bash\nnova seal bypass X [--valid-hours N]\n```", "unknown option --valid-hours"),
        ("`nova backup create/restore`", "unknown command nova backup restore"),
    ],
)
def test_the_checker_rejects_known_drift(snippet: str, expected: str) -> None:
    """Shapes the 2026-10-08 scan flagged; the checker must still see each one."""
    root = root_command()
    problems = [
        p
        for inv in extract(Path("synthetic.md"), snippet)
        for p in problems_for(inv.tokens, root)
    ]
    assert any(expected in p for p in problems), problems


@pytest.mark.parametrize(
    "snippet",
    [
        "```bash\nnova capture -- python train.py --epochs 3\n```",
        "```bash\nnova capture python train.py --epochs 3\n```",
        "```bash\nnova validate $RUN | jq .\n```",
        "`nova --version`",
        "`nova <command> --help`",
        "```bash\nhelm install nova ./deploy/helm/novafabric\n```",
        "```bash\ndocker compose exec nova sh -c 'echo ok'\n```",
        "```bash\n# the run id nova printed\n```",
        "`nova backup create/verify`",
        "`nova classify run --biometrics --no-affects-rights`",
    ],
)
def test_the_checker_accepts_valid_shapes(snippet: str) -> None:
    root = root_command()
    problems = [
        p
        for inv in extract(Path("synthetic.md"), snippet)
        for p in problems_for(inv.tokens, root)
    ]
    assert problems == []


def test_every_documented_invocation_resolves(scan: tuple[list[Finding], int]) -> None:
    found, _ = scan
    unexplained = [
        f"{path}:{line}: {problem}   <- {text}"
        for path, line, text, problem, context in found
        if (path, text) not in ALLOWLIST or not _HONESTY.search(context)
    ]
    assert not unexplained, (
        f"{len(unexplained)} documented `nova` invocation(s) do not match the "
        "real CLI (check `nova <cmd> --help`; fix the doc, or label a genuinely "
        "planned command per the docs honesty rule and add it to ALLOWLIST with "
        "a reason):\n" + "\n".join(unexplained)
    )


def test_allowlist_entries_are_still_needed(scan: tuple[list[Finding], int]) -> None:
    found, _ = scan
    live = {(path, text) for path, _, text, _, _ in found}
    stale = sorted(set(ALLOWLIST) - live)
    assert not stale, f"ALLOWLIST entries that match nothing any more: {stale}"
