"""ADR-0188 removal gate — pure checks over the published deprecation register.

The deprecation register (the ``### Register`` table in
``docs/api-reference.md``) is the durable record of every deprecated ``/v0``
endpoint. The runtime :data:`~novafabric.server.deprecation.DEPRECATION_REGISTER`
cannot serve as that record on its own: deleting a route deletes its
``deprecated(...)`` call site, and with it the runtime entry. So a removal is
detected by comparing the *published* register rows against the routes the
server currently exposes, at the current package version.

Rules enforced (ADR-0188 acceptance criteria):

- **Row completeness** — every row names an endpoint, a deprecation release,
  an earliest-removal release, and a replacement (or an explicit ``none``).
- **Minimum window** — the earliest-removal release is at least two minors
  after the deprecation release: ``earliest >= (major, minor + 2, 0)`` of the
  deprecation release, compared as release tuples. Crossing a major boundary
  therefore always satisfies the window (``1.9.0 -> 2.0.0`` is allowed, since
  the bump rule makes a major the only legal post-1.0 removal point anyway).
- **Removal gate** — a register-listed endpoint that is no longer served must
  record the release that removed it (``Removed in`` column), and that release
  must be ``>= earliest-removal``, ``<=`` the current release (no removal
  before the current release has reached the earliest-removal release), and a
  legal removal point per the **bump rule**: a minor release (``X.Y.0``)
  pre-1.0, a major release (``X.0.0``) post-1.0. A row that records a removal
  while the endpoint is still served is also a violation.

Version strings are compared on their ``major.minor.patch`` release tuple; a
leading ``v`` and any pre-release/dev/local suffix (``0.62.0rc1``) are
accepted and ignored, so a release candidate is gated as the release it
precedes.

Everything here is pure (no IO, no FastAPI import): callers pass the docs text,
the set of served routes, and the current version string.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass

__all__ = [
    "DeprecationWindowError",
    "GateViolation",
    "RegisterParseError",
    "RegisterRow",
    "check_register_rows",
    "check_removals",
    "is_removal_release",
    "minimum_earliest_removal",
    "normalize_route",
    "parse_register_table",
    "parse_release",
    "validate_window",
]

Release = tuple[int, int, int]

_RELEASE_RE = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?(?:[.+\-]?[A-Za-z0-9.+\-]*)?$")
_HTTP_METHODS = frozenset({"GET", "PUT", "POST", "DELETE", "OPTIONS", "HEAD", "PATCH", "TRACE"})
# Characters that make up a placeholder/separator table cell ("—", "---", ":-:").
_PLACEHOLDER_CHARS = frozenset("—–- :")

_COL_ENDPOINT = "endpoint"
_COL_DEPRECATED_IN = "deprecated in"
_COL_EARLIEST = "earliest removal"
_COL_SUNSET = "sunset date"
_COL_REPLACEMENT = "replacement"
_COL_REMOVED_IN = "removed in"
_REQUIRED_COLUMNS = (_COL_ENDPOINT, _COL_DEPRECATED_IN, _COL_EARLIEST, _COL_REPLACEMENT)


class DeprecationWindowError(ValueError):
    """A version string is malformed or violates the two-minor window."""


class RegisterParseError(ValueError):
    """The docs register section or table cannot be parsed."""


@dataclass(frozen=True)
class RegisterRow:
    """One row of the published deprecation register."""

    path: str
    """Normalized route path (``/v0`` prefix stripped, e.g. ``/old``)."""

    method: str | None
    """HTTP method when the row names one (``GET /v0/old``), else ``None``."""

    deprecated_in: str
    earliest_removal: str
    replacement: str
    sunset: str = ""
    removed_in: str | None = None

    @property
    def endpoint(self) -> str:
        """Display form used in violation messages."""
        return f"{self.method} {self.path}" if self.method else self.path


@dataclass(frozen=True)
class GateViolation:
    """One failed rule for one register row."""

    endpoint: str
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.endpoint}: [{self.rule}] {self.message}"


# --------------------------------------------------------------------------- #
# Versions
# --------------------------------------------------------------------------- #


def parse_release(value: str) -> Release:
    """Parse ``X.Y[.Z]`` (optional ``v`` prefix and suffix) into a release tuple.

    Raises:
        DeprecationWindowError: if ``value`` is not a release version.
    """
    match = _RELEASE_RE.match(value.strip())
    if match is None:
        raise DeprecationWindowError(
            f"{value!r} is not a release version (expected e.g. '0.60.0')."
        )
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def minimum_earliest_removal(deprecated_in: str) -> Release:
    """Smallest earliest-removal release allowed for a deprecation release."""
    major, minor, _ = parse_release(deprecated_in)
    return major, minor + 2, 0


def validate_window(deprecated_in: str, earliest_removal: str) -> None:
    """Enforce "earliest removal at least two minors after deprecation".

    Raises:
        DeprecationWindowError: on a malformed version or a too-short window.
    """
    floor = minimum_earliest_removal(deprecated_in)
    if parse_release(earliest_removal) < floor:
        raise DeprecationWindowError(
            f"earliest removal {earliest_removal!r} is less than two minor releases "
            f"after deprecation release {deprecated_in!r} "
            f"(minimum {'.'.join(map(str, floor))})."
        )


def is_removal_release(version: str) -> bool:
    """Bump rule: removal lands only in a minor release pre-1.0 (``0.Y.0``)
    and only in a major release post-1.0 (``X.0.0``)."""
    major, minor, patch = parse_release(version)
    if major == 0:
        return patch == 0 and minor > 0
    return minor == 0 and patch == 0


# --------------------------------------------------------------------------- #
# Docs register table
# --------------------------------------------------------------------------- #


def normalize_route(path: str) -> str:
    """Canonical route form: backticks stripped, leading ``/v0`` removed
    (``api/openapi.yaml`` paths are relative to the ``/v0`` server URL)."""
    p = path.strip().strip("`").strip()
    if p == "/v0":
        return "/"
    if p.startswith("/v0/"):
        p = p[len("/v0") :]
    if not p.startswith("/"):
        p = "/" + p
    return p


def _is_placeholder(cell: str) -> bool:
    return not cell or set(cell) <= _PLACEHOLDER_CHARS


def _cells(line: str) -> list[str]:
    return [c.strip().strip("`").strip() for c in line.strip().strip("|").split("|")]


def _register_table_lines(markdown: str) -> list[str]:
    section = re.search(r"^##\s+Deprecation register\b.*$", markdown, re.MULTILINE)
    if section is None:
        raise RegisterParseError("missing '## Deprecation register' section (ADR-0188)")
    body = markdown[section.end() :]
    next_section = re.search(r"^##\s", body, re.MULTILINE)
    if next_section:
        body = body[: next_section.start()]
    table = re.search(r"^###\s+Register\s*$", body, re.MULTILINE)
    if table is None:
        raise RegisterParseError("the Deprecation register section lost its '### Register' table")
    lines = [ln for ln in body[table.end() :].splitlines() if ln.strip().startswith("|")]
    if not lines:
        raise RegisterParseError("the '### Register' subsection has no table")
    return lines


def _parse_endpoint(cell: str) -> tuple[str | None, str]:
    parts = cell.split()
    method: str | None = None
    if len(parts) == 2 and parts[0].upper() in _HTTP_METHODS:
        method, cell = parts[0].upper(), parts[1]
    elif len(parts) != 1 or not parts[0].startswith("/"):
        raise RegisterParseError(f"unparseable endpoint cell {cell!r}")
    return method, normalize_route(cell)


def parse_register_table(markdown: str) -> list[RegisterRow]:
    """Parse the ``### Register`` table of ``docs/api-reference.md``.

    Columns are matched by header name (case-insensitive); ``Endpoint``,
    ``Deprecated in``, ``Earliest removal`` and ``Replacement`` are required,
    ``Sunset date`` and ``Removed in`` optional. The separator row and
    all-placeholder rows (``| — | — | … |``) are skipped. Cell values are
    returned verbatim (empty for a placeholder) — completeness and window
    rules are :func:`check_register_rows`' job.

    Raises:
        RegisterParseError: missing section/table/column, or a bad endpoint cell.
    """
    lines = _register_table_lines(markdown)
    header = [c.lower() for c in _cells(lines[0])]
    missing = [c for c in _REQUIRED_COLUMNS if c not in header]
    if missing:
        raise RegisterParseError(f"register table is missing column(s) {missing}")
    index = {name: header.index(name) for name in header}

    def cell(cells: list[str], name: str) -> str:
        i = index.get(name)
        value = cells[i] if i is not None and i < len(cells) else ""
        return "" if _is_placeholder(value) else value

    rows: list[RegisterRow] = []
    for line in lines[1:]:
        cells = _cells(line)
        if all(_is_placeholder(c) for c in cells):
            continue  # separator or empty-state placeholder row
        endpoint = cell(cells, _COL_ENDPOINT)
        if not endpoint:
            raise RegisterParseError(f"register row without an endpoint: {line.strip()!r}")
        method, path = _parse_endpoint(endpoint)
        rows.append(
            RegisterRow(
                path=path,
                method=method,
                deprecated_in=cell(cells, _COL_DEPRECATED_IN),
                earliest_removal=cell(cells, _COL_EARLIEST),
                replacement=cell(cells, _COL_REPLACEMENT),
                sunset=cell(cells, _COL_SUNSET),
                removed_in=cell(cells, _COL_REMOVED_IN) or None,
            )
        )
    return rows


# --------------------------------------------------------------------------- #
# Gate checks
# --------------------------------------------------------------------------- #


def check_register_rows(rows: Iterable[RegisterRow]) -> list[GateViolation]:
    """Row-completeness and minimum-window rules for every register row."""
    violations: list[GateViolation] = []
    for row in rows:
        ep = row.endpoint
        if not row.deprecated_in:
            violations.append(GateViolation(ep, "row", "no deprecation release named"))
        if not row.earliest_removal:
            violations.append(GateViolation(ep, "row", "no earliest-removal release named"))
        if not row.replacement:
            violations.append(
                GateViolation(ep, "row", "no replacement named (write 'none' explicitly)")
            )
        if row.deprecated_in and row.earliest_removal:
            try:
                validate_window(row.deprecated_in, row.earliest_removal)
            except DeprecationWindowError as exc:
                violations.append(GateViolation(ep, "window", str(exc)))
        if row.removed_in is not None:
            try:
                parse_release(row.removed_in)
            except DeprecationWindowError as exc:
                violations.append(GateViolation(ep, "row", f"Removed in: {exc}"))
    return violations


def _is_served(row: RegisterRow, served: Collection[tuple[str, str]]) -> bool:
    return any(
        path == row.path and (row.method is None or method == row.method) for method, path in served
    )


def check_removals(
    rows: Iterable[RegisterRow],
    served_routes: Collection[tuple[str, str]],
    current_version: str,
) -> list[GateViolation]:
    """The removal gate: register rows vs served routes at ``current_version``.

    Args:
        rows: parsed register rows (see :func:`parse_register_table`).
        served_routes: ``(METHOD, path)`` pairs the server currently exposes;
            paths are normalized with :func:`normalize_route` here.
        current_version: the current package release (``novafabric.__version__``).

    Returns:
        One :class:`GateViolation` per broken rule; empty when the gate passes.
        Rows with unparseable versions are reported by
        :func:`check_register_rows` and skipped here.
    """
    served = {(m.upper(), normalize_route(p)) for m, p in served_routes}
    current = parse_release(current_version)
    violations: list[GateViolation] = []
    for row in rows:
        ep = row.endpoint
        is_served = _is_served(row, served)
        if is_served:
            if row.removed_in is not None:
                violations.append(
                    GateViolation(
                        ep,
                        "removal",
                        f"register records 'Removed in {row.removed_in}' but the "
                        "endpoint is still served",
                    )
                )
            continue
        if row.removed_in is None:
            violations.append(
                GateViolation(
                    ep,
                    "removal",
                    "register-listed endpoint is no longer served but its row does not "
                    "record the removing release ('Removed in' column)",
                )
            )
            continue
        try:
            earliest = parse_release(row.earliest_removal)
            removed = parse_release(row.removed_in)
        except DeprecationWindowError:
            continue  # reported by check_register_rows
        if current < earliest:
            violations.append(
                GateViolation(
                    ep,
                    "premature",
                    f"removed at current release {current_version} before its "
                    f"earliest-removal release {row.earliest_removal}",
                )
            )
        if removed < earliest:
            violations.append(
                GateViolation(
                    ep,
                    "premature",
                    f"'Removed in {row.removed_in}' precedes earliest-removal "
                    f"release {row.earliest_removal}",
                )
            )
        if removed > current:
            violations.append(
                GateViolation(
                    ep,
                    "premature",
                    f"'Removed in {row.removed_in}' is later than the current "
                    f"release {current_version}; remove the endpoint in that release",
                )
            )
        if not is_removal_release(row.removed_in):
            violations.append(
                GateViolation(
                    ep,
                    "bump",
                    f"'Removed in {row.removed_in}' is not a legal removal release "
                    "(minor X.Y.0 pre-1.0, major X.0.0 post-1.0)",
                )
            )
    return violations
