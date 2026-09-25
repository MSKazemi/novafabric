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

"""Timestamp discipline for ordered ``facets.embodied`` objects (ADR-0162 P2).

NF-303 excursions and NF-310 trajectory hops are the first embodied objects
whose *order* is evidence, so their ``ts`` has to be comparable. A naive
timestamp cannot be ordered against an aware one, and "10:00" on a robot's
local clock says nothing about "10:00" on the operator's — so a timestamp
without an offset is refused rather than guessed at.

The string is validated here but stored exactly as the caller wrote it: the
sealed record keeps the declared bytes, and only comparisons use the parsed
value. NovaFabric records clocks; it never disciplines one (NF-308).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


class InvalidTimestampError(Exception):
    """Raised when an embodied ``ts`` is not an offset-aware ISO-8601 instant.

    Deliberately **not** a ``ValueError`` — pydantic would fold it into a
    generic ``ValidationError`` and lose the name (see
    :class:`novafabric.embodied.RawPayloadRejectedError`).
    """


def parse_ts(value: Any) -> datetime:
    """Parse ``value`` as an offset-aware ISO-8601 instant.

    Accepts the ``Z`` suffix. Returns the parsed :class:`datetime` for
    comparison only; callers keep the original string.

    Raises:
        InvalidTimestampError: if ``value`` is not a string, does not parse,
            or carries no UTC offset.
    """
    if not isinstance(value, str) or not value.strip():
        raise InvalidTimestampError(
            f"timestamp {value!r} is not an ISO-8601 string; ordered embodied "
            "evidence needs a comparable instant (ADR-0162 P2)"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidTimestampError(f"timestamp {value!r} is not valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidTimestampError(
            f"timestamp {value!r} has no UTC offset; a naive time cannot be "
            "ordered against another clock domain — add 'Z' or '+HH:MM'"
        )
    return parsed


__all__ = ["InvalidTimestampError", "parse_ts"]
