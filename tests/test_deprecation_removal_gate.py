"""ADR-0188 removal gate.

The gate fails CI when the published deprecation register
(``docs/api-reference.md`` "### Register") breaks the lifecycle rules:

- every row names a deprecation release, an earliest-removal release at least
  two minors later, and a replacement (or an explicit ``none``);
- a register-listed endpoint that is no longer served must record the release
  that removed it, and that release must be ``>= earliest-removal``,
  ``<= novafabric.__version__``, and a legal removal point (minor ``0.Y.0``
  pre-1.0, major ``X.0.0`` post-1.0);
- a row recording a removal while the endpoint is still served fails.

"Served" = mounted by the default ``create_app(ServerConfig())`` app OR declared
in ``api/openapi.yaml`` — so an opt-in router (e.g. ``demo_device_grant``) is
never mistaken for a removal.

The first block is the live gate over the current tree; the rest pins the pure
rules in :mod:`novafabric.server.deprecation_gate` and the additive
``earliest_removal`` field on :func:`novafabric.server.deprecation.deprecated`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import novafabric
from novafabric.server.deprecation_gate import (
    DeprecationWindowError,
    GateViolation,
    RegisterParseError,
    RegisterRow,
    check_register_rows,
    check_removals,
    is_removal_release,
    minimum_earliest_removal,
    normalize_route,
    parse_register_table,
    parse_release,
    validate_window,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
OPENAPI_PATH = REPO_ROOT / "api" / "openapi.yaml"
DOCS_PATH = REPO_ROOT / "docs" / "api-reference.md"

_HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


# --------------------------------------------------------------------------- #
# Live gate over the current tree
# --------------------------------------------------------------------------- #


def _served_routes() -> set[tuple[str, str]]:
    """``(METHOD, path)`` pairs served by the default app or declared in the spec."""
    pytest.importorskip("fastapi")
    from novafabric.server.app import create_app
    from novafabric.server.config import ServerConfig

    served: set[tuple[str, str]] = set()
    for route in create_app(ServerConfig()).routes:
        path = getattr(route, "path", None)
        for method in getattr(route, "methods", None) or ():
            if path:
                served.add((str(method).upper(), str(path)))
    spec = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
    for path, item in (spec.get("paths") or {}).items():
        if isinstance(item, dict):
            for method in item:
                if str(method).lower() in _HTTP_METHODS:
                    served.add((str(method).upper(), str(path)))
    return served


def _docs_rows() -> list[RegisterRow]:
    return parse_register_table(DOCS_PATH.read_text(encoding="utf-8"))


def test_register_rows_are_complete_and_respect_the_window() -> None:
    violations = check_register_rows(_docs_rows())
    assert not violations, "\n".join(map(str, violations))


def test_no_register_listed_endpoint_was_removed_prematurely() -> None:
    violations = check_removals(_docs_rows(), _served_routes(), novafabric.__version__)
    assert not violations, "\n".join(map(str, violations))


def test_runtime_register_agrees_with_docs_rows() -> None:
    """A runtime entry that declares since/earliest_removal must match its row."""
    pytest.importorskip("fastapi")
    from novafabric.server.deprecation import deprecation_register

    rows = {row.path: row for row in _docs_rows() if row.removed_in is None}
    for entry in deprecation_register():
        if entry.route is None or entry.earliest_removal is None:
            continue  # route binding and row existence are the drift gate's job
        row = rows.get(normalize_route(entry.route))
        assert row is not None, f"{entry.route} has no register row"
        assert (row.deprecated_in, row.earliest_removal) == (
            entry.since,
            entry.earliest_removal,
        ), f"{entry.route}: runtime since/earliest_removal disagree with the docs row"


def test_current_version_is_parseable() -> None:
    parse_release(novafabric.__version__)


# --------------------------------------------------------------------------- #
# Versions, window, bump rule
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0.60.0", (0, 60, 0)),
        ("v1.2.3", (1, 2, 3)),
        ("0.62", (0, 62, 0)),
        ("0.62.0rc1", (0, 62, 0)),
        ("1.0.0.dev3", (1, 0, 0)),
        (" 2.0.0+local ", (2, 0, 0)),
    ],
)
def test_parse_release(raw: str, expected: tuple[int, int, int]) -> None:
    assert parse_release(raw) == expected


@pytest.mark.parametrize("raw", ["", "none", "1", "a.b.c", "0.60.x.y z"])
def test_parse_release_rejects_non_versions(raw: str) -> None:
    with pytest.raises(DeprecationWindowError):
        parse_release(raw)


def test_minimum_earliest_removal_is_two_minors_later() -> None:
    assert minimum_earliest_removal("0.60.3") == (0, 62, 0)
    assert minimum_earliest_removal("1.9.0") == (1, 11, 0)


@pytest.mark.parametrize(
    ("since", "earliest"),
    [("0.60.0", "0.62.0"), ("0.60.5", "0.62.0"), ("0.60.0", "0.70.0"), ("1.9.0", "2.0.0")],
)
def test_window_accepts(since: str, earliest: str) -> None:
    validate_window(since, earliest)


@pytest.mark.parametrize(
    ("since", "earliest"),
    [("0.60.0", "0.61.0"), ("0.60.0", "0.61.9"), ("0.60.0", "0.60.0"), ("1.3.0", "1.4.0")],
)
def test_window_rejects_less_than_two_minors(since: str, earliest: str) -> None:
    with pytest.raises(DeprecationWindowError, match="two minor releases"):
        validate_window(since, earliest)


@pytest.mark.parametrize(
    ("version", "legal"),
    [
        ("0.62.0", True),
        ("0.62.1", False),
        ("0.0.0", False),
        ("1.0.0", True),
        ("2.0.0", True),
        ("1.1.0", False),
        ("1.0.1", False),
    ],
)
def test_bump_rule(version: str, legal: bool) -> None:
    assert is_removal_release(version) is legal


# --------------------------------------------------------------------------- #
# Docs table parser
# --------------------------------------------------------------------------- #

_DOC = """\
# API

## Deprecation register (ADR-0188)

Intro.

### Register

{body}

## Next section

| Endpoint | not | the | register |
|---|---|---|---|
| /v0/elsewhere | x | y | z |
"""

_HEADER = (
    "| Endpoint | Deprecated in | Earliest removal | Sunset date | Replacement | Removed in |\n"
    "|---|---|---|---|---|---|\n"
)


def _doc(rows: str) -> str:
    return _DOC.format(body=_HEADER + rows)


def test_parser_empty_register_placeholder_row() -> None:
    assert parse_register_table(_doc("| — | — | — | — | — | — |")) == []


def test_parser_reads_rows_and_normalizes_paths() -> None:
    rows = parse_register_table(
        _doc(
            "| `GET /v0/old` | 0.60.0 | 0.62.0 | 2027-01-15 | `/v0/new` | — |\n"
            "| `/v0/gone` | 0.50.0 | 0.52.0 | 2026-06-01 | none | 0.52.0 |\n"
        )
    )
    assert rows == [
        RegisterRow("/old", "GET", "0.60.0", "0.62.0", "/v0/new", "2027-01-15", None),
        RegisterRow("/gone", None, "0.50.0", "0.52.0", "none", "2026-06-01", "0.52.0"),
    ]
    assert rows[0].endpoint == "GET /old"
    assert rows[1].endpoint == "/gone"


def test_parser_tolerates_missing_optional_columns() -> None:
    doc = _DOC.format(
        body="| Endpoint | Deprecated in | Earliest removal | Replacement |\n"
        "|---|---|---|---|\n"
        "| /v0/old | 0.60.0 | 0.62.0 | none |\n"
    )
    (row,) = parse_register_table(doc)
    assert (row.sunset, row.removed_in) == ("", None)


def test_parser_keeps_blank_cells_for_the_row_checker() -> None:
    (row,) = parse_register_table(_doc("| /v0/old | — |  | — | — | — |"))
    assert (row.deprecated_in, row.earliest_removal, row.replacement) == ("", "", "")


@pytest.mark.parametrize(
    ("doc", "match"),
    [
        ("# nothing here\n", "Deprecation register"),
        ("## Deprecation register\n\ntext only\n", "### Register"),
        ("## Deprecation register\n\n### Register\n\nno table\n", "no table"),
        (
            "## Deprecation register\n\n### Register\n\n| Endpoint | Replacement |\n|---|---|\n",
            "missing column",
        ),
        (_doc("| — | 0.60.0 | 0.62.0 | — | none | — |"), "without an endpoint"),
        (_doc("| old endpoint here | 0.60.0 | 0.62.0 | — | none | — |"), "unparseable"),
        (_doc("| FETCH /v0/old | 0.60.0 | 0.62.0 | — | none | — |"), "unparseable"),
    ],
)
def test_parser_errors(doc: str, match: str) -> None:
    with pytest.raises(RegisterParseError, match=match):
        parse_register_table(doc)


def test_normalize_route() -> None:
    assert normalize_route("/v0") == "/"
    assert normalize_route("`/v0/runs/{id}`") == "/runs/{id}"
    assert normalize_route("runs") == "/runs"
    assert normalize_route("/scim/v2/Users") == "/scim/v2/Users"


# --------------------------------------------------------------------------- #
# Row checks
# --------------------------------------------------------------------------- #


def _row(**kw: str | None) -> RegisterRow:
    base: dict[str, str | None] = {
        "path": "/old",
        "method": None,
        "deprecated_in": "0.60.0",
        "earliest_removal": "0.62.0",
        "replacement": "/v0/new",
        "sunset": "2027-01-15",
        "removed_in": None,
    }
    base.update(kw)
    return RegisterRow(**base)  # type: ignore[arg-type]


def _rules(violations: list[GateViolation]) -> list[str]:
    return [v.rule for v in violations]


def test_complete_row_passes() -> None:
    assert check_register_rows([_row(), _row(replacement="none")]) == []


def test_row_missing_fields_each_reported() -> None:
    violations = check_register_rows([_row(deprecated_in="", earliest_removal="", replacement="")])
    messages = " ".join(v.message for v in violations)
    assert _rules(violations) == ["row", "row", "row"]
    assert "deprecation release" in messages
    assert "earliest-removal" in messages
    assert "'none'" in messages


def test_row_window_too_short() -> None:
    (v,) = check_register_rows([_row(earliest_removal="0.61.0")])
    assert v.rule == "window"
    assert str(v).startswith("/old: [window]")


def test_row_bad_versions() -> None:
    assert _rules(check_register_rows([_row(deprecated_in="soon")])) == ["window"]
    assert _rules(check_register_rows([_row(removed_in="later")])) == ["row"]


# --------------------------------------------------------------------------- #
# Removal gate
# --------------------------------------------------------------------------- #

SERVED = {("GET", "/v0/old"), ("POST", "/v0/other")}


def test_served_row_passes_before_and_after_earliest_removal() -> None:
    assert check_removals([_row()], SERVED, "0.60.0") == []
    assert check_removals([_row()], SERVED, "0.99.0") == []


def test_method_scoped_row_matches_on_method() -> None:
    assert check_removals([_row(method="GET")], SERVED, "0.61.0") == []
    (v,) = check_removals([_row(method="DELETE")], SERVED, "0.61.0")
    assert v.rule == "removal"


def test_removed_without_recording_removed_in_fails() -> None:
    (v,) = check_removals([_row()], set(), "0.62.0")
    assert v.rule == "removal"
    assert "Removed in" in v.message


def test_legal_removal_passes() -> None:
    assert check_removals([_row(removed_in="0.62.0")], set(), "0.62.0") == []
    # later patch releases keep passing — the row records the removing release
    assert check_removals([_row(removed_in="0.62.0")], set(), "0.62.3") == []
    assert check_removals([_row(removed_in="0.64.0")], set(), "1.2.0") == []


def test_premature_removal_rejected() -> None:
    violations = check_removals([_row(removed_in="0.61.0")], set(), "0.61.0")
    assert _rules(violations) == ["premature", "premature"]
    assert "earliest-removal" in violations[0].message


def test_removal_recorded_for_future_release_rejected() -> None:
    (v,) = check_removals([_row(removed_in="0.63.0")], set(), "0.62.1")
    assert v.rule == "premature"
    assert "later than the current release" in v.message


def test_removal_in_patch_release_violates_bump_rule() -> None:
    (v,) = check_removals([_row(removed_in="0.62.1")], set(), "0.62.1")
    assert v.rule == "bump"


def test_post_1_0_removal_requires_major() -> None:
    minor = _row(deprecated_in="1.3.0", earliest_removal="1.5.0", removed_in="1.5.0")
    assert _rules(check_removals([minor], set(), "1.5.0")) == ["bump"]
    major = _row(deprecated_in="1.3.0", earliest_removal="1.5.0", removed_in="2.0.0")
    assert check_removals([major], set(), "2.0.0") == []


def test_removed_in_recorded_but_still_served_fails() -> None:
    (v,) = check_removals([_row(removed_in="0.62.0")], SERVED, "0.62.0")
    assert v.rule == "removal"
    assert "still served" in v.message


def test_unparseable_row_versions_are_left_to_the_row_checker() -> None:
    assert (
        check_removals([_row(earliest_removal="soon", removed_in="0.62.0")], set(), "0.62.0") == []
    )


def test_bad_current_version_raises() -> None:
    with pytest.raises(DeprecationWindowError):
        check_removals([], set(), "dev")


# --------------------------------------------------------------------------- #
# deprecated(earliest_removal=...) — additive, validated at declaration time
# --------------------------------------------------------------------------- #


@pytest.fixture
def _clean_register():
    pytest.importorskip("fastapi")
    from novafabric.server.deprecation import DEPRECATION_REGISTER

    saved = list(DEPRECATION_REGISTER)
    DEPRECATION_REGISTER.clear()
    yield DEPRECATION_REGISTER
    DEPRECATION_REGISTER[:] = saved


LINK = "https://example.com/docs/api-reference#deprecation-register"


def test_deprecated_records_earliest_removal(_clean_register) -> None:
    from novafabric.server.deprecation import deprecated

    deprecated("2027-01-15", LINK, since="0.60.0", earliest_removal="0.62.0")
    (entry,) = _clean_register
    assert (entry.since, entry.earliest_removal) == ("0.60.0", "0.62.0")


def test_deprecated_earliest_removal_is_optional(_clean_register) -> None:
    from novafabric.server.deprecation import deprecated

    deprecated("2027-01-15", LINK, since="0.60.0")
    assert _clean_register[0].earliest_removal is None


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"earliest_removal": "0.62.0"}, "requires since"),
        ({"since": "0.60.0", "earliest_removal": "0.61.0"}, "two minor releases"),
        ({"since": "0.60.0", "earliest_removal": "next"}, "not a release version"),
        ({"since": "sixty", "earliest_removal": "0.62.0"}, "not a release version"),
    ],
)
def test_deprecated_rejects_bad_window(_clean_register, kwargs: dict[str, str], match: str) -> None:
    from novafabric.server.deprecation import DeprecationConfigError, deprecated

    with pytest.raises(DeprecationConfigError, match=match):
        deprecated("2027-01-15", LINK, **kwargs)
    assert list(_clean_register) == []
