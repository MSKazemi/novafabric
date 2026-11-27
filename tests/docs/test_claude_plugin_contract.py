"""Guard: the public Claude Code plugin installs, and its skills name only real CLI.

The README of ``integrations/claude-plugin/`` told users to run
``/plugin marketplace add MSKazemi/novafabric``. Claude Code reads a marketplace from
``.claude-plugin/marketplace.json`` at the repository root, and the public repository
had none, so the documented install command could not work (issue #243). The skills
also described behaviour the code no longer had: a deploy flow written for the
dashboard while the Helm chart defaults to the REST API server, a
``docker run <image> nova --help`` the image entrypoint never runs, a migration
"stops at v001" while it upgrades to head.

What this module holds, against the documented plugin format
(https://code.claude.com/docs/en/plugins/marketplace-reference and
https://code.claude.com/docs/en/plugins/manifest-reference):

* the root marketplace file exists, has the required fields, and each relative
  ``source`` stays inside the repository and points at a plugin whose
  ``plugin.json`` name equals the entry name (a mismatch breaks
  ``/plugin install <name>@<marketplace>``);
* ``plugin.json`` has a valid name, a version, an author and this repo's licence;
* every skill has ``SKILL.md`` frontmatter with ``name`` equal to its directory and a
  bounded ``description``;
* the plugin ships no hooks, MCP servers, executables or pre-approved tools, so
  installing it grants nothing beyond what each prompt asks for;
* every ``nova …`` command written in a code span or code block of the plugin's
  Markdown resolves to a real command in the CLI tree, and every ``--flag`` written
  after it is one that command accepts.

Each validator is exercised on a deliberately broken fixture too, so a check that
silently stops matching fails here instead of passing vacuously.

Not covered: the real ``claude plugin validate`` / install run (it needs the Claude
Code binary, which CI does not have). The plugin README records that run's output.
"""

from __future__ import annotations

import json
import re
import shlex
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from novafabric.cli.introspect import root_command, subcommands

REPO = Path(__file__).resolve().parents[2]
MARKETPLACE = REPO / ".claude-plugin" / "marketplace.json"
PLUGIN_DIR = REPO / "integrations" / "claude-plugin"

#: Plugin and marketplace names: letters, digits, ".", "_", "-", starting alphanumeric.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
#: Agent Skills frontmatter ``name``: lowercase letters, digits and hyphens, <= 64 chars.
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SKILL_NAME_MAX = 64
SKILL_DESCRIPTION_MAX = 1024
#: Marketplace names Claude Code refuses for a repository outside github.com/anthropics/.
RESERVED_MARKETPLACE_NAMES = frozenset(
    {
        "claude-code-marketplace",
        "claude-code-plugins",
        "claude-plugins-official",
        "anthropic-marketplace",
        "anthropic-plugins",
        "agent-skills",
        "inline",
        "builtin",
        "skills-dir",
        "synced",
    }
)
#: Top-level keys Claude Code reads from marketplace.json; anything else is ignored silently.
MARKETPLACE_KEYS = frozenset(
    {
        "$schema",
        "name",
        "owner",
        "plugins",
        "description",
        "version",
        "metadata",
        "forceRemoveDeletedPlugins",
        "allowCrossMarketplaceDependenciesOn",
        "renames",
    }
)
#: Plugin components that run code or pre-approve tools. The NovaFabric plugin is
#: skills-only; adding one of these is a security-relevant change that needs review.
FORBIDDEN_PLUGIN_PATHS = (
    "hooks",
    ".mcp.json",
    ".lsp.json",
    "bin",
    "monitors",
    "commands",
    "agents",
)
FORBIDDEN_MANIFEST_KEYS = ("hooks", "mcpServers", "lspServers", "commands", "agents", "userConfig")


# ---------------------------------------------------------------------------
# Validators (pure functions: they return a list of problems, empty when valid)
# ---------------------------------------------------------------------------


def marketplace_problems(repo: Path) -> list[str]:
    """Problems in ``<repo>/.claude-plugin/marketplace.json`` and the plugins it lists."""
    path = repo / ".claude-plugin" / "marketplace.json"
    if not path.is_file():
        return [f"missing {path.relative_to(repo)}"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"marketplace.json is not valid JSON: {exc}"]
    if not isinstance(data, dict):
        return ["marketplace.json must be a JSON object"]

    problems: list[str] = []
    unknown = sorted(set(data) - MARKETPLACE_KEYS)
    if unknown:
        problems.append(f"unknown top-level key(s) {unknown}: Claude Code ignores them")
    name = data.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name) or ".." in name:
        problems.append(f"invalid marketplace name {name!r}")
    elif name.lower() in RESERVED_MARKETPLACE_NAMES or name.lower().startswith("claudeai-"):
        problems.append(f"marketplace name {name!r} is reserved")
    owner = data.get("owner")
    if not isinstance(owner, dict) or not str(owner.get("name", "")).strip():
        problems.append("owner.name is required")
    plugins = data.get("plugins")
    if not isinstance(plugins, list) or not plugins:
        return [*problems, "plugins must be a non-empty array"]

    seen: set[str] = set()
    for index, entry in enumerate(plugins):
        problems.extend(_entry_problems(repo, index, entry, seen))
    return problems


def _entry_problems(repo: Path, index: int, entry: Any, seen: set[str]) -> list[str]:
    where = f"plugins[{index}]"
    if not isinstance(entry, dict):
        return [f"{where} must be an object"]
    problems: list[str] = []
    name = entry.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        problems.append(f"{where}.name {name!r} is invalid")
    elif name in seen:
        problems.append(f"{where}.name {name!r} is duplicated")
    else:
        seen.add(name)
    source = entry.get("source")
    if not isinstance(source, str):
        # Remote sources (github, git-subdir, ...) are valid in general, but this repo
        # ships its plugin in-tree; a remote source would install something we never test.
        return [*problems, f"{where}.source must be a relative path into this repository"]
    if not source.startswith("./"):
        problems.append(f"{where}.source {source!r} must start with './'")
    if ".." in Path(source).parts or "\\" in source:
        return [*problems, f"{where}.source {source!r} escapes the marketplace root"]
    plugin_root = (repo / source).resolve()
    if not plugin_root.is_relative_to(repo.resolve()):
        return [*problems, f"{where}.source {source!r} escapes the marketplace root"]
    if not plugin_root.is_dir():
        return [*problems, f"{where}.source {source!r} does not exist"]
    manifest = plugin_root / ".claude-plugin" / "plugin.json"
    if not manifest.is_file():
        return [*problems, f"{where}: {source}/.claude-plugin/plugin.json is missing"]
    problems.extend(f"{where}: {p}" for p in plugin_manifest_problems(plugin_root))
    try:
        manifest_name = json.loads(manifest.read_text(encoding="utf-8")).get("name")
    except (json.JSONDecodeError, AttributeError):
        return problems
    if manifest_name != name:
        problems.append(
            f"{where}.name {name!r} != plugin.json name {manifest_name!r}: "
            f"`/plugin install {manifest_name}@<marketplace>` would not find it"
        )
    return problems


def plugin_manifest_problems(plugin_root: Path) -> list[str]:
    """Problems in ``<plugin_root>/.claude-plugin/plugin.json`` and its permissions surface."""
    path = plugin_root / ".claude-plugin" / "plugin.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ["missing .claude-plugin/plugin.json"]
    except json.JSONDecodeError as exc:
        return [f"plugin.json is not valid JSON: {exc}"]
    if not isinstance(data, dict):
        return ["plugin.json must be a JSON object"]
    problems: list[str] = []
    name = data.get("name")
    if not isinstance(name, str) or not SKILL_NAME_RE.match(name):
        problems.append(f"plugin name {name!r} must be kebab-case")
    elif re.match(r"^(claude|anthropic|anthropics|cc-plugin)(-|$)", name):
        problems.append(f"plugin name {name!r} is reserved")
    if not isinstance(data.get("version"), str) or not data["version"].strip():
        problems.append("plugin.json version is missing")
    if not isinstance(data.get("description"), str) or not data["description"].strip():
        problems.append("plugin.json description is missing")
    author = data.get("author")
    if not isinstance(author, dict) or not str(author.get("name", "")).strip():
        problems.append("plugin.json author.name is missing")
    for key in FORBIDDEN_MANIFEST_KEYS:
        if key in data:
            problems.append(f"plugin.json declares {key!r}: the plugin is skills-only")
    for rel in FORBIDDEN_PLUGIN_PATHS:
        if (plugin_root / rel).exists():
            problems.append(f"plugin ships {rel!r}: the plugin is skills-only")
    return problems


def skill_problems(plugin_root: Path) -> list[str]:
    """Problems in each ``skills/<name>/SKILL.md`` frontmatter."""
    skills_dir = plugin_root / "skills"
    dirs = sorted(p for p in skills_dir.iterdir() if p.is_dir()) if skills_dir.is_dir() else []
    if not dirs:
        return ["plugin has no skills/<name>/ directories"]
    problems: list[str] = []
    for skill_dir in dirs:
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            problems.append(f"{skill_dir.name}: SKILL.md is missing")
            continue
        text = skill_md.read_text(encoding="utf-8")
        match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        if match is None:
            problems.append(f"{skill_dir.name}: SKILL.md has no YAML frontmatter")
            continue
        try:
            meta = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            problems.append(f"{skill_dir.name}: frontmatter is not valid YAML: {exc}")
            continue
        if not isinstance(meta, dict):
            problems.append(f"{skill_dir.name}: frontmatter must be a mapping")
            continue
        name = meta.get("name")
        if name != skill_dir.name:
            problems.append(f"{skill_dir.name}: frontmatter name {name!r} != directory name")
        elif not SKILL_NAME_RE.match(name) or len(name) > SKILL_NAME_MAX:
            problems.append(f"{skill_dir.name}: invalid skill name {name!r}")
        description = meta.get("description")
        if not isinstance(description, str) or not description.strip():
            problems.append(f"{skill_dir.name}: description is missing")
        elif len(description) > SKILL_DESCRIPTION_MAX:
            problems.append(
                f"{skill_dir.name}: description is {len(description)} chars "
                f"(max {SKILL_DESCRIPTION_MAX})"
            )
        if "allowed-tools" in meta:
            problems.append(f"{skill_dir.name}: pre-approves tools via allowed-tools")
    return problems


# ---------------------------------------------------------------------------
# `nova …` commands written in the plugin's Markdown
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"^\s*(```|~~~)")
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_NOVA = re.compile(r"(?<![\w/.:-])nova\s+")
_COMMAND_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
_STOP = re.compile(r"[|;&#]")


def code_segments(markdown: str) -> Iterator[str]:
    """Each fenced-block line (continuations joined) and each inline code span."""
    in_fence = False
    pending = ""
    for line in markdown.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            pending = ""
            continue
        if in_fence:
            if line.rstrip().endswith("\\"):
                pending += line.rstrip()[:-1] + " "
                continue
            yield pending + line
            pending = ""
        else:
            yield from _CODE_SPAN.findall(line)


def nova_invocations(segment: str) -> Iterator[list[str]]:
    """Token lists for every ``nova …`` invocation inside one code segment.

    A shell comment is searched too (it often holds an alternative command), and an
    invocation ends at a shell operator or the start of a comment.
    """
    for match in _NOVA.finditer(segment):
        rest = segment[match.end() :]
        stop = _STOP.search(rest)
        if stop is not None:
            rest = rest[: stop.start()]
        try:
            tokens = shlex.split(rest)
        except ValueError:
            tokens = rest.split()
        yield tokens


def _option_names(command: Any) -> set[str]:
    names = {"--help"}
    for param in getattr(command, "params", []):
        if getattr(param, "param_type_name", None) == "option":
            names.update(param.opts)
            names.update(param.secondary_opts)
    return names


def _is_variadic(command: Any) -> bool:
    return any(
        getattr(p, "param_type_name", None) == "argument" and p.nargs == -1
        for p in getattr(command, "params", [])
    )


def invocation_problems(tokens: list[str], root: Any) -> list[str]:
    """Why ``nova <tokens>`` does not name a real command and real flags (empty if it does)."""
    command, path, i = root, ["nova"], 0
    while i < len(tokens):
        children = subcommands(command)
        token = tokens[i]
        if children is None or token.startswith("-"):
            break
        if not _COMMAND_NAME.match(token):
            if command is root:
                return []  # e.g. `helm install nova oci://…` — not a nova invocation
            break
        if token not in children:
            return [f"`{' '.join(path)} {token}`: no such command"]
        command = children[token]
        path.append(token)
        i += 1
    options = _option_names(command)
    variadic = _is_variadic(command)
    problems: list[str] = []
    for token in tokens[i:]:
        if token == "--":
            break
        if not token.startswith("-") or token == "-":
            if variadic:
                break  # the rest is the wrapped command's own argv
            continue
        flag = token.split("=", 1)[0]
        if flag not in options:
            problems.append(f"`{' '.join(path)}` has no option {flag}")
    return problems


def markdown_command_problems(markdown: str, root: Any) -> list[str]:
    problems: list[str] = []
    for segment in code_segments(markdown):
        for tokens in nova_invocations(segment):
            problems.extend(invocation_problems(tokens, root))
    return problems


def plugin_markdown_files() -> list[Path]:
    return sorted(PLUGIN_DIR.rglob("*.md"))


# ---------------------------------------------------------------------------
# The real plugin
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def nova_root() -> Any:
    return root_command()


def test_root_marketplace_lists_the_in_tree_plugin() -> None:
    assert marketplace_problems(REPO) == []
    data = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    sources = [entry["source"] for entry in data["plugins"]]
    assert "./integrations/claude-plugin" in sources


def test_plugin_manifest_is_valid_and_skills_only() -> None:
    assert plugin_manifest_problems(PLUGIN_DIR) == []


def test_plugin_licence_and_repository_match_this_project() -> None:
    manifest = json.loads((PLUGIN_DIR / ".claude-plugin" / "plugin.json").read_text("utf-8"))
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert manifest["license"] == project["license"]["text"]
    assert manifest["repository"] == "https://github.com/MSKazemi/novafabric"
    assert manifest["homepage"].startswith("https://github.com/MSKazemi/novafabric")


def test_every_skill_has_valid_frontmatter() -> None:
    assert skill_problems(PLUGIN_DIR) == []


@pytest.mark.parametrize(
    "path", plugin_markdown_files(), ids=lambda p: str(p.relative_to(PLUGIN_DIR))
)
def test_every_nova_command_in_the_plugin_exists(path: Path, nova_root: Any) -> None:
    problems = markdown_command_problems(path.read_text(encoding="utf-8"), nova_root)
    assert not problems, f"{path.relative_to(REPO)}:\n" + "\n".join(problems)


def test_the_plugin_markdown_names_nova_commands_at_all(nova_root: Any) -> None:
    """Non-vacuity: the scanner finds the commands the skills are built around."""
    found: set[str] = set()
    for path in plugin_markdown_files():
        for segment in code_segments(path.read_text(encoding="utf-8")):
            for tokens in nova_invocations(segment):
                if tokens and _COMMAND_NAME.match(tokens[0]):
                    found.add(tokens[0])
    for expected in ("init", "capture", "validate", "verify", "replay", "seal", "serve", "server"):
        assert expected in found, f"scanner no longer sees `nova {expected}` in the plugin"


# ---------------------------------------------------------------------------
# Negative cases: every validator must reject the defect it exists to catch
# ---------------------------------------------------------------------------


def _write_plugin(root: Path, *, name: str = "demo", skill: str = "demo-skill") -> Path:
    plugin = root / "plugins" / "demo"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "0.1.0", "description": "d", "author": {"name": "a"}}),
        encoding="utf-8",
    )
    (plugin / "skills" / skill).mkdir(parents=True)
    (plugin / "skills" / skill / "SKILL.md").write_text(
        f"---\nname: {skill}\ndescription: does a thing\n---\n\n# body\n", encoding="utf-8"
    )
    return plugin


def _write_marketplace(root: Path, data: Any) -> None:
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    text = data if isinstance(data, str) else json.dumps(data)
    (root / ".claude-plugin" / "marketplace.json").write_text(text, encoding="utf-8")


def _marketplace(source: str = "./plugins/demo", name: str = "demo") -> dict[str, Any]:
    return {
        "name": "demo-market",
        "owner": {"name": "o"},
        "plugins": [{"name": name, "source": source}],
    }


def test_a_valid_fixture_passes(tmp_path: Path) -> None:
    _write_plugin(tmp_path)
    _write_marketplace(tmp_path, _marketplace())
    assert marketplace_problems(tmp_path) == []


def test_a_missing_marketplace_file_is_reported(tmp_path: Path) -> None:
    assert marketplace_problems(tmp_path) == ["missing .claude-plugin/marketplace.json"]


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("{not json", "not valid JSON"),
        ({"owner": {"name": "o"}, "plugins": []}, "invalid marketplace name"),
        ({"name": "claude-plugins-official", "owner": {"name": "o"}, "plugins": [{}]}, "reserved"),
        ({"name": "m", "plugins": [{"name": "demo", "source": "./plugins/demo"}]}, "owner.name"),
        ({"name": "m", "owner": {"name": "o"}, "plugins": []}, "non-empty array"),
        ({**_marketplace(), "extra": 1}, "unknown top-level key"),
        (_marketplace(source="plugins/demo"), "must start with './'"),
        (_marketplace(source="./../demo"), "escapes the marketplace root"),
        (_marketplace(source="./plugins/missing"), "does not exist"),
        (_marketplace(name="other"), "!= plugin.json name"),
        (
            {
                "name": "m",
                "owner": {"name": "o"},
                "plugins": [{"name": "demo", "source": {"source": "github"}}],
            },
            "relative path into this repository",
        ),
    ],
)
def test_an_invalid_marketplace_is_rejected(tmp_path: Path, data: Any, expected: str) -> None:
    _write_plugin(tmp_path)
    _write_marketplace(tmp_path, data)
    problems = marketplace_problems(tmp_path)
    assert any(expected in p for p in problems), problems


def test_a_plugin_without_a_manifest_is_rejected(tmp_path: Path) -> None:
    plugin = _write_plugin(tmp_path)
    (plugin / ".claude-plugin" / "plugin.json").unlink()
    _write_marketplace(tmp_path, _marketplace())
    assert any("plugin.json is missing" in p for p in marketplace_problems(tmp_path))


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda p: (p / "hooks").mkdir(), "ships 'hooks'"),
        (lambda p: (p / ".mcp.json").write_text("{}", encoding="utf-8"), "ships '.mcp.json'"),
        (lambda p: (p / "bin").mkdir(), "ships 'bin'"),
    ],
)
def test_a_plugin_that_runs_code_is_rejected(tmp_path: Path, mutate: Any, expected: str) -> None:
    plugin = _write_plugin(tmp_path)
    mutate(plugin)
    assert any(expected in p for p in plugin_manifest_problems(plugin))


@pytest.mark.parametrize(
    ("manifest", "expected"),
    [
        (
            {"name": "Demo Plugin", "version": "1", "description": "d", "author": {"name": "a"}},
            "kebab-case",
        ),
        (
            {"name": "claude-demo", "version": "1", "description": "d", "author": {"name": "a"}},
            "reserved",
        ),
        ({"name": "demo", "description": "d", "author": {"name": "a"}}, "version"),
        ({"name": "demo", "version": "1", "description": "d"}, "author.name"),
        (
            {
                "name": "demo",
                "version": "1",
                "description": "d",
                "author": {"name": "a"},
                "hooks": {},
            },
            "declares 'hooks'",
        ),
    ],
)
def test_an_invalid_plugin_manifest_is_rejected(
    tmp_path: Path, manifest: dict[str, Any], expected: str
) -> None:
    plugin = _write_plugin(tmp_path)
    (plugin / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert any(expected in p for p in plugin_manifest_problems(plugin))


@pytest.mark.parametrize(
    ("frontmatter", "expected"),
    [
        ("name: wrong-name\ndescription: d", "!= directory name"),
        ("name: demo-skill", "description is missing"),
        (f"name: demo-skill\ndescription: {'x' * 1025}", "max 1024"),
        ("name: demo-skill\ndescription: d\nallowed-tools: Bash", "allowed-tools"),
    ],
)
def test_an_invalid_skill_is_rejected(tmp_path: Path, frontmatter: str, expected: str) -> None:
    plugin = _write_plugin(tmp_path)
    (plugin / "skills" / "demo-skill" / "SKILL.md").write_text(
        f"---\n{frontmatter}\n---\n", encoding="utf-8"
    )
    assert any(expected in p for p in skill_problems(plugin))


def test_a_skill_without_frontmatter_is_rejected(tmp_path: Path) -> None:
    plugin = _write_plugin(tmp_path)
    (plugin / "skills" / "demo-skill" / "SKILL.md").write_text(
        "# no frontmatter\n", encoding="utf-8"
    )
    assert any("no YAML frontmatter" in p for p in skill_problems(plugin))


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("Run `nova no-such-command <dir>`.", "nova no-such-command`: no such command"),
        ("```bash\nnova seal no-such-sub\n```", "nova seal no-such-sub`: no such command"),
        ("```bash\nnova replay <dir> --no-such-flag\n```", "has no option --no-such-flag"),
        ("```bash\nnova export-evidence <dir> \\\n  --nope out.zip\n```", "has no option --nope"),
        ("```bash\npip install x  # then: nova bogus\n```", "nova bogus`: no such command"),
    ],
)
def test_an_unknown_command_or_flag_is_reported(
    markdown: str, expected: str, nova_root: Any
) -> None:
    problems = markdown_command_problems(markdown, nova_root)
    assert any(expected in p for p in problems), problems


@pytest.mark.parametrize(
    "markdown",
    [
        "```bash\nnova capture -- python agent.py --their-own-flag\n```",
        "```bash\nnova capture python3 agent.py --their-own-flag\n```",
        "```bash\nhelm install nova oci://ghcr.io/example/charts/novafabric --version 1.0.0\n```",
        "`docker run --rm --entrypoint nova example/image:1.0 --help`",
        "```bash\nkubectl rollout status deploy/nova-novafabric\n```",
        "Run `nova replay <dir> --mode forensic` then `nova seal init`.",
    ],
)
def test_real_commands_and_non_nova_text_pass(markdown: str, nova_root: Any) -> None:
    assert markdown_command_problems(markdown, nova_root) == []
