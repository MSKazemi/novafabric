"""L9: an install hint in ``--help`` must keep its extra.

Rich reads ``[server]`` as a markup tag and drops it, so the help text
``Requires novafabric[server] and …`` rendered as ``Requires novafabric and …``
— the one token that makes the instruction work, silently gone. Escaping the
bracket (``novafabric\\[server]``) keeps it. This walks every command, finds each
``novafabric[<extra>]`` its raw help names (command help and every option's help),
and asserts the rendered ``--help`` still shows it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

#: ``novafabric[extra]`` or the escaped ``novafabric\[extra]`` in source text.
_EXTRA_IN_SOURCE = re.compile(r"novafabric\\?\[(?P<extra>[a-z0-9_,-]+)\]")


def _walk(command: Any, path: list[str]) -> Iterator[tuple[list[str], Any]]:
    yield path, command
    for name, sub in (getattr(command, "commands", None) or {}).items():
        yield from _walk(sub, [*path, name])


def _raw_help(command: Any) -> str:
    parts: list[Any] = [command.help, command.short_help]
    parts.extend(getattr(p, "help", None) for p in command.params)
    return "\n".join(p for p in parts if isinstance(p, str))


def _cases() -> list[tuple[str, tuple[str, ...]]]:
    cases = []
    for path, command in _walk(get_command(app), []):
        extras = tuple(sorted({m["extra"] for m in _EXTRA_IN_SOURCE.finditer(_raw_help(command))}))
        if path and extras:
            cases.append((" ".join(path), extras))
    return cases


CASES = _cases()


def _flat(text: str) -> str:
    """Rendered help with the Rich panel borders and all whitespace removed, so a
    hint wrapped across lines still matches."""
    return re.sub(r"[\s│╭╮╰╯─]+", "", text)


def test_the_scan_is_not_vacuous() -> None:
    paths = {p for p, _ in CASES}
    assert len(CASES) >= 5, CASES
    assert "migrate-to-postgres" in paths
    assert "kg" in paths


@pytest.mark.parametrize(("command_path", "extras"), CASES, ids=[c[0] for c in CASES])
def test_rendered_help_keeps_every_extra(command_path: str, extras: tuple[str, ...]) -> None:
    result = runner.invoke(app, [*command_path.split(" "), "--help"], terminal_width=200)
    assert result.exit_code == 0, result.output
    rendered = _flat(result.output)
    lost = [e for e in extras if f"novafabric[{e}]" not in rendered]
    assert not lost, (
        f"`nova {command_path} --help` dropped the extra(s) {lost}: Rich ate "
        "'[extra]' as markup. Escape the bracket as r'novafabric\\[extra]'."
    )


def test_migrate_to_postgres_help_names_the_env_var_the_option_reads() -> None:
    migrate = get_command(app).commands["migrate-to-postgres"]  # type: ignore[attr-defined]
    envvars = {p.envvar for p in migrate.params if getattr(p, "envvar", None)}
    assert envvars == {"NOVAFABRIC_POSTGRES_DSN"}
    result = runner.invoke(app, ["migrate-to-postgres", "--help"], terminal_width=200)
    assert "NOVAFABRIC_DB_URL" not in result.output
    assert "NOVAFABRIC_POSTGRES_DSN" in result.output


#: Any ``[token]`` Rich could read as a tag: a letter or ``/`` after the bracket,
#: escaped (``\[`` — already fixed, kept as a regression case) or not.
_TAG_LIKE = re.compile(r"\\?\[(?P<tok>[A-Za-z/][^\[\]\n]{0,40})\]")


def _bracket_cases() -> list[tuple[str, tuple[str, ...]]]:
    cases = []
    for path, command in _walk(get_command(app), []):
        toks = tuple(sorted({m["tok"] for m in _TAG_LIKE.finditer(_raw_help(command))}))
        if path and toks:
            cases.append((" ".join(path), toks))
    return cases


BRACKET_CASES = _bracket_cases()


def test_the_bracket_scan_is_not_vacuous() -> None:
    paths = {p for p, _ in BRACKET_CASES}
    assert {"query", "prompt tree", "view save", "export-rai-scorecard"} <= paths, paths


@pytest.mark.parametrize(
    ("command_path", "tokens"), BRACKET_CASES, ids=[c[0] for c in BRACKET_CASES]
)
def test_no_bracketed_help_text_is_eaten(command_path: str, tokens: tuple[str, ...]) -> None:
    """The same defect for any bracketed text (``[asc|desc]``, ``[label]``, ``[refs]``)."""
    result = runner.invoke(app, [*command_path.split(" "), "--help"], terminal_width=200)
    rendered = _flat(result.output)
    lost = [t for t in tokens if "[" + re.sub(r"\s+", "", t) + "]" not in rendered]
    assert not lost, f"`nova {command_path} --help` dropped {lost}; escape '[' as '\\['."
