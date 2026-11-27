"""Guard: docs/cli-reference.md must cover every top-level `nova` command.

The dashboard command registry is drift-guarded
(tests/serve/test_command_registry_coverage.py), but the Markdown CLI
reference used to drift freely — the 2026-07-16 audit found 26 registered
top-level commands with no reference section. This guard keeps the doc
honest: every visible top-level command (group or leaf) must be mentioned
as ``nova <name>`` somewhere in the reference.

Scope note: deliberately top-level only. Requiring every one of the ~264
leaf paths would force boilerplate sections; a top-level mention with the
group's subcommands documented in its section is the bar the existing doc
style sets.
"""

from __future__ import annotations

import re
from pathlib import Path

from novafabric.cli.introspect import top_level_command_names

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_REFERENCE = REPO_ROOT / "docs" / "cli-reference.md"


def _top_level_commands() -> set[str]:
    return top_level_command_names()


def test_cli_reference_exists() -> None:
    assert CLI_REFERENCE.is_file()


def test_every_top_level_command_is_documented() -> None:
    text = CLI_REFERENCE.read_text(encoding="utf-8")
    missing = sorted(
        name
        for name in _top_level_commands()
        if not re.search(rf"nova {re.escape(name)}\b", text)
    )
    assert not missing, (
        f"{len(missing)} top-level command(s) have no section in "
        f"docs/cli-reference.md: {missing}. Document each new command when "
        "it ships (docs honesty rule) — see the file's existing section style."
    )


def test_no_documented_commands_that_no_longer_exist() -> None:
    """Headings claiming a `nova <cmd>` that the CLI no longer registers."""
    text = CLI_REFERENCE.read_text(encoding="utf-8")
    known = _top_level_commands()
    # Only inspect Markdown headings — prose may legitimately mention
    # historic names, and `#` lines inside code fences are shell comments.
    stale: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line.startswith("#"):
            continue
        for match in re.findall(r"`?nova ([a-z][a-z0-9-]*)\b", line):
            if match not in known:
                stale.append(match)
    assert not stale, (
        f"cli-reference.md headings reference command(s) not in the CLI: "
        f"{sorted(set(stale))}"
    )


# ---------------------------------------------------------------------------
# Leaf coverage — the scope note above says "deliberately top-level only",
# on the reasoning that requiring every leaf would force boilerplate sections.
#
# Measured 2026-09-10: that fear did not materialise. All **263** subcommand
# paths are already documented, across cli-reference and the guides. So this
# holds what the docs have actually achieved rather than demanding new prose —
# a mention of `nova <group> <sub>` anywhere in the user-facing set, which is
# the same bar the top-level guard uses.
#
# ⚠ It looks at the same working tree the CLI is imported from. An earlier
# measurement compared HEAD's *docs* against the working tree's *commands* and
# reported five phantom gaps (`nova dashboard …`) — a feature whose module,
# registration and documentation are all uncommitted together. Comparing two
# different states of the tree invents drift that does not exist.
# ---------------------------------------------------------------------------

USER_FACING = (
    REPO_ROOT / "docs" / "cli-reference.md",
    REPO_ROOT / "docs" / "user-guide.md",
    REPO_ROOT / "docs" / "operator-guide.md",
    REPO_ROOT / "docs" / "getting-started.md",
    REPO_ROOT / "docs" / "developer-guide.md",
    REPO_ROOT / "README.md",
)


def _leaf_command_paths() -> list[str]:
    from typer.main import get_command

    from novafabric.cli.main import app

    def walk(command, path):
        yield path, command
        for name, sub in (getattr(command, "commands", None) or {}).items():
            yield from walk(sub, [*path, name])

    return [" ".join(p) for p, _ in walk(get_command(app), []) if len(p) > 1]


def test_the_command_tree_has_subcommands() -> None:
    """Guard the guard: an empty walk would make the check below vacuous."""
    leaves = _leaf_command_paths()
    assert len(leaves) > 100, f"walked only {len(leaves)} subcommands: {leaves[:5]}"


def test_every_subcommand_is_mentioned_somewhere() -> None:
    text = "\n".join(p.read_text(encoding="utf-8") for p in USER_FACING if p.is_file())
    missing = sorted(p for p in _leaf_command_paths() if f"nova {p}" not in text)
    assert not missing, (
        f"{len(missing)} subcommand(s) are registered but appear in no "
        f"user-facing document: {missing}. A user can run these today and find "
        f"nothing written about them."
    )


# ---------------------------------------------------------------------------
# Flag coverage — every flag the reference shows on a `nova …` invocation must
# exist on that command. The guards above only check command names, so a flag
# that was renamed or removed stayed in the reference and failed for whoever
# copied it (the 2026-11-27 audit found `nova asset diff --output-format text`,
# a value the command never accepted).
#
# Scope: invocations only — a line (fenced or inline code) that names a
# `nova <path>` the CLI registers, followed by `--flag` tokens. Prose that
# mentions a flag without a command is not checked. An option that takes a
# value consumes it (by the option's arity). Scanning stops at `--` (end of
# options) and, for a pass-through command such as `nova capture`, at the first
# positional token, because what follows belongs to the workload. A path that
# stops at a group followed by a word is reported as an unknown subcommand.
# ---------------------------------------------------------------------------

#: Options every command accepts, plus the root's own.
_UNIVERSAL_FLAGS = frozenset(
    {"--help", "-h", "--version", "-V", "--install-completion", "--show-completion"}
)

#: (command path, flag) pairs the reference names on purpose as NOT existing,
#: e.g. "This is not `nova replay --check-equivalence`". Keep each one justified.
_DOCUMENTED_AS_ABSENT = frozenset(
    {
        # replay-equivalence section: explains why the verdict is a separate command.
        ("nova replay", "--check-equivalence"),
    }
)

#: Subcommand paths the reference names on purpose as planned / future design
#: (each sits in a sentence that labels it so). Remove an entry when it ships.
_DOCUMENTED_AS_UNSHIPPED = frozenset(
    {
        "nova memstore mutation",  # ADR-0171 remaining surfaces: planned
        "nova lineage memstore",  # ADR-0171: planned
        "nova assure-baseline list",  # "Not in this slice"
        "nova embodied sensors",  # ADR-0162 remaining surfaces: planned
        "nova lineage embodied",  # ADR-0162: planned
        "nova settlement bind",  # settlement sub-commands: planned
        "nova policy budget",  # ADR-0136 P2/P3: future design
    }
)

_INVOCATION = re.compile(r"(?<![\w/.-])nova((?: +[^\s`|;&#]+)+)")


def _command_index() -> dict[str, object]:
    from novafabric.cli.introspect import root_command, subcommands

    index: dict[str, object] = {}

    def visit(command: object, path: str) -> None:
        index[path] = command
        for name, child in (subcommands(command) or {}).items():
            visit(child, f"{path} {name}")

    visit(root_command(), "nova")
    return index


def _options_of(command: object) -> dict[str, object]:
    """The command's OWN options by spelling (a group's children are not included)."""
    options: dict[str, object] = {}
    for param in getattr(command, "params", []):
        for opt in [*param.opts, *getattr(param, "secondary_opts", [])]:
            if opt.startswith("-"):
                options[opt] = param
    return options


def _value_count(param: object) -> int:
    """How many following tokens the option consumes as its value."""
    if getattr(param, "is_flag", False) or getattr(param, "count", False):
        return 0
    nargs = getattr(param, "nargs", 1)
    return nargs if isinstance(nargs, int) and nargs > 0 else 1


def _passes_through(command: object) -> bool:
    """True when the command collects a variadic workload argument."""
    return any(
        getattr(param, "nargs", 1) == -1 and not getattr(param, "opts", [""])[0].startswith("-")
        for param in getattr(command, "params", [])
    )


_SUBCOMMAND_NAME = re.compile(r"[a-z][a-z0-9-]*")
_FLAG_SPELLING = re.compile(r"--[a-z0-9][a-z0-9-]*|-[A-Za-z]")


def documented_flag_problems(text: str, index: dict[str, object]) -> list[str]:
    """Every ``nova <path> … --flag`` in ``text`` that ``<path>`` does not accept.

    Also reports ``nova <group> <word>`` where ``<word>`` is not a subcommand of
    that group: such a path resolves only as far as the group, and checking its
    flags against the group would hide the typo.
    """
    from novafabric.cli.introspect import subcommands

    problems: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in _INVOCATION.finditer(line):
            raw = match.group(1).split()
            # A parenthetical ends the invocation: "### nova x (experimental)" is prose.
            cut = next((k for k, t in enumerate(raw) if t.startswith("(")), len(raw))
            tokens = [t.strip("[]") for t in raw[:cut]]
            tokens = [t for t in tokens if t]
            path, i = "nova", 0
            while i < len(tokens) and f"{path} {tokens[i]}" in index:
                path, i = f"{path} {tokens[i]}", i + 1
            if path == "nova":
                continue
            command = index[path]
            is_group = subcommands(command) is not None
            if is_group and i < len(tokens) and _SUBCOMMAND_NAME.fullmatch(tokens[i]):
                if f"{path} {tokens[i]}" in _DOCUMENTED_AS_UNSHIPPED:
                    continue
                problems.append(f"line {lineno}: `{path} {tokens[i]}` is not a subcommand")
                continue
            options = _options_of(command)
            passthrough = _passes_through(command)
            skip = 0
            for token in tokens[i:]:
                if skip:
                    skip -= 1
                    continue
                if token == "--":
                    break
                if not token.startswith("-"):
                    if passthrough:
                        break
                    continue
                spelled, has_value = token.split("=", 1)[0].rstrip(",.:"), "=" in token
                flags = [f for f in spelled.split("/") if _FLAG_SPELLING.fullmatch(f)]
                for flag in flags:
                    if flag in _UNIVERSAL_FLAGS or (path, flag) in _DOCUMENTED_AS_ABSENT:
                        continue
                    if flag not in options:
                        problems.append(f"line {lineno}: `{path} {flag}`")
                # A single option that takes a value consumes the next token(s), so a
                # flag written after an option value is still checked.
                if len(flags) == 1 and flags[0] in options and not has_value:
                    skip = _value_count(options[flags[0]])
    return problems


def test_flag_check_catches_a_missing_flag() -> None:
    """Guard the guard: a made-up flag on a real command must be reported."""
    index = _command_index()
    sample = "nova asset diff a@1 b@2 --no-such-flag\nnova capture -- python x.py --anything"
    assert documented_flag_problems(sample, index) == ["line 1: `nova asset diff --no-such-flag`"]


def test_flag_check_reports_a_mistyped_subcommand() -> None:
    """A path that stops at a group must not borrow its children's flags."""
    index = _command_index()
    # --output-format belongs to `nova asset diff`, not to the `nova asset` group.
    assert documented_flag_problems("nova asset dif a@1 b@2 --output-format json", index) == [
        "line 1: `nova asset dif` is not a subcommand"
    ]
    assert documented_flag_problems("nova asset --output-format json", index) == [
        "line 1: `nova asset --output-format`"
    ]


def test_flag_check_skips_option_values_on_pass_through_commands() -> None:
    """An option's value is not the workload: flags after it are still checked."""
    index = _command_index()
    sample = "nova capture --runner kubernetes --bogus -- python x.py"
    assert documented_flag_problems(sample, index) == ["line 1: `nova capture --bogus`"]
    # The workload still ends option scanning.
    assert documented_flag_problems("nova capture python train.py --lr 0.1", index) == []


def test_every_documented_flag_exists_on_its_command() -> None:
    text = CLI_REFERENCE.read_text(encoding="utf-8")
    problems = documented_flag_problems(text, _command_index())
    assert not problems, (
        f"docs/cli-reference.md shows {len(problems)} flag(s) the CLI does not accept: "
        f"{problems}. Fix the reference to the command's current `--help`, or, if the "
        "line deliberately names a flag that does not exist, add it to "
        "_DOCUMENTED_AS_ABSENT with the reason."
    )
