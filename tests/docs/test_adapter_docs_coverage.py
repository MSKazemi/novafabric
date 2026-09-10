"""Guard: every exported adapter and every registered runner is documented.

`docs/cli-reference.md` is drift-guarded for **CLI commands**
(`test_cli_reference_coverage.py`), and the dashboard registry is guarded
separately. The **Python adapter API** was guarded by nothing — and that is
exactly how three of them shipped invisible.

Found 2026-09-10: `wrap_llamaindex`, `wrap_pydantic_ai` and `wrap_haystack` were
implemented, exported from `novafabric.adapters.__all__`, and covered by 19
passing tests — while appearing in **no user-facing document at all**. Nobody
could discover them, so nothing ever contradicted the ROADMAP row that still
listed all three under *"Next — specified, unclaimed, ready to build"* with
issues #1/#2/#3 open. Under-claiming is the drift nobody is incentivised to
catch, because nothing breaks when it happens.

Scope note: mirrors the sibling guard's bar deliberately — a **mention** in one
of the user-facing docs, not a prescribed section shape. Requiring a section per
adapter would force boilerplate; requiring nothing let three vanish.
"""

from __future__ import annotations

import re
from pathlib import Path

import novafabric.adapters as adapters_pkg

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Where a user could reasonably discover an adapter.
USER_FACING_DOCS = (
    REPO_ROOT / "docs" / "cli-reference.md",
    REPO_ROOT / "docs" / "user-guide.md",
    REPO_ROOT / "docs" / "developer-guide.md",
)


def _exported_adapter_symbols() -> set[str]:
    """Public adapter entry points, from the package's own ``__all__``.

    Derived from the source of truth rather than a hand-written list, so a new
    adapter is covered the moment it is exported — the mirror-list mistake that
    makes a guard quietly stop guarding.
    """
    return {
        name
        for name in getattr(adapters_pkg, "__all__", ())
        if name.startswith(("wrap_", "register_", "make_"))
    }


def test_the_export_list_is_actually_populated() -> None:
    """Guard the guard: an empty set would make the coverage test vacuous."""
    exported = _exported_adapter_symbols()
    assert len(exported) >= 8, (
        f"expected the full adapter export surface, found {sorted(exported)}"
    )


def test_every_exported_adapter_is_mentioned_in_user_facing_docs() -> None:
    text = "\n".join(
        p.read_text(encoding="utf-8") for p in USER_FACING_DOCS if p.is_file()
    )
    missing = sorted(name for name in _exported_adapter_symbols() if name not in text)
    assert not missing, (
        "adapters are exported but documented nowhere a user would look: "
        f"{missing}. They ship invisible — which is how wrap_llamaindex, "
        "wrap_pydantic_ai and wrap_haystack stayed on the ROADMAP as unbuilt "
        "while passing 19 tests. Add them to docs/cli-reference.md "
        "§Framework Adapters (reference) or docs/user-guide.md (tutorial)."
    )


def test_each_adapter_module_is_importable_without_its_framework() -> None:
    """The documented promise: importing the module never requires the framework.

    Every adapter module must stay importable so `from novafabric.adapters
    import wrap_x` works on a machine that has never installed the framework;
    the `ImportError` naming the install command is raised at **call** time.
    Asserted here because the docs say so in both guides.
    """
    import importlib

    for module in ("llamaindex", "pydantic_ai", "haystack"):
        importlib.import_module(f"novafabric.adapters.{module}")


# ---------------------------------------------------------------------------
# Runners — the same asymmetry, found by asking the same question again.
#
# `--runner lsf` and `--runner pbs` have been selectable from the CLI for some
# time, are in the registry, and are covered by unit tests. Both appeared in
# **zero** user-facing documents until 2026-09-10. Being experimental is a
# reason to label them, not a reason to hide them: a user can already pick one
# from `nova capture --runner`, and finding nothing written about it is worse
# than finding "experimental".
# ---------------------------------------------------------------------------

RUNNER_DOCS = (
    REPO_ROOT / "docs" / "operator-guide.md",
    REPO_ROOT / "docs" / "user-guide.md",
    REPO_ROOT / "docs" / "cli-reference.md",
)


def _registered_runner_names() -> set[str]:
    """Runner names the CLI will actually accept, from the registry itself."""
    from novafabric.runners._registry import known_runner_names

    return set(known_runner_names())


def test_the_runner_registry_is_actually_populated() -> None:
    """Guard the guard: an empty registry would make the check vacuous."""
    names = _registered_runner_names()
    assert len(names) >= 6, f"expected the full runner set, found {sorted(names)}"


def test_every_registered_runner_is_documented() -> None:
    text = "\n".join(p.read_text(encoding="utf-8") for p in RUNNER_DOCS if p.is_file())
    missing = sorted(
        name
        for name in _registered_runner_names()
        if f"--runner {name}" not in text and f"`{name}`" not in text
    )
    assert not missing, (
        f"runners are selectable from `nova capture --runner` but documented "
        f"nowhere: {missing}. A user can pick one today and find nothing "
        f"written about it. Label experimental ones as experimental — the docs "
        f"honesty rule — but do not leave them undiscoverable."
    )


# ---------------------------------------------------------------------------
# Escape hatches — the same question, asked of the safety defaults.
#
# 22 of 130 NOVA* environment variables were read by the code and documented
# nowhere. Most are operational knobs. Four were not: they switch off
# authentication, the non-loopback bind refusal, the dashboard path denylist,
# and webhook URL validation. A control an operator cannot find is a control
# they cannot audit, and a deployment review that greps the docs for its own
# risk surface would have missed all four.
# ---------------------------------------------------------------------------

#: Names that mean "a safety default can be turned off here".
_ESCAPE_HATCH = re.compile(
    r"NOVA[A-Z_]*(INSECURE|ALLOW|I_KNOW|I_ACCEPT|SKIP|DISABLE|UNSAFE|NO_AUTH|BYPASS)[A-Z_]*"
)

SECURITY_DOCS = (
    REPO_ROOT / "SECURITY.md",
    REPO_ROOT / "docs" / "operator-guide.md",
    REPO_ROOT / "docs" / "cli-reference.md",
    REPO_ROOT / "docs" / "user-guide.md",
)


def _escape_hatch_env_vars() -> set[str]:
    """Escape-hatch env var names, read out of the source tree itself."""
    src = REPO_ROOT / "src" / "novafabric"
    found: set[str] = set()
    for path in src.rglob("*.py"):
        for match in _ESCAPE_HATCH.finditer(path.read_text(encoding="utf-8")):
            found.add(match.group(0))
    return {n for n in found if not n.endswith("_")}


def test_escape_hatches_are_actually_found() -> None:
    """Guard the guard: a regex that matches nothing would pass silently."""
    hatches = _escape_hatch_env_vars()
    assert len(hatches) >= 8, f"expected the escape-hatch set, found {sorted(hatches)}"


def test_every_escape_hatch_is_documented() -> None:
    text = "\n".join(p.read_text(encoding="utf-8") for p in SECURITY_DOCS if p.is_file())
    missing = sorted(n for n in _escape_hatch_env_vars() if n not in text)
    assert not missing, (
        f"environment variables that switch OFF a safety default, documented "
        f"nowhere an operator would look: {missing}. A control that cannot be "
        f"found cannot be audited. Add it to SECURITY.md §Escape hatches."
    )
