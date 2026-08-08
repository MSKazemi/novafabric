"""Version-tolerant introspection of the mounted HTTP route table.

The sibling of :mod:`novafabric.cli.introspect`, and it exists for the same
reason. Contract tests, the OpenAPI conformance gate, and the dashboard route
audit all need "every API route this app serves", and each of them used to walk
``app.routes`` filtering on ``isinstance(r, APIRoute)``.

That works only while FastAPI flattens included routers into ``app.routes``.
FastAPI 0.141 stopped doing so: ``include_router`` now appends a single lazy
``fastapi.routing._IncludedRouter`` instead of copying each child route up. An
``isinstance`` filter therefore returns *nothing* for every included router,
which reads exactly like "the API lost its endpoints".

It has not. Measured on 0.141.1, ``GET /api/holds`` still returns 200 and the
generated ``openapi.json`` still lists the path — only the introspection shape
changed. Recording that here because the CLI equivalent of this bug was read as
a broken CLI and cost the project a frozen Typer pin; the same misreading of
this one would cost a frozen FastAPI pin.

The traversal below therefore avoids class checks entirely:

* an object exposing ``routes`` is a container — recurse
* an object exposing ``original_router`` is FastAPI's lazy include — recurse
  into the router it wraps
* an object exposing ``path`` and ``methods`` is a route — yield it

Cycles are impossible in a route tree but guarded anyway, because a mount that
points at its own parent app is a configuration mistake that should raise rather
than hang a test run.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

__all__ = ["RouteInfo", "iter_routes", "route_methods", "route_paths"]

#: Depth limit for the recursive walk. Real route trees nest two or three deep;
#: anything beyond this is a cycle or a pathological mount.
_MAX_DEPTH = 20


class RouteInfo:
    """One mounted route: its path and the HTTP methods it answers."""

    __slots__ = ("methods", "path", "route")

    def __init__(self, path: str, methods: frozenset[str], route: Any) -> None:
        self.path = path
        self.methods = methods
        #: The underlying framework object, for callers needing more detail.
        self.route = route

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"RouteInfo({self.path!r}, {sorted(self.methods)!r})"


def _children(node: Any) -> tuple[Any, str] | None:
    """Return ``(child routes, prefix)`` for a container, or ``None`` for a leaf.

    The prefix matters because FastAPI >= 0.141 no longer rewrites child paths
    when a router is included. ``/deep`` included under ``prefix="/nested"``
    stays ``/deep`` on the route object, and the prefix lives on the wrapper —
    so a walker that ignores it reports a path the server does not serve.
    """
    # FastAPI >= 0.141: include_router() appends a lazy wrapper that holds the
    # prefix in its include context rather than applying it to the children.
    included = getattr(node, "original_router", None)
    if included is not None:
        context = getattr(node, "include_context", None)
        prefix = getattr(context, "prefix", "") or ""
        return getattr(included, "routes", ()), prefix

    # Starlette Mount / APIRouter / FastAPI app. A Mount carries its prefix as
    # `.path`; an APIRouter as `.prefix`; an app as neither.
    routes = getattr(node, "routes", None)
    if routes is not None:
        prefix = getattr(node, "prefix", None)
        if not isinstance(prefix, str):
            mount_path = getattr(node, "path", None)
            prefix = mount_path if isinstance(mount_path, str) else ""
        return routes, prefix

    return None


def iter_routes(app: Any) -> Iterator[RouteInfo]:
    """Yield every HTTP route reachable from ``app``.

    Works whether routers were flattened into ``app.routes`` (FastAPI < 0.141)
    or mounted lazily (>= 0.141), and equally for a bare ``APIRouter``.

    Non-HTTP entries — WebSocket routes, static mounts, the docs endpoints —
    are skipped, since every caller wants the API surface.
    """
    yield from _visit(app, 0, "")


def _visit(node: Any, depth: int, prefix: str) -> Iterator[RouteInfo]:
    if depth > _MAX_DEPTH:
        raise RecursionError(
            f"Route tree deeper than {_MAX_DEPTH} levels; a mount most likely "
            "points at its own parent."
        )

    container = _children(node)
    if container is not None:
        children, own_prefix = container
        for child in children:
            yield from _visit(child, depth + 1, prefix + own_prefix)
        return

    yield from _emit(node, prefix)


def _emit(node: Any, prefix: str) -> Iterator[RouteInfo]:
    """Yield ``node`` as a route if it looks like one."""
    path = getattr(node, "path", None)
    methods = getattr(node, "methods", None)
    if isinstance(path, str) and methods:
        yield RouteInfo(prefix + path, frozenset(methods), node)


def route_paths(app: Any) -> set[str]:
    """Every mounted HTTP path."""
    return {info.path for info in iter_routes(app)}


def route_methods(app: Any) -> set[tuple[str, str]]:
    """Every mounted ``(path, method)`` pair."""
    return {(info.path, method) for info in iter_routes(app) for method in info.methods}
