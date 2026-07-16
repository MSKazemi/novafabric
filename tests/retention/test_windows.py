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
"""Window parsing and due-date computation tests (ADR-0134)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from novafabric.retention.windows import (
    WindowParseError,
    compute_due_at,
    is_due,
    parse_iso_duration,
    parse_window,
)

CREATED = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("P1825D", timedelta(days=1825)),
        ("P30D", timedelta(days=30)),
        ("P2W", timedelta(days=14)),
        ("P1Y", timedelta(days=365)),
        ("P1M", timedelta(days=30)),
        ("P1Y6M", timedelta(days=365 + 180)),
        ("PT12H", timedelta(hours=12)),
        ("P1DT6H30M15S", timedelta(days=1, hours=6, minutes=30, seconds=15)),
    ],
)
def test_parse_iso_duration(value: str, expected: timedelta) -> None:
    assert parse_iso_duration(value) == expected


@pytest.mark.parametrize("value", ["", "P", "PT", "30 days", "5 years", "P-3D", "1825D", "Pabc"])
def test_parse_iso_duration_rejects_malformed(value: str) -> None:
    with pytest.raises(WindowParseError):
        parse_iso_duration(value)


def test_parse_window_duration_and_absolute_date() -> None:
    assert parse_window("P90D") == timedelta(days=90)
    assert parse_window("2031-01-01") == date(2031, 1, 1)
    with pytest.raises(WindowParseError):
        parse_window("2031-13-45")  # matches the date shape but is not a date
    with pytest.raises(WindowParseError):
        parse_window("next tuesday")


def test_compute_due_at_duration_is_relative_to_created_at() -> None:
    assert compute_due_at("P90D", CREATED) == CREATED + timedelta(days=90)


def test_compute_due_at_absolute_date_is_midnight_utc() -> None:
    assert compute_due_at("2031-01-01", CREATED) == datetime(2031, 1, 1, tzinfo=timezone.utc)


def test_compute_due_at_naive_created_at_treated_as_utc() -> None:
    naive = datetime(2026, 1, 1)
    assert compute_due_at("P1D", naive) == datetime(2026, 1, 2, tzinfo=timezone.utc)


def test_is_due_boundary() -> None:
    due_at = CREATED + timedelta(days=90)
    assert not is_due("P90D", CREATED, due_at - timedelta(seconds=1))
    assert is_due("P90D", CREATED, due_at)  # now >= due_at
    assert is_due("P90D", CREATED, due_at + timedelta(days=1))
