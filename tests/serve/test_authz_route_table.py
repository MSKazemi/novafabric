"""ADR-0228 D1/D1a/D2/D3 — the scope lattice and the route-table completeness guard.

The completeness test is the reason D2 chose a central table over 184 route
decorators: a decorator set cannot be checked for gaps without reflecting over
the app anyway, and a gap in an authorization table is not a style problem.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from novafabric.serve.authz import (
    ROUTE_SCOPES,
    Scope,
    grantable_scopes,
    parse_scope,
    required_scope,
    satisfies,
)

pytest.importorskip("fastapi")

from novafabric.serve.app import create_app  # noqa: E402
from novafabric.serve.introspect import iter_routes  # noqa: E402

#: Routes mounted by ``nova serve`` *after* ``create_app`` returns, so they never
#: appear in a factory-built app. They still inherit the app-level dependency,
#: so leaving them unclassified would 403 the whole TV-5 surface.
_CALLER_MOUNTED = {
    ("GET", "/api/tv5/live"),
    ("GET", "/api/tv5/windows"),
    ("GET", "/api/tv5/snapshot/{window_id}"),
}


def _mounted_pairs(tmp_path: Path) -> set[tuple[str, str]]:
    """Every ``(method, path_template)`` the factory can mount, across **every flag**.

    Sweeping all four combinations rather than the default one is not thoroughness
    for its own sake: the first version of this guard fixed
    ``static_mounted_by_caller=True`` and therefore never saw the ``GET /``
    placeholder, which is registered only in the other branch. D3 caught it — the
    route 403'd — but a guard that reaches production before it reaches CI is a
    guard that ran late. A conditionally-registered route is invisible to any
    enumeration that pins the condition.
    """
    static_dir = tmp_path / "static"
    static_dir.mkdir(exist_ok=True)
    (static_dir / "index.html").write_text("<html></html>", encoding="utf-8")

    pairs: set[tuple[str, str]] = set()
    for topology in (False, True):
        for mounted_by_caller in (True, False):
            for static in (None, static_dir):
                app = create_app(
                    token="x" * 32,
                    capsule_dir=tmp_path,
                    static_dir=static,
                    static_mounted_by_caller=mounted_by_caller,
                    topology_enabled=topology,
                )
                for route in iter_routes(app):
                    for method in route.methods:
                        if method in ("HEAD", "OPTIONS"):
                            continue
                        pairs.add((method, route.path))
    return pairs


def test_every_mounted_route_is_classified(tmp_path: Path) -> None:
    """D2/D3: a new route without a classification fails here, not in production.

    D3 makes an unclassified route deny, which turns a forgotten line into a
    visibly broken feature rather than a quiet disclosure. This test is what
    keeps that branch unreachable in a released build.
    """
    missing = sorted(pair for pair in _mounted_pairs(tmp_path) if pair not in ROUTE_SCOPES)
    assert not missing, (
        f"{len(missing)} serve route(s) have no ADR-0228 authorization scope and would "
        f"therefore be denied to everyone: {missing}. Add each to "
        "serve.authz.ROUTE_SCOPES with the scope its capability warrants."
    )


def test_caller_mounted_routes_are_classified() -> None:
    """TV-5 is included after create_app and still inherits the app-level dependency."""
    missing = sorted(pair for pair in _CALLER_MOUNTED if pair not in ROUTE_SCOPES)
    assert not missing, f"caller-mounted routes unclassified: {missing}"


def test_table_has_no_entries_for_routes_that_do_not_exist(tmp_path: Path) -> None:
    """A stale entry is a silent lie about coverage — the failure mode of an allowlist.

    A hand-maintained allowlist that is never pruned drifts into describing an
    app that no longer exists, and then reads as proof of a coverage it does not
    have. This repo has been bitten by exactly that shape before.
    """
    live = _mounted_pairs(tmp_path) | _CALLER_MOUNTED
    stale = sorted(pair for pair in ROUTE_SCOPES if pair not in live)
    assert not stale, (
        f"{len(stale)} ROUTE_SCOPES entries name routes this app does not mount: {stale}"
    )


def test_public_is_not_grantable() -> None:
    """``public`` classifies a route; it is never something a credential holds."""
    assert Scope.public not in grantable_scopes()
    assert grantable_scopes() == {Scope.read, Scope.operate, Scope.admin, Scope.audit}


@pytest.mark.parametrize(
    ("held", "required", "expected"),
    [
        # The cumulative chain: admin ⊇ operate ⊇ read.
        (Scope.read, Scope.read, True),
        (Scope.read, Scope.operate, False),
        (Scope.read, Scope.admin, False),
        (Scope.operate, Scope.read, True),
        (Scope.operate, Scope.operate, True),
        (Scope.operate, Scope.admin, False),
        (Scope.admin, Scope.read, True),
        (Scope.admin, Scope.operate, True),
        (Scope.admin, Scope.admin, True),
        # D1a: audit sees everything read sees, plus the trail; never mutates.
        (Scope.audit, Scope.read, True),
        (Scope.audit, Scope.audit, True),
        (Scope.audit, Scope.operate, False),
        (Scope.audit, Scope.admin, False),
        # Only audit and admin reach the trail.
        (Scope.read, Scope.audit, False),
        (Scope.operate, Scope.audit, False),
        (Scope.admin, Scope.audit, True),
        # A public route needs nothing.
        (Scope.read, Scope.public, True),
        (Scope.audit, Scope.public, True),
    ],
)
def test_scope_lattice(held: Scope, required: Scope, expected: bool) -> None:
    assert satisfies(held, required) is expected


def test_audit_scope_is_strictly_more_than_read() -> None:
    """The whole point of D1a: audit is not "read minus" and not "read plus write"."""
    read_can = {r for r in grantable_scopes() if satisfies(Scope.read, r)}
    audit_can = {r for r in grantable_scopes() if satisfies(Scope.audit, r)}
    assert read_can < audit_can, "audit must strictly dominate read"
    assert not satisfies(Scope.audit, Scope.operate)
    assert not satisfies(Scope.audit, Scope.admin)


def test_unclassified_route_returns_none_not_read() -> None:
    """D3 is a sentinel, not a default. ``None`` means deny at every call site."""
    assert required_scope("GET", "/api/route-that-does-not-exist") is None
    assert required_scope("POST", "/api/runs") is None  # method not mounted


def test_required_scope_is_method_sensitive() -> None:
    """Reading a run and destroying it share a path and must not share a scope."""
    assert required_scope("GET", "/api/runs/{run_id}") is Scope.read
    assert required_scope("DELETE", "/api/runs/{run_id}") is Scope.admin


def test_the_escalation_bridge_routes_are_admin() -> None:
    """E2: the roles surface writes the table `nova server start` reads."""
    for method in ("GET", "POST"):
        assert required_scope(method, "/api/admin/roles") is Scope.admin
    assert required_scope("DELETE", "/api/admin/roles/{subject}/{role}") is Scope.admin


def test_evidence_destroying_routes_are_admin() -> None:
    """The four endpoints the ADR names as the reason it exists."""
    assert required_scope("DELETE", "/api/runs/{run_id}") is Scope.admin
    assert required_scope("POST", "/api/compliance/pii/erase") is Scope.admin
    assert required_scope("POST", "/api/seal/{capsule_id}/bypass") is Scope.admin
    assert required_scope("POST", "/api/admin/roles") is Scope.admin


def test_read_only_posts_are_read_not_operate() -> None:
    """Verb is not capability — a POST that only computes stays reachable by an auditor."""
    for path in (
        "/api/query",
        "/api/kg/blast-radius",
        "/api/mcp/scan",
        "/api/policy/check",
        "/api/compliance/export/rocrate",
        "/api/runs/{run_id}/verify",
    ):
        assert required_scope("POST", path) is Scope.read, path


def test_audit_trail_routes_require_the_audit_scope() -> None:
    assert required_scope("GET", "/api/audit") is Scope.audit
    assert required_scope("GET", "/api/reports/dashboard-audit") is Scope.audit
    assert required_scope("GET", "/api/policy/recent-decisions") is Scope.audit


def test_parse_scope_falls_back_rather_than_raising() -> None:
    """Runs on the request path against on-disk data — must not take the server down."""
    assert parse_scope("read") is Scope.read
    assert parse_scope(None) is Scope.admin
    assert parse_scope("") is Scope.admin
    assert parse_scope("nonsense") is Scope.admin
    assert parse_scope("nonsense", default=Scope.read) is Scope.read
    # `public` is a route classification and must never be read back as held.
    assert parse_scope("public", default=Scope.read) is Scope.read


# ---------------------------------------------------------------------------
# Prose-drift guard
# ---------------------------------------------------------------------------

_COUNT_CLAIMS = (
    (Path("src/novafabric/serve/authz.py"), r"(\d+) entries at the time of writing"),
    (Path("design/adr/0228-dashboard-authorization-model.md"), r"\*\*(\d+) entries\*\*"),
    (Path("CHANGELOG.md"), r"one declarative table of (\d+) routes"),
    (Path("ROADMAP.md"), r"one declarative (\d+)-route table"),
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_documented_route_counts_match_the_table() -> None:
    """Four surfaces quote the table's size; a fifth route makes all four wrong.

    Every one of these was already stale once — adding a single conditionally
    registered route to the table left 209 written in four places. A number
    copied into prose is a fact with no owner unless something checks it, and
    "roughly 200 routes are classified" is precisely the claim a reader would
    trust without re-deriving.
    """
    import re

    total = len(ROUTE_SCOPES)
    wrong: list[str] = []
    for rel, pattern in _COUNT_CLAIMS:
        path = _REPO_ROOT / rel
        if not path.is_file():  # a private-only surface in a public checkout
            continue
        found = re.findall(pattern, path.read_text(encoding="utf-8"))
        if not found:
            wrong.append(f"{rel}: no count found for /{pattern}/")
            continue
        for value in found:
            if int(value) != total:
                wrong.append(f"{rel}: says {value}, table has {total}")
    assert not wrong, "ROUTE_SCOPES size drifted away from the docs quoting it: " + "; ".join(wrong)


def test_documented_scope_breakdown_matches_the_table() -> None:
    """The per-scope split is quoted twice and drifts the same way the total does."""
    import collections
    import re

    counts = collections.Counter(scope.value for scope in ROUTE_SCOPES.values())
    for rel, pattern in (
        (Path("src/novafabric/serve/authz.py"), r"(\d+) ``(read|admin|operate|public|audit)``"),
        (
            Path("design/adr/0228-dashboard-authorization-model.md"),
            r"(\d+) `(read|admin|operate|public|audit)`",
        ),
    ):
        path = _REPO_ROOT / rel
        if not path.is_file():
            continue
        claims = re.findall(pattern, path.read_text(encoding="utf-8"))
        assert claims, f"{rel}: no scope breakdown found"
        for value, scope_name in claims:
            assert int(value) == counts[scope_name], (
                f"{rel}: claims {value} {scope_name!r} routes, table has {counts[scope_name]}"
            )
