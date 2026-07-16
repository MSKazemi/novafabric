"""ADR-0188 deprecation drift gate.

Three surfaces must agree on exactly which ``/v0`` endpoints are deprecated:

1. **runtime** — the :data:`DEPRECATION_REGISTER` populated by
   ``deprecated(...)`` call sites, with routes resolved by inspecting the
   fully-mounted server app (``create_app`` with a default config);
2. **spec** — every operation in ``api/openapi.yaml`` carrying
   ``deprecated: true``, plus the ``x-deprecation-policy.currently-deprecated``
   list;
3. **docs** — the "### Register" table in ``docs/api-reference.md`` (or its
   "No endpoints are currently deprecated." sentinel).

All three are empty today, so the gate passes. Any future drift — an endpoint
deprecated on one surface but not the others — fails naming the offending
path(s). Parsing is tolerant of formatting (backticks, an optional ``/v0``
prefix, ``METHOD /path`` cells) but strict on content.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytest.importorskip("fastapi")

from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import ServerConfig  # noqa: E402
from novafabric.server.deprecation import (  # noqa: E402
    DEPRECATION_REGISTER,
    DeprecationEntry,
    deprecation_register,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
OPENAPI_PATH = REPO_ROOT / "api" / "openapi.yaml"
DOCS_PATH = REPO_ROOT / "docs" / "api-reference.md"

DOCS_SENTINEL = "No endpoints are currently deprecated."

_HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}

# Characters that make up a placeholder/separator table cell ("—", "---", ":-:").
_PLACEHOLDER_CHARS = set("—–- :")


def _normalize(path: str) -> str:
    """Canonical route form: backticks stripped, leading ``/v0`` removed
    (``api/openapi.yaml`` paths are relative to the ``/v0`` server URL)."""
    p = path.strip().strip("`").strip()
    if p == "/v0":
        return "/"
    if p.startswith("/v0/"):
        p = p[len("/v0"):]
    if not p.startswith("/"):
        p = "/" + p
    return p


# --------------------------------------------------------------------------- #
# Surface collectors
# --------------------------------------------------------------------------- #


def _runtime_deprecated_routes() -> set[str]:
    """Deprecated routes per the runtime register, with the app mounted.

    ``DeprecationEntry.route`` binds lazily on first request, so resolve each
    entry's path by finding its dependency on the fully-mounted app instead.
    An entry that matches no mounted route still fails the gate, by name.
    """
    app = create_app(ServerConfig())
    entries = deprecation_register()
    bound: dict[int, str] = {}
    for route in app.routes:
        for dep in getattr(route, "dependencies", None) or []:
            call = getattr(dep, "dependency", None)
            entry = getattr(call, "_entry", None)
            if entry is not None and any(entry is known for known in entries):
                bound[id(entry)] = str(getattr(route, "path", "<unknown>"))
    return {
        _normalize(
            bound.get(id(entry))
            or entry.route
            or f"<unmounted register entry: link={entry.link!r}>"
        )
        for entry in entries
    }


def _load_spec() -> dict[str, object]:
    spec = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
    assert isinstance(spec, dict), "api/openapi.yaml is not a mapping"
    return spec


def _spec_deprecated_operations(spec: dict[str, object]) -> set[str]:
    """Paths of every operation marked ``deprecated: true`` in the spec."""
    deprecated_paths: set[str] = set()
    paths = spec.get("paths")
    assert isinstance(paths, dict), "api/openapi.yaml lost its paths block"
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method, operation in item.items():
            if str(method).lower() not in _HTTP_METHODS:
                continue
            if isinstance(operation, dict) and operation.get("deprecated") is True:
                deprecated_paths.add(_normalize(str(path)))
    return deprecated_paths


def _spec_policy_list(spec: dict[str, object]) -> set[str]:
    """The ``x-deprecation-policy.currently-deprecated`` route list."""
    policy = spec.get("x-deprecation-policy")
    assert isinstance(policy, dict), (
        "api/openapi.yaml lost its x-deprecation-policy block (ADR-0188)"
    )
    listed = policy.get("currently-deprecated")
    assert isinstance(listed, list), (
        "x-deprecation-policy.currently-deprecated must be a list "
        f"(got {type(listed).__name__})"
    )
    return {_normalize(str(item)) for item in listed}


def _docs_register_rows() -> set[str]:
    """Endpoints listed in the docs register table (empty if sentinel)."""
    text = DOCS_PATH.read_text(encoding="utf-8")
    section_match = re.search(r"^##\s+Deprecation register\b.*$", text, re.MULTILINE)
    assert section_match, (
        "docs/api-reference.md lost its '## Deprecation register' section (ADR-0188)"
    )
    section = text[section_match.end():]
    next_section = re.search(r"^##\s", section, re.MULTILINE)
    if next_section:
        section = section[: next_section.start()]

    register_match = re.search(r"^###\s+Register\s*$", section, re.MULTILINE)
    assert register_match, (
        "the Deprecation register section lost its '### Register' table"
    )
    table = section[register_match.end():]

    rows: set[str] = set()
    for line in table.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        first_cell = stripped.strip("|").split("|", 1)[0].strip().strip("`").strip()
        if not first_cell or first_cell.lower() == "endpoint":
            continue  # header row
        if set(first_cell) <= _PLACEHOLDER_CHARS:
            continue  # separator or placeholder ("| — | — | …") row
        path_match = re.search(r"(/[^\s`|]*)", first_cell)
        assert path_match, (
            f"unparseable endpoint cell in the docs register table: {first_cell!r}"
        )
        rows.add(_normalize(path_match.group(1)))

    has_sentinel = DOCS_SENTINEL in table
    if has_sentinel:
        assert not rows, (
            "docs register is self-contradictory: the 'No endpoints are "
            f"currently deprecated.' sentinel coexists with rows {sorted(rows)}"
        )
    else:
        assert rows, (
            "docs register has neither the empty-state sentinel nor any rows"
        )
    return rows


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #


def _assert_no_drift() -> None:
    runtime = _runtime_deprecated_routes()
    spec = _load_spec()
    spec_ops = _spec_deprecated_operations(spec)
    policy = _spec_policy_list(spec)
    docs = _docs_register_rows()

    assert runtime == spec_ops, (
        "deprecation drift between the runtime register and openapi.yaml "
        f"'deprecated: true' operations — only-in-runtime={sorted(runtime - spec_ops)}, "
        f"only-in-openapi={sorted(spec_ops - runtime)}"
    )
    assert policy == runtime, (
        "deprecation drift between x-deprecation-policy.currently-deprecated "
        f"and the runtime register — only-in-policy={sorted(policy - runtime)}, "
        f"only-in-runtime={sorted(runtime - policy)}"
    )
    assert docs == runtime, (
        "deprecation drift between the docs/api-reference.md register table "
        f"and the runtime register — only-in-docs={sorted(docs - runtime)}, "
        f"only-in-runtime={sorted(runtime - docs)}"
    )


def test_gate_passes_on_current_tree() -> None:
    """All three surfaces agree today (all empty — nothing is deprecated)."""
    _assert_no_drift()


def test_runtime_register_matches_openapi_operations() -> None:
    assert _runtime_deprecated_routes() == _spec_deprecated_operations(_load_spec())


def test_policy_list_matches_runtime_register() -> None:
    assert _spec_policy_list(_load_spec()) == _runtime_deprecated_routes()


def test_docs_register_matches_runtime_register() -> None:
    assert _docs_register_rows() == _runtime_deprecated_routes()


def test_all_surfaces_are_empty_today() -> None:
    """ADR-0188 ships the mechanism only — nothing is deprecated in v0.59."""
    assert _runtime_deprecated_routes() == set()
    spec = _load_spec()
    assert _spec_deprecated_operations(spec) == set()
    assert _spec_policy_list(spec) == set()
    assert _docs_register_rows() == set()


def test_synthetic_drift_fails_by_name() -> None:
    """A register entry with no openapi/docs counterpart must fail the gate,
    naming the drifted route."""
    saved = list(DEPRECATION_REGISTER)
    DEPRECATION_REGISTER.append(
        DeprecationEntry(
            sunset="Fri, 15 Jan 2027 00:00:00 GMT",
            link="https://example.invalid/api-reference#deprecation-register",
            successor="/v0/new-ghost",
            since="0.60.0",
            route="/v0/ghost",
        )
    )
    try:
        with pytest.raises(AssertionError, match="/ghost"):
            _assert_no_drift()
    finally:
        DEPRECATION_REGISTER[:] = saved
