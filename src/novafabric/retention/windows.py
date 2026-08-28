# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Retention-window parsing and due-date computation (ADR-0134, stdlib only).

A ``window`` is either an ISO-8601 duration (``P1825D``) measured from the
item's ``created_at``, or an absolute RFC 3339 date (``2031-01-01``).
Calendar components are approximated deterministically: 1 year = 365 days,
1 month = 30 days (documented in ``the private design/spec/retention-scheduler-v0.md``).
All computation is UTC.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

_DURATION_RE = re.compile(
    r"^P(?!$)(?:(?P<years>\d+)Y)?(?:(?P<months>\d+)M)?(?:(?P<weeks>\d+)W)?"
    r"(?:(?P<days>\d+)D)?"
    r"(?:T(?!$)(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class WindowParseError(ValueError):
    """Raised when a retention window or duration string is malformed."""


def parse_iso_duration(value: str) -> timedelta:
    """Parse an ISO-8601 duration (``P30D``, ``P1Y6M``, ``PT12H``) into a timedelta.

    Years are approximated as 365 days and months as 30 days.

    Raises:
        WindowParseError: if *value* is not a valid non-empty ISO-8601 duration.
    """
    m = _DURATION_RE.match(value)
    if m is None:
        raise WindowParseError(f"invalid ISO-8601 duration: {value!r}")
    parts = {k: int(v) for k, v in m.groupdict().items() if v is not None}
    if not parts:
        raise WindowParseError(f"empty ISO-8601 duration: {value!r}")
    return timedelta(
        days=parts.get("years", 0) * 365
        + parts.get("months", 0) * 30
        + parts.get("weeks", 0) * 7
        + parts.get("days", 0),
        hours=parts.get("hours", 0),
        minutes=parts.get("minutes", 0),
        seconds=parts.get("seconds", 0),
    )


def parse_window(value: str) -> timedelta | date:
    """Parse a binding ``window``: an ISO-8601 duration or an absolute date.

    Raises:
        WindowParseError: if *value* is neither form.
    """
    if _DATE_RE.match(value):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise WindowParseError(f"invalid absolute date: {value!r}") from exc
    return parse_iso_duration(value)


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def compute_due_at(window: str, created_at: datetime) -> datetime:
    """Return the UTC datetime at which an item created at *created_at* becomes due.

    Duration windows are measured from ``created_at``; absolute dates are due
    at 00:00:00 UTC on that date.
    """
    parsed = parse_window(window)
    if isinstance(parsed, timedelta):
        return _as_utc(created_at) + parsed
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc)


def is_due(window: str, created_at: datetime, now: datetime) -> bool:
    """True when ``now (UTC) >= due_at`` for the given window and creation time."""
    return _as_utc(now) >= compute_due_at(window, created_at)
