"""Guard test: the `age`-profile Makefile recipes are pinned to reviewed text.

Companion to `tests/deploy/test_image_pins.py` and
`tests/deploy/test_compose_profiles.py` — same enforcement pattern ("the test
is the enforcement, the doc/comment is the documentation"), applied to a
different real incident.

## The incident this guards against

`docker compose -f deploy/docker/docker-compose.yml --profile age config
--services` resolves to the *union* `{age, postgres, nova}` — `postgres` and
`nova` carry no `profiles:` key of their own, so they are always "active"
under any profile filter, `age` included. A `docker compose --profile age
<verb>` invocation with **no explicit service name** therefore operates on
that whole union, not on `age` alone.

During S5 development, a first draft of `make age-down` was literally
`$(COMPOSE_AGE) down` — no service name. Verified live: it stopped and
removed an already-running, unrelated dev stack's `novafabric-postgres` and
`novafabric-serve` containers. A near-identical first draft of the
integration test's teardown (`docker compose --profile age down -v`) went
further and deleted that stack's `pg-data`/`kuzu-data` named volumes — real,
unrecoverable data loss on a shared host. See the S5 task report.

## How this guard works — two checks, no shell parsing

Earlier revisions of this file tried to *understand* the recipe lines: a
staged parser with a Make-variable expander, a quote-aware splitter, a shlex
tokenizer, flag-value tables and bounded `sh -c` recursion. Every review
round found a new way to fool it (`${VAR}` vs `$(VAR)`, prose inside an
`@echo`, a trailing `# comment` whose text happens to contain the word
"age", …). Chasing full shell + Make semantics with a text parser has no
convergence point, and this project's prime directive is explicit: never move
fast by creating unreviewable complexity. So the parser is gone. Two cheap
checks replace it, and between them they are structurally complete:

1. **`check_approved_age_recipes` — pins modification.** The recipe lines of
   every `age`-profile target must match `_APPROVED_AGE_RECIPES` exactly,
   normalizing only trivia (the leading tab, surrounding whitespace, blank
   lines). Any edit fails, forcing a human to consciously re-approve it.
   There is no shell syntax to outsmart, because nothing here interprets the
   shell — the assertion is just that the bytes are the reviewed bytes.

2. **`check_no_unapproved_age_references` — catches addition.** No recipe
   line outside those pinned targets may reference the `age` profile at all
   (`--profile age`, `COMPOSE_PROFILES=…age…`, `COMPOSE_AGE`, in `$()` or
   `${}` form, plus any variable that transitively aliases one — a coarse
   substring scan, not a parse). A new age-profile target fails this and is
   told to get reviewed and added to the allowlist.

Modification is caught by (1), addition by (2), and neither can be bypassed
by comments, quoting, variable indirection, line continuations, `sh -c`, or
any other shell cleverness — because neither one parses shell. Check 2 is
deliberately over-broad: a false positive costs someone thirty seconds and a
considered allowlist entry; a false negative cost this project a production
incident.

## The one real limitation

Only this single `Makefile` is read. A compose command living in an
`include`d makefile, or inside a script that a recipe invokes
(`./scripts/foo.sh`), is outside what these checks can see.

Pure text test — it never starts a child process, unlike the opt-in
`tests/deploy/test_age_compose_integration.py`. Runs by default.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"

# The reviewed, approved recipe text for every `age`-profile target. Every
# command names the `age` service explicitly; none is ever a bare `up`/`down`.
# Changing a line here is a deliberate act that must be re-reviewed against
# the union-activation hazard described in `_INCIDENT_NOTE` below.
_APPROVED_AGE_RECIPES: dict[str, tuple[str, ...]] = {
    "age-up": (
        "$(COMPOSE_AGE) up -d age",
        '@echo "novafabric-age is up: '
        'postgresql://nova:nova@localhost:5433/nova_lineage"',
    ),
    "age-down": (
        "$(COMPOSE_AGE) stop age",
        "$(COMPOSE_AGE) rm -f age",
    ),
}

# Coarse, deliberately over-broad markers for "this line touches the `age`
# compose profile". Substring matching only — no tokenizing, no shell
# semantics, nothing to outsmart.
_AGE_MARKER_RE = re.compile(
    r"--profile[=\s]+age\b|COMPOSE_PROFILES=\S*\bage\b|COMPOSE_AGE"
)

# A Make target line: not a recipe (no leading tab), not a comment, and not a
# `NAME := value` assignment (the `(?!=)` rejects `:=`/`::=`).
_TARGET_LINE_RE = re.compile(r"^([^\t#\s][^:=]*):(?!=)")

# `NAME := / ::= / ?= / += / = value` — the assignment forms GNU Make supports.
_ASSIGNMENT_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?::=|::=|\?=|\+=|=)\s*(.*)$"
)

_INCIDENT_NOTE = (
    "A bare (service-unscoped) `docker compose --profile age <verb>` targets the "
    "UNION of every service the `age` profile activates -- {age, postgres, nova} "
    "-- not just `age`, because `postgres`/`nova` carry no `profiles:` key of "
    "their own and are therefore always \"active\" under any profile filter. "
    "This is exactly the shape that caused a real incident during S5 "
    "development: an unscoped `$(COMPOSE_AGE) down` / `docker compose --profile "
    "age down -v` stopped/removed an already-running dev stack's "
    "novafabric-postgres/novafabric-serve containers and deleted their "
    "pg-data/kuzu-data volumes. Every Makefile command touching the `age` "
    "profile must name the `age` service explicitly (e.g. `up -d age`, `stop "
    "age`, `rm -f age`)."
)

_PIN_NOTE = (
    "The text in _APPROVED_AGE_RECIPES is pinned precisely because it is the "
    "text that was reviewed against that hazard: every command names `age` "
    "explicitly, and none is a bare `up`/`down`. If you are intentionally "
    "changing these recipes, check the new command against the hazard FIRST, "
    "then update _APPROVED_AGE_RECIPES to match -- do not delete this "
    "assertion, and do not re-pin whatever you happened to type without "
    "checking it."
)

_ADD_NOTE = (
    "A recipe line outside the approved `age` targets references the `age` "
    "compose profile. If this is a legitimate new target, verify every command "
    "on it names the `age` service explicitly, then add the target and its "
    "reviewed recipe text to _APPROVED_AGE_RECIPES in this file so it is pinned "
    "too. This check is deliberately over-broad: a false positive costs you one "
    "allowlist entry, a false negative cost this project a production incident."
)


def _recipe_blocks(
    makefile_text: str,
) -> list[tuple[frozenset[str], list[tuple[int, str]]]]:
    """Group the Makefile's recipe lines under the target they belong to.

    Returns `(target_names, [(lineno, stripped_line), …])` per rule. This
    reads Make's *block structure* only — a leading tab means "recipe line",
    a `name:` line starts a new rule. It never looks inside a recipe line.
    Blank lines are dropped and never end a block; any other non-tab line
    that is not a target line (a comment, a directive) ends the current
    block, so an orphaned recipe line belongs to no target and is therefore
    treated as unapproved by check 2.
    """
    blocks: list[tuple[frozenset[str], list[tuple[int, str]]]] = []
    current: list[tuple[int, str]] | None = None
    continued = False
    for lineno, raw in enumerate(makefile_text.splitlines(), start=1):
        if raw.startswith("\t"):
            body = raw.strip()
            if not body:
                continue
            if current is None:
                blocks.append((frozenset(), []))
                current = blocks[-1][1]
            current.append((lineno, body))
            continue
        if not raw.strip():
            continue
        was_continued, continued = continued, raw.rstrip().endswith("\\")
        if was_continued:
            continue
        match = _TARGET_LINE_RE.match(raw)
        if match:
            blocks.append((frozenset(match.group(1).split()), []))
            current = blocks[-1][1]
        else:
            current = None
    return blocks


def _age_alias_variables(makefile_text: str) -> set[str]:
    """Names of variables whose definition transitively reaches the profile.

    Seeded with every variable whose right-hand side carries an `age` marker,
    then extended to anything referencing one of those, to a fixed point.
    Substring matching over assignment text — this is not an expander, and it
    evaluates nothing.
    """
    assignments: dict[str, str] = {}
    for raw in makefile_text.splitlines():
        if raw.startswith("\t") or raw.lstrip().startswith("#"):
            continue
        match = _ASSIGNMENT_RE.match(raw)
        if match:
            name, rhs = match.group(1), match.group(2)
            assignments[name] = assignments.get(name, "") + " " + rhs

    aliases = {n for n, rhs in assignments.items() if _AGE_MARKER_RE.search(rhs)}
    changed = True
    while changed:
        changed = False
        for name, rhs in assignments.items():
            if name in aliases:
                continue
            if any(f"$({a})" in rhs or f"${{{a}}}" in rhs for a in aliases):
                aliases.add(name)
                changed = True
    return aliases


def _references_age_profile(line: str, aliases: set[str]) -> bool:
    """Coarse "does this line touch the age profile" test."""
    if _AGE_MARKER_RE.search(line):
        return True
    return any(f"$({a})" in line or f"${{{a}}}" in line for a in aliases)


def check_approved_age_recipes(makefile_text: str) -> list[str]:
    """Check 1: every approved `age` target's recipe matches the pinned text."""
    blocks = _recipe_blocks(makefile_text)
    problems: list[str] = []
    for target, expected in _APPROVED_AGE_RECIPES.items():
        matching = [lines for names, lines in blocks if target in names]
        if not matching:
            problems.append(
                f"Target `{target}` is in _APPROVED_AGE_RECIPES but no longer "
                f"exists in the Makefile. If it was deliberately removed or "
                f"renamed, drop/update its entry here in the same change."
            )
            continue
        if len(matching) > 1:
            problems.append(
                f"Target `{target}` has {len(matching)} rules with recipes; "
                f"this guard pins a single reviewed recipe per target."
            )
        actual = tuple(line for _, line in matching[0])
        if actual != expected:
            problems.append(
                f"Recipe for `{target}` no longer matches its approved text.\n"
                f"  approved: {list(expected)}\n"
                f"  found:    {list(actual)}"
            )
    return problems


def check_no_unapproved_age_references(makefile_text: str) -> list[str]:
    """Check 2: no recipe outside the approved targets touches the profile."""
    aliases = _age_alias_variables(makefile_text)
    approved = set(_APPROVED_AGE_RECIPES)
    problems: list[str] = []
    for names, lines in _recipe_blocks(makefile_text):
        if names & approved:
            continue
        for lineno, line in lines:
            if _references_age_profile(line, aliases):
                target = "/".join(sorted(names)) if names else "<no target>"
                problems.append(
                    f"Makefile:{lineno}: recipe line {line!r} in target "
                    f"`{target}` references the `age` compose profile but is "
                    f"not an approved, pinned `age` recipe."
                )
    return problems


# --------------------------------------------------------------------------
# The invariants, against the real Makefile
# --------------------------------------------------------------------------


def test_age_recipes_match_their_approved_text() -> None:
    problems = check_approved_age_recipes(MAKEFILE.read_text(encoding="utf-8"))
    assert not problems, (
        _INCIDENT_NOTE + "\n\n" + _PIN_NOTE + "\n\n" + "\n".join(problems)
    )


def test_no_unapproved_age_profile_references() -> None:
    problems = check_no_unapproved_age_references(MAKEFILE.read_text(encoding="utf-8"))
    assert not problems, (
        _INCIDENT_NOTE + "\n\n" + _ADD_NOTE + "\n\n" + "\n".join(problems)
    )


# --------------------------------------------------------------------------
# Controls — synthetic text only; the real Makefile is never edited
# --------------------------------------------------------------------------


def _synthetic_makefile(
    *,
    age_up: tuple[str, ...] | None = None,
    age_down: tuple[str, ...] | None = None,
    extra: str = "",
) -> str:
    """Render a synthetic Makefile, approved by default."""
    up = _APPROVED_AGE_RECIPES["age-up"] if age_up is None else age_up
    down = _APPROVED_AGE_RECIPES["age-down"] if age_down is None else age_down
    body = (
        "COMPOSE_AGE  := docker compose -f deploy/docker/docker-compose.yml"
        " --profile age\n\n"
    )
    if up:
        body += "age-up:\n" + "".join(f"\t{line}\n" for line in up) + "\n"
    if down:
        body += "age-down:\n" + "".join(f"\t{line}\n" for line in down) + "\n"
    return body + extra


def test_synthetic_baseline_is_clean() -> None:
    """The synthetic fixture must itself pass, or every control below lies."""
    text = _synthetic_makefile()
    assert check_approved_age_recipes(text) == []
    assert check_no_unapproved_age_references(text) == []


def test_pin_catches_a_recipe_reverted_to_a_bare_down() -> None:
    """Negative control for check 1: the exact original incident shape."""
    text = _synthetic_makefile(age_down=("$(COMPOSE_AGE) down",))
    problems = check_approved_age_recipes(text)
    assert len(problems) == 1, problems
    assert "age-down" in problems[0]


def test_pin_catches_a_comment_suffixed_bypass() -> None:
    """A trailing shell comment mentioning `age` no longer buys anything.

    The previous parser was fooled by exactly this: `$(COMPOSE_AGE) down
    # matches only the age service` scanned as "an `age` token follows the
    verb" and passed, while the command actually executed was an unscoped
    `down`. Pinning cannot be fooled by it — the bytes simply differ.
    """
    text = _synthetic_makefile(
        age_down=(
            "$(COMPOSE_AGE) down  # matches only the age service per profile scoping",
        )
    )
    assert len(check_approved_age_recipes(text)) == 1


def test_pin_catches_an_extra_command_appended_to_an_approved_recipe() -> None:
    text = _synthetic_makefile(
        age_down=_APPROVED_AGE_RECIPES["age-down"] + ("$(COMPOSE_AGE) down -v",)
    )
    assert len(check_approved_age_recipes(text)) == 1


def test_pin_catches_a_deleted_age_target() -> None:
    """Deleting a pinned target must fail, not silently stop protecting."""
    text = _synthetic_makefile(age_down=())
    problems = check_approved_age_recipes(text)
    assert len(problems) == 1, problems
    assert "no longer exists" in problems[0]


def test_pin_normalizes_only_whitespace_trivia() -> None:
    """Trailing spaces and interleaved blank lines are not a recipe change."""
    text = (
        "COMPOSE_AGE  := docker compose -f deploy/docker/docker-compose.yml"
        " --profile age\n"
        "age-up:\n"
        "\t$(COMPOSE_AGE) up -d age   \n"
        "\n"
        '\t@echo "novafabric-age is up: '
        'postgresql://nova:nova@localhost:5433/nova_lineage"\n'
        "\n"
        "age-down:\n"
        "\t  $(COMPOSE_AGE) stop age\n"
        "\t$(COMPOSE_AGE) rm -f age\t\n"
    )
    assert check_approved_age_recipes(text) == []


def test_scan_catches_a_new_unscoped_age_target() -> None:
    """Negative control for check 2: a brand-new age-profile recipe."""
    text = _synthetic_makefile(extra="age-nuke:\n\t$(COMPOSE_AGE) down -v\n")
    assert check_approved_age_recipes(text) == []  # the pinned targets are fine
    problems = check_no_unapproved_age_references(text)
    assert len(problems) == 1, problems
    assert "age-nuke" in problems[0]


def test_scan_catches_a_new_target_even_when_correctly_scoped() -> None:
    """Over-broad on purpose: even a *safe* new age recipe needs review."""
    text = _synthetic_makefile(extra="age-restart:\n\t$(COMPOSE_AGE) restart age\n")
    assert len(check_no_unapproved_age_references(text)) == 1


def test_scan_catches_a_literal_profile_flag() -> None:
    text = _synthetic_makefile(extra="age-nuke:\n\tdocker compose --profile age down\n")
    assert len(check_no_unapproved_age_references(text)) == 1


def test_scan_catches_the_compose_profiles_env_route() -> None:
    text = _synthetic_makefile(
        extra="age-nuke:\n\tCOMPOSE_PROFILES=prod,age docker compose down\n"
    )
    assert len(check_no_unapproved_age_references(text)) == 1


def test_scan_follows_variable_aliases_in_both_syntaxes() -> None:
    """`AGE_CMD := $(COMPOSE_AGE)` and the `${}` form both stay visible."""
    paren = _synthetic_makefile(
        extra="AGE_CMD := $(COMPOSE_AGE)\nage-nuke:\n\t$(AGE_CMD) down\n"
    )
    assert len(check_no_unapproved_age_references(paren)) == 1

    brace = _synthetic_makefile(
        extra="AGE_CMD := ${COMPOSE_AGE}\nAGE_CMD2 := $(AGE_CMD)\n"
        "age-nuke:\n\t${AGE_CMD2} down\n"
    )
    assert len(check_no_unapproved_age_references(brace)) == 1


def test_scan_terminates_on_cyclic_variable_definitions() -> None:
    """Self- and mutually-referential definitions must not hang the closure."""
    text = _synthetic_makefile(
        extra="A := $(B)\nB := $(A)\nC := $(C) $(COMPOSE_AGE)\n"
        "age-nuke:\n\t$(C) down\n"
    )
    assert len(check_no_unapproved_age_references(text)) == 1


def test_scan_ignores_comments_and_other_profiles() -> None:
    """Prose about the incident, and the `prod` profile, are not violations.

    `prod-up`/`prod-down` bring up the *whole* stack on purpose — that is
    what "prod" means here — and the Makefile documents this very incident in
    comments that necessarily contain the dangerous command text.
    """
    text = _synthetic_makefile(
        extra=(
            "# Never write a bare `docker compose --profile age down` -- it\n"
            "# destroyed $(COMPOSE_AGE)'s neighbours once already.\n"
            "COMPOSE_PROD := docker compose -f deploy/docker/docker-compose.yml"
            " --profile prod\n"
            "prod-up:\n"
            "\t$(COMPOSE_PROD) up --build -d\n"
            "prod-down:\n"
            "\t$(COMPOSE_PROD) down\n"
        )
    )
    assert check_no_unapproved_age_references(text) == []
    assert check_approved_age_recipes(text) == []


def test_guard_never_shells_out() -> None:
    """Hard requirement: this file must never start a child process.

    The incident this file exists for was caused by a test that drove the
    container runtime. The forbidden names are assembled at runtime so this
    assertion cannot trip over its own source.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    for name in (
        "import " + "subprocess",
        "os." + "system",
        "os." + "popen",
        "pty." + "spawn",
        "os." + "execv",
    ):
        assert name not in source, name
