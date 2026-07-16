"""API deprecation & sunset mechanism for the multi-user ``/v0`` API.

ADR-0188 (experimental): deprecated endpoints emit three response headers —

- ``Deprecation:`` (RFC 9745) — ``@<unix-timestamp>`` when the deprecation
  moment is known, ``true`` otherwise;
- ``Sunset:`` (RFC 8594) — the earliest-removal date as an HTTP-date;
- ``Link: <url>; rel="deprecation"`` — pointing at the register entry in
  ``docs/api-reference.md``.

Every deprecated route is also recorded in the module-level
:data:`DEPRECATION_REGISTER` (route, since-version, sunset date, successor)
so future tooling (``/v0/version``, docs generators, the release drift gate)
can enumerate the current table via :func:`deprecation_register`.

Policy (ADR-0188): a deprecated endpoint stays working for at least two
minor releases; removal lands only in a minor bump pre-1.0 (major post-1.0);
the ``serve/`` dashboard API is experimental/unversioned and exempt.

**No endpoint is deprecated today** — the register starts empty; this module
is the mechanism, not a deprecation. Usage, when the first deprecation lands::

    @router.get(
        "/old-endpoint",
        dependencies=[deprecated(
            sunset="2027-01-01",
            link="https://…/docs/api-reference.md#api-deprecation-register",
            successor="/v0/new-endpoint",
            since="0.60.0",
        )],
    )
    async def old_endpoint() -> ...: ...
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime

from fastapi import Request, Response, params
from fastapi.params import Depends as DependsParam


class DeprecationConfigError(ValueError):
    """Raised when ``deprecated(...)`` is configured with an invalid value."""


@dataclass
class DeprecationEntry:
    """One row of the deprecation register (ADR-0188).

    ``route`` is bound lazily on the first request through the wrapped
    endpoint (the FastAPI route path is not known at declaration time).
    """

    sunset: str
    """Earliest-removal date, normalized to an RFC 8594 HTTP-date."""

    link: str
    """URL of the register entry, sent as ``Link: <…>; rel="deprecation"``."""

    successor: str | None = None
    """Replacement endpoint, or ``None`` for an explicit "none"."""

    since: str | None = None
    """Release in which the deprecation was announced (e.g. ``"0.60.0"``)."""

    deprecation: str = "true"
    """Value of the ``Deprecation:`` header (``@<unix-ts>`` or ``true``)."""

    route: str | None = None
    """FastAPI route path (e.g. ``/v0/old``); bound on first request."""


#: Module-level register of all declared deprecations. Starts EMPTY —
#: production code has no ``deprecated(...)`` call sites today (ADR-0188
#: ships the mechanism only). Append-only via :func:`deprecated`.
DEPRECATION_REGISTER: list[DeprecationEntry] = []

_register_lock = threading.Lock()


def deprecation_register() -> tuple[DeprecationEntry, ...]:
    """Return the current deprecation register as an immutable snapshot.

    For future ``/v0/version`` responses, docs tooling, and the
    register-vs-``openapi.yaml`` drift gate (ADR-0188, future CI).
    """
    return tuple(DEPRECATION_REGISTER)


def _parse_timestamp(value: str, param: str) -> datetime:
    """Parse an ISO 8601 date or datetime string into an aware UTC datetime."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DeprecationConfigError(
            f"{param}={value!r} is not an ISO 8601 date or datetime "
            "(expected e.g. '2027-01-01' or '2027-01-01T00:00:00+00:00')."
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class _DeprecatedDependency:
    """FastAPI dependency that stamps the three ADR-0188 headers."""

    def __init__(self, entry: DeprecationEntry, headers: dict[str, str]) -> None:
        self._entry = entry
        self._headers = headers

    async def __call__(self, request: Request, response: Response) -> None:
        if self._entry.route is None:
            route = request.scope.get("route")
            path = (
                getattr(route, "path_format", None)
                or getattr(route, "path", None)
                or request.url.path
            )
            with _register_lock:
                if self._entry.route is None:
                    self._entry.route = str(path)
        for name, value in self._headers.items():
            response.headers[name] = value


def deprecated(
    sunset: str,
    link: str,
    successor: str | None = None,
    *,
    since: str | None = None,
    deprecated_at: str | None = None,
) -> params.Depends:
    """Mark a route as deprecated per ADR-0188.

    Returns a FastAPI dependency (pass it in the route's ``dependencies=[…]``
    list) that adds ``Deprecation:``, ``Sunset:`` (RFC 8594 HTTP-date) and
    ``Link: <link>; rel="deprecation"`` headers to every response, and
    records the route in :data:`DEPRECATION_REGISTER`.

    Args:
        sunset: Earliest-removal date (ISO 8601 date or datetime). Emitted
            as an RFC 8594 HTTP-date. Per policy it must be at least two
            minor releases after ``since``.
        link: URL of the deprecation-register entry for this endpoint.
        successor: Replacement endpoint path or URL; ``None`` means an
            explicit "none" in the register.
        since: Release that announced the deprecation (register column).
        deprecated_at: Optional ISO 8601 moment the deprecation took
            effect; when given, ``Deprecation: @<unix-ts>`` is emitted
            instead of ``Deprecation: true``.

    Raises:
        DeprecationConfigError: if ``sunset`` or ``deprecated_at`` cannot
            be parsed (fails fast at declaration time, not per-request).
    """
    sunset_http_date = format_datetime(_parse_timestamp(sunset, "sunset"), usegmt=True)
    if deprecated_at is not None:
        deprecation_value = f"@{int(_parse_timestamp(deprecated_at, 'deprecated_at').timestamp())}"
    else:
        deprecation_value = "true"

    entry = DeprecationEntry(
        sunset=sunset_http_date,
        link=link,
        successor=successor,
        since=since,
        deprecation=deprecation_value,
    )
    with _register_lock:
        DEPRECATION_REGISTER.append(entry)

    headers = {
        "Deprecation": deprecation_value,
        "Sunset": sunset_http_date,
        "Link": f'<{link}>; rel="deprecation"',
    }
    return DependsParam(_DeprecatedDependency(entry, headers))
