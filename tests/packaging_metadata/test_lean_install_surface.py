"""Lean default-install surface guard (S2 / ADR-0222).

ADR-0222 moved ``duckdb``, ``pyarrow``, ``python-louvain`` and
``clickhouse-connect`` out of ``[project.dependencies]`` into extras, because
every import site of all four is already inside extra-gated code. A plain
``pip install novafabric`` therefore no longer ships them.

That guarantee is only worth anything if it keeps holding. These tests simulate
the lean install *in-process*, by installing a ``sys.meta_path`` finder that
refuses to import the moved distributions (plus ``numpy``, which reached a bare
install only transitively via ``python-louvain``), and then assert:

  a. the CLI still imports and ``nova --help`` still exits 0;
  b. the set of ``src/novafabric/`` modules that fail under the blocker is
     exactly a fixed allowlist — a regression guard against a future PR adding
     a top-level ``import duckdb``/``pyarrow`` to a core code path;
  c. the four distributions are absent from core deps and present in the
     extras that actually need them;
  d. no distribution name appears in both core deps and an extra (the
     duplicate-pin bug ADR-0222 also fixed for ``clickhouse-connect``).

The blocker is deliberately coarse (it blocks the top-level package name and
every submodule) so a module cannot sneak past by importing ``pyarrow.parquet``
directly.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT_PATH = _REPO_ROOT / "pyproject.toml"
_SRC_ROOT = _REPO_ROOT / "src"
_PKG_ROOT = _SRC_ROOT / "novafabric"

# Import names (not distribution names) withheld from a lean install.
# python-louvain's import name is `community`; clickhouse-connect's is
# `clickhouse_connect`. numpy is included because ADR-0222's reverse-dependency
# check showed python-louvain was its only path into a bare install.
BLOCKED_IMPORT_NAMES = frozenset(
    {"duckdb", "pyarrow", "numpy", "community", "clickhouse_connect"}
)

# Distribution names moved out of [project.dependencies] by ADR-0222, mapped to
# the extras that must provide them. Every listed extra must pin the dist.
MOVED_DISTRIBUTIONS: dict[str, frozenset[str]] = {
    "duckdb": frozenset({"scale", "serve", "query"}),
    "pyarrow": frozenset({"scale", "serve", "lineage-migration"}),
    "python-louvain": frozenset({"serve"}),
    "clickhouse-connect": frozenset({"scale", "clickhouse"}),
}

# THE ALLOWLIST. Every module under src/novafabric/ that fails to import when
# the moved dependencies are unavailable, mapped to the extra that fixes it.
#
# Derived empirically (see ADR-0222 §Evidence), not copied from a design doc:
# reproduce with the sweep embedded in `_sweep_source()` below. All seven live
# in code that was already extra-gated before ADR-0222 — that is precisely why
# the four dependencies were safe to move.
#
# Adding an entry here is a deliberate act: it means a NEW module now needs an
# extra to import. Prefer a function-level (lazy) import instead.
EXPECTED_BLOCKED_MODULES: dict[str, str] = {
    "novafabric.evidence_fabric.duckdb_accumulator": "scale",
    "novafabric.evidence_fabric.pii_table": "scale",
    "novafabric.lineage.migration.kit": "lineage-migration",
    "novafabric.lineage.migration.parquet_replay": "lineage-migration",
    "novafabric.serve.topology.ads_encoder": "serve",
    "novafabric.serve.topology.cluster_store": "serve",
    "novafabric.serve.topology.topology_extractor": "serve",
}

# Distributions pinned in BOTH [project.dependencies] and an extra.
#
# **Empty as of 2026-08-01** — ADR-0222 OQ-2 resolved the last three, so the
# guard below is now fully strict. `httpx` is genuinely core and the `server` /
# `federation` extras stopped restating it; `pyjwt` and `python-multipart` are
# server-only and left core entirely. The rule going forward: declare each
# dependency exactly once, in the lowest tier that genuinely needs it.
#
# Keep this set empty. `test_grandfathered_duplicate_pins_are_still_real` exists
# so an entry cannot outlive the duplicate it excuses.
_GRANDFATHERED_DUPLICATE_PINS: frozenset[str] = frozenset()

# Alembic migration modules are not importable standalone in ANY configuration:
# `env.py` reads `alembic.context`, which only exists while Alembic is driving
# it, and the `versions/*.py` scripts do `from alembic import op`, which resolves
# only under Alembic's runtime. They fail identically with and without the
# blocker, so they are baseline noise rather than a dependency finding.
#
# The exact SET is environment-dependent and must NOT be hardcoded: `alembic`
# itself ships in the [server] extra, so in a no-extras install the repo-root
# `alembic/` directory shadows the real distribution as a namespace package
# (`import alembic` then *succeeds* and only `alembic.config` fails) and the
# `versions/*.py` scripts fail too, while with the extras installed they import
# fine. Match on the subtree, not on a literal list.
#
# (This note used to add "which is how CI's `unit` job installs —
# `uv sync --frozen`". No longer true as of 2026-08-01: that install left 61
# tests unable to import their dependency, so the job now uses --all-extras.)
_ALEMBIC_SUBTREE_PREFIX = "novafabric.metadata_store.migrations."


@pytest.fixture(scope="module")
def pyproject() -> dict[str, Any]:
    with _PYPROJECT_PATH.open("rb") as handle:
        return tomllib.load(handle)


def _dist_name(requirement: str) -> str:
    """Normalise a PEP 508 requirement to its comparable distribution name."""
    name = requirement.split(";")[0].strip()
    for sep in (">=", "<=", "==", "!=", "~=", ">", "<", "["):
        name = name.split(sep)[0]
    return name.strip().lower().replace("_", "-")


# ---------------------------------------------------------------------------
# The in-process blocker
# ---------------------------------------------------------------------------


class _BlockingFinder:
    """A ``sys.meta_path`` finder that makes the moved deps look uninstalled."""

    def find_spec(
        self, fullname: str, path: Any = None, target: Any = None
    ) -> None:  # noqa: D102
        if fullname.split(".")[0] in BLOCKED_IMPORT_NAMES:
            raise ImportError(f"No module named {fullname!r} (lean-install blocker)")
        return None


def _sweep_source() -> str:
    """The sweep, as a standalone script run in a pristine subprocess.

    A subprocess is required, not a convenience: this test module runs inside a
    dev venv where every extra is already installed and much of ``novafabric``
    is already in ``sys.modules``. Blocking imports in-process would be defeated
    by that cache, and purging it would corrupt the rest of the test session.
    """
    return textwrap.dedent(
        f"""
        import importlib, json, sys
        from pathlib import Path

        BLOCKED = {sorted(BLOCKED_IMPORT_NAMES)!r}

        class Blocker:
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split(".")[0] in BLOCKED:
                    raise ImportError(f"No module named {{fullname!r}}")
                return None

        if "--block" in sys.argv:
            sys.meta_path.insert(0, Blocker())

        src = Path({str(_SRC_ROOT)!r})
        names = set()
        for py in (src / "novafabric").rglob("*.py"):
            parts = list(py.relative_to(src).parts)
            if parts[-1] == "__init__.py":
                parts = parts[:-1]
            else:
                parts[-1] = parts[-1][:-3]
            if parts:
                names.add(".".join(parts))

        failures = {{}}
        for name in sorted(names):
            # Purge novafabric between attempts: a package whose __init__ failed
            # can leave a half-initialised submodule in sys.modules and make a
            # later import spuriously pass.
            for mod in [
                m for m in sys.modules
                if m == "novafabric" or m.startswith("novafabric.")
            ]:
                del sys.modules[mod]
            try:
                importlib.import_module(name)
            except BaseException as exc:
                failures[name] = f"{{type(exc).__name__}}: {{exc}}"

        print(json.dumps({{"total": len(names), "failures": failures}}))
        """
    )


def _run_sweep(*, block: bool) -> dict[str, Any]:
    import json

    script = _sweep_source()
    argv = [sys.executable, "-c", script]
    if block:
        argv.append("--block")
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=600, cwd=str(_REPO_ROOT)
    )
    assert proc.returncode == 0, f"sweep failed:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# (a) the CLI survives a lean install
# ---------------------------------------------------------------------------


def test_cli_imports_and_help_exits_zero_without_moved_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`nova --help` must work on a plain `pip install novafabric`.

    This is the single most important guarantee in ADR-0222: whatever else
    degrades, the CLI must start. It exercises the eager import chain
    cli/main.py -> cli/insights.py -> lineage/analytics -> networkx, which is
    exactly why networkx stayed in core.
    """
    finder = _BlockingFinder()
    monkeypatch.setattr(sys, "meta_path", [finder, *sys.meta_path])
    # Force a genuine re-import of the CLI tree under the blocker.
    for name in [
        m for m in sys.modules if m == "novafabric" or m.startswith("novafabric.")
    ]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    main = importlib.import_module("novafabric.cli.main")

    from typer.testing import CliRunner

    result = CliRunner().invoke(main.app, ["--help"])
    assert result.exit_code == 0, result.output


def test_networkx_analytics_run_without_numpy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """networkx stays in core, and it must be *usable*, not merely importable.

    Importing networkx without numpy is easy; running graph analytics without
    it is the real claim. Guards against a future analytics change reaching for
    a numpy/scipy-backed networkx algorithm, which would silently re-introduce
    numpy as a hard requirement.
    """
    monkeypatch.setattr(sys, "meta_path", [_BlockingFinder(), *sys.meta_path])

    import networkx as nx

    from novafabric.lineage.analytics.centrality import compute_metrics_for_graph

    graph = nx.MultiDiGraph()
    for src_node, dst_node in [("a", "b"), ("b", "c"), ("c", "a"), ("c", "d")]:
        for node in (src_node, dst_node):
            graph.add_node(node, kind="run", ref=node, payload={})
        graph.add_edge(src_node, dst_node, edge_type="derived_from")

    report = compute_metrics_for_graph(graph, top_n=3, seed=0)
    assert report.node_count == 4
    assert report.edge_count == 4

    # networkx's own Louvain (nx.community) — NOT python-louvain, which moved
    # to the `serve` extra. insights.py relies on this distinction.
    communities = list(nx.community.louvain_communities(graph.to_undirected(), seed=0))
    assert communities


# ---------------------------------------------------------------------------
# (b) the allowlist regression guard
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def blocked_sweep() -> dict[str, Any]:
    return _run_sweep(block=True)


@pytest.fixture(scope="module")
def baseline_sweep() -> dict[str, Any]:
    return _run_sweep(block=False)


def test_baseline_sweep_only_fails_on_alembic_migration_scripts(
    baseline_sweep: dict[str, Any],
) -> None:
    """Establish the no-blocker baseline, so the blocked run is a clean diff.

    Anything failing here that is NOT an Alembic migration script means the
    environment is broken in some way unrelated to ADR-0222, and the diff the
    next test takes would silently absorb it.
    """
    assert baseline_sweep["total"] > 500, "sweep did not discover the package tree"
    unexpected = sorted(
        name
        for name in baseline_sweep["failures"]
        if not name.startswith(_ALEMBIC_SUBTREE_PREFIX)
    )
    assert not unexpected, (
        "modules failed to import with NOTHING blocked, outside the Alembic "
        f"migration subtree: {unexpected}. The blocked-vs-baseline diff cannot "
        "be trusted until this is understood."
    )


def test_moved_dependencies_are_installed_in_the_test_environment(
    baseline_sweep: dict[str, Any],
) -> None:
    """The test environment itself must have the moved dependencies.

    This is not redundant with the baseline test — it is the guard for a
    specific, easy-to-miss CI failure mode. CI's `unit` job installs with
    `uv sync --frozen` (**no** `--all-extras`), so the only thing putting
    duckdb / pyarrow / python-louvain / clickhouse-connect into that
    environment is `[dependency-groups] dev`. If someone removes them from the
    dev group, every allowlisted module fails in the *baseline* too, the diff
    in the next test cancels them out, and the whole regression guard silently
    degrades into a no-op that still reports green.

    Failing loudly here instead is the difference between a caught mistake and
    a guard that quietly stops guarding.
    """
    already_broken = sorted(
        set(baseline_sweep["failures"]) & set(EXPECTED_BLOCKED_MODULES)
    )
    assert not already_broken, (
        f"{already_broken} fail to import even with nothing blocked, so this "
        "environment is missing an optional dependency. Add duckdb, pyarrow, "
        "python-louvain and clickhouse-connect to [dependency-groups] dev — CI's "
        "unit job runs `uv sync --frozen` with no extras."
    )


def test_blocked_module_set_matches_allowlist_exactly(
    blocked_sweep: dict[str, Any], baseline_sweep: dict[str, Any]
) -> None:
    """The dependency-attributable failure set is exactly the allowlist.

    Failing *more* means a core path grew a top-level import of a moved dep —
    that is the regression this guard exists to catch. Failing *fewer* means the
    allowlist is stale and should be trimmed.
    """
    attributable = set(blocked_sweep["failures"]) - set(baseline_sweep["failures"])
    expected = set(EXPECTED_BLOCKED_MODULES)

    newly_broken = sorted(attributable - expected)
    assert not newly_broken, (
        "These modules now need an optional dependency to import, but are not on "
        f"the ADR-0222 allowlist: {newly_broken}. Prefer a function-level import "
        "over adding them here."
    )
    stale = sorted(expected - attributable)
    assert not stale, (
        f"Allowlist is stale — these modules import fine now: {stale}. Remove them."
    )


def test_every_allowlisted_module_lives_under_an_extra_gated_path() -> None:
    """The allowlist is only defensible if each entry is already extra-gated.

    A module that needs an extra to import must live in a subtree users only
    reach after installing that extra. If this ever fails, a *core* code path
    has become extra-dependent and ADR-0222's premise no longer holds.
    """
    gated_prefixes = (
        "novafabric.evidence_fabric.",
        "novafabric.lineage.migration.",
        "novafabric.serve.topology.",
        "novafabric.cost.clickhouse_store",
    )
    for module, extra in EXPECTED_BLOCKED_MODULES.items():
        assert module.startswith(gated_prefixes), (
            f"{module} needs extra [{extra}] but is not under a known extra-gated "
            "subtree — this is a core path regression"
        )


# ---------------------------------------------------------------------------
# (c) + (d) pyproject structure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("distribution", sorted(MOVED_DISTRIBUTIONS))
def test_moved_distribution_absent_from_core_dependencies(
    distribution: str, pyproject: dict[str, Any]
) -> None:
    core = {_dist_name(req) for req in pyproject["project"]["dependencies"]}
    assert distribution not in core, (
        f"{distribution} is back in [project.dependencies]; ADR-0222 moved it to "
        f"{sorted(MOVED_DISTRIBUTIONS[distribution])}"
    )


@pytest.mark.parametrize("distribution", sorted(MOVED_DISTRIBUTIONS))
def test_moved_distribution_present_in_its_extras(
    distribution: str, pyproject: dict[str, Any]
) -> None:
    extras = pyproject["project"]["optional-dependencies"]
    for extra in sorted(MOVED_DISTRIBUTIONS[distribution]):
        assert extra in extras, f"extra [{extra}] disappeared"
        names = {_dist_name(req) for req in extras[extra]}
        assert distribution in names, (
            f"[{extra}] must pin {distribution} — code under that extra imports it"
        )


def test_networkx_stays_in_core(pyproject: dict[str, Any]) -> None:
    """networkx is deliberately NOT moved: the CLI imports it eagerly."""
    core = {_dist_name(req) for req in pyproject["project"]["dependencies"]}
    assert "networkx" in core


def test_no_distribution_is_pinned_in_both_core_and_an_extra(
    pyproject: dict[str, Any],
) -> None:
    """A dist in core AND an extra is a duplicate pin that can drift apart.

    This is the exact bug ADR-0222 fixed for clickhouse-connect, which was
    pinned in both [project.dependencies] and the `scale` extra with
    independently editable version floors.

    Three pre-existing duplicates are grandfathered (see
    ``_GRANDFATHERED_DUPLICATE_PINS``). They are outside ADR-0222's scope,
    which is deliberately limited to the four moved distributions; they are
    recorded here rather than silently ignored so the guard stays sharp for
    anything new.
    """
    core = {_dist_name(req) for req in pyproject["project"]["dependencies"]}
    offenders: dict[str, list[str]] = {}
    for extra, reqs in pyproject["project"]["optional-dependencies"].items():
        for req in reqs:
            name = _dist_name(req)
            if name == "novafabric":  # the self-referencing `all` aggregate
                continue
            if name in core and name not in _GRANDFATHERED_DUPLICATE_PINS:
                offenders.setdefault(name, []).append(extra)
    assert not offenders, f"distributions pinned in both core and extras: {offenders}"


def test_grandfathered_duplicate_pins_are_still_real(
    pyproject: dict[str, Any],
) -> None:
    """Keep the exemption list honest: drop entries once they stop applying.

    Without this, the exemption above would quietly outlive the duplicates it
    excuses and start hiding genuinely new ones.
    """
    core = {_dist_name(req) for req in pyproject["project"]["dependencies"]}
    in_extras = {
        _dist_name(req)
        for reqs in pyproject["project"]["optional-dependencies"].values()
        for req in reqs
    }
    for name in _GRANDFATHERED_DUPLICATE_PINS:
        assert name in core and name in in_extras, (
            f"{name} is no longer pinned in both core and an extra — remove it "
            "from _GRANDFATHERED_DUPLICATE_PINS"
        )


def test_all_extra_includes_every_moved_distribution(
    pyproject: dict[str, Any],
) -> None:
    """`pip install 'novafabric[all]'` is the documented migration path.

    It must genuinely restore the pre-ADR-0222 import surface, otherwise the
    backward-compatibility story in the ADR and CHANGELOG is false.
    """
    extras = pyproject["project"]["optional-dependencies"]
    aggregate = extras["all"]
    assert len(aggregate) == 1, "`all` is expected to be one self-referencing entry"
    inner = aggregate[0]
    named = set(inner[inner.index("[") + 1 : inner.rindex("]")].split(","))

    for distribution, providers in MOVED_DISTRIBUTIONS.items():
        assert named & providers, (
            f"`all` names none of {sorted(providers)}, so it would not reinstate "
            f"{distribution}"
        )
