"""Chargeback export of metered usage — CSV / NDJSON (ADR-0208 P2, experimental).

``nova server usage export`` reads the ADR-0208 D4 tiers and emits one row
per ``(period, org, workspace, metric)`` for a closed range of ``YYYY-MM``
periods:

- **finalized** periods serve from ``usage_rollups`` (write-once, the stable
  rows a finance export should bill from) — ``status = "final"``;
- **not-yet-finalized** periods (always the current one) serve from
  ``usage_counters`` — ``status = "provisional"``: the totals can still grow
  until the period is frozen at the first metering write of the next period.

Guarantees:

- **Deterministic ordering** — ``(period, org, workspace, metric)``
  ascending; identical DB state always yields byte-identical output.
- **RFC 4180 CSV** — comma-separated, ``CRLF`` record terminator, a header
  row, fields containing ``,``/``"``/CR/LF quoted with doubled quotes.
- **Formula-injection-safe cells** — any *text* cell whose NFKC-normalized
  form starts with ``=``, ``+``, ``-``, ``@``, TAB, CR or LF — or with
  leading whitespace followed by one of those triggers — is prefixed with a
  single quote (OWASP CSV injection guidance), so a hostile workspace slug
  (including full-width ``＝＋－＠`` look-alikes, which spreadsheets may fold
  to ASCII) cannot execute in a spreadsheet. The original characters are
  kept; only the quote is added. Integer cells (``total`` may be negative after delete or
  reconciliation adjustments) are emitted as plain numbers and never
  prefixed.
- **NDJSON** — one JSON object per line, keys sorted, raw values (JSON
  consumers are not spreadsheets; no cell rewriting).

Read-only: the export never writes to the usage tables. Retention bound,
stated honestly: rollups older than ``server.usage.rollup_retention_months``
(default 24) are pruned, so periods beyond that window export as empty.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import unicodedata
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from novafabric.server import usage

ExportFormat = Literal["csv", "ndjson"]

#: CSV/NDJSON column order (stable public contract of the export).
COLUMNS: Final[tuple[str, ...]] = (
    "period",
    "org",
    "workspace",
    "metric",
    "total",
    "status",
    "finalized_at",
)

#: Upper bound on the number of periods one export may span (10 years) —
#: keeps a mistyped range bounded.
MAX_PERIODS: Final[int] = 120

_PERIOD_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])\Z")

#: Leading characters a spreadsheet may interpret as a formula (checked
#: after NFKC normalization, so full-width ``＝＋－＠`` are covered too).
_FORMULA_PREFIXES: Final[tuple[str, ...]] = ("=", "+", "-", "@", "\t", "\r", "\n")


class ChargebackExportError(Exception):
    """Base class for chargeback export failures."""


class InvalidPeriodRangeError(ChargebackExportError):
    """A period is malformed, the range is inverted, or it is too wide."""


class UsageStoreUnavailableError(ChargebackExportError):
    """The registry exists but could not be read (locked, I/O error, corrupt).

    Raised only on the read-only path: an empty export would be
    indistinguishable from "no usage" and silently under-bill, so any SQLite
    failure other than a missing DB file or missing usage table fails loudly.
    """


class ChargebackRow(BaseModel):
    """One exported usage total for ``(period, org, workspace, metric)``."""

    model_config = ConfigDict(frozen=True)

    period: str
    org: str
    workspace: str
    metric: str
    total: int
    status: Literal["final", "provisional"]
    finalized_at: str | None = None


def periods_between(start: str, end: str) -> list[str]:
    """Every ``YYYY-MM`` period from *start* to *end* inclusive (pure).

    Raises:
        InvalidPeriodRangeError: malformed period, ``start > end``, or more
            than :data:`MAX_PERIODS` periods.
    """
    for value in (start, end):
        if not _PERIOD_RE.fullmatch(value):
            raise InvalidPeriodRangeError(f"invalid period {value!r}: expected YYYY-MM")
    if start > end:
        raise InvalidPeriodRangeError(f"period range is inverted: {start} > {end}")
    sy, sm = (int(p) for p in start.split("-"))
    ey, em = (int(p) for p in end.split("-"))
    first, last = sy * 12 + sm - 1, ey * 12 + em - 1
    if last - first + 1 > MAX_PERIODS:
        raise InvalidPeriodRangeError(
            f"period range {start}..{end} spans {last - first + 1} periods; "
            f"the maximum is {MAX_PERIODS}"
        )
    return [f"{i // 12:04d}-{i % 12 + 1:02d}" for i in range(first, last + 1)]


def open_usage_db_read_only(db_path: Path | None = None) -> sqlite3.Connection | None:
    """Open the registry DB strictly read-only (``mode=ro``), or ``None`` if absent.

    Used by the HTTP export so a GET can never create the registry file, the
    usage tables, or change pragmas. ``None`` means the DB file does not exist
    (nothing was ever metered). ``sqlite3.Row`` rows; the caller closes it.
    """
    from novafabric.registry.store import get_db_path

    resolved = (db_path or get_db_path()).resolve()
    if not resolved.is_file():
        return None
    try:
        conn = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as exc:
        if not resolved.is_file():  # removed between the check and the open
            return None
        raise UsageStoreUnavailableError(f"cannot open usage registry read-only: {exc}") from exc
    conn.row_factory = sqlite3.Row
    return conn


def _is_missing_table(exc: sqlite3.Error) -> bool:
    """True only for "the usage tables were never created" (metering never ran)."""
    return "no such table" in str(exc)


def chargeback_rows(
    start: str,
    end: str,
    *,
    db_path: Path | None = None,
    workspace: str | None = None,
    org: str | None = None,
    read_only: bool = False,
) -> list[ChargebackRow]:
    """Usage rows for periods *start*..*end*, deterministically ordered.

    Finalized ``(workspace, period, metric)`` triples come from
    ``usage_rollups`` (``status='final'``); triples not yet finalized come
    from ``usage_counters`` (``status='provisional'``, org denormalized from
    the most recent ledger row — ``'default'`` when none remains). Optional
    exact-match *workspace* / *org* filters.

    With ``read_only=True`` (the HTTP export, ADR-0208 P3) the registry is
    opened ``mode=ro`` via :func:`open_usage_db_read_only` — no DDL, no
    pragma writes, no file creation — and a missing DB or missing usage
    tables export as zero rows instead of being created. Any other SQLite
    failure (locked, disk I/O, corrupt file) raises
    :class:`UsageStoreUnavailableError` rather than exporting an empty — and
    therefore silently wrong — billing file.
    """
    periods = periods_between(start, end)
    lo, hi = periods[0], periods[-1]
    if read_only:
        ro_conn = open_usage_db_read_only(db_path)
        if ro_conn is None:
            return []
        conn = ro_conn
    else:
        conn = usage.open_usage_db(db_path)
    try:
        rows: list[ChargebackRow] = [
            ChargebackRow(
                period=r["period"],
                org=r["org"],
                workspace=r["workspace"],
                metric=r["metric"],
                total=int(r["total"]),
                status="final",
                finalized_at=r["finalized_at"],
            )
            for r in conn.execute(
                "SELECT org, workspace, period, metric, total, finalized_at"
                " FROM usage_rollups WHERE period BETWEEN ? AND ?",
                (lo, hi),
            )
        ]
        rows.extend(
            ChargebackRow(
                period=r["period"],
                org=r["org"],
                workspace=r["workspace"],
                metric=r["metric"],
                total=int(r["total"]),
                status="provisional",
            )
            for r in conn.execute(
                """
                SELECT c.workspace, c.period, c.metric, c.total,
                       COALESCE((SELECT l.org FROM usage_ledger l
                                  WHERE l.workspace = c.workspace
                                    AND l.period = c.period
                                  ORDER BY l.recorded_at DESC LIMIT 1),
                                'default') AS org
                  FROM usage_counters c
                 WHERE c.period BETWEEN ? AND ?
                   AND NOT EXISTS (SELECT 1 FROM usage_rollups r
                                    WHERE r.workspace = c.workspace
                                      AND r.period = c.period
                                      AND r.metric = c.metric)
                """,
                (lo, hi),
            )
        )
    except sqlite3.Error as exc:
        if not read_only:
            raise
        if not _is_missing_table(exc):
            raise UsageStoreUnavailableError(f"cannot read usage registry: {exc}") from exc
        # Usage tables never created on this registry (metering never ran):
        # a read-only export reports "no usage", it does not create them.
        rows = []
    finally:
        conn.close()
    if workspace is not None:
        rows = [r for r in rows if r.workspace == workspace]
    if org is not None:
        rows = [r for r in rows if r.org == org]
    return sorted(rows, key=lambda r: (r.period, r.org, r.workspace, r.metric))


def safe_cell(value: str) -> str:
    """Neutralize a spreadsheet formula trigger in a text cell (pure).

    The cell is NFKC-normalized for the *check only* (full-width ``＝`` →
    ``=`` etc.). It gets a leading single quote when the normalized form
    starts with ``=``, ``+``, ``-``, ``@``, TAB, CR or LF, or when leading
    whitespace is followed by one of those; anything else is returned
    unchanged. The returned text keeps the original characters.
    """
    normalized = unicodedata.normalize("NFKC", value)
    if normalized.startswith(_FORMULA_PREFIXES) or normalized.lstrip().startswith(
        _FORMULA_PREFIXES
    ):
        return f"'{value}"
    return value


def _csv_record(row: ChargebackRow) -> list[str | int]:
    data = row.model_dump()
    record: list[str | int] = []
    for col in COLUMNS:
        value = data[col]
        if value is None:
            record.append("")
        elif isinstance(value, int):
            record.append(value)
        else:
            record.append(safe_cell(str(value)))
    return record


def _csv_line(record: Sequence[str | int]) -> str:
    buf = io.StringIO(newline="")
    csv.writer(buf, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL).writerow(record)
    return buf.getvalue()


def iter_csv(rows: Iterable[ChargebackRow]) -> Iterator[str]:
    """Yield RFC 4180 CSV one CRLF-terminated line at a time (header first)."""
    yield _csv_line(COLUMNS)
    for row in rows:
        yield _csv_line(_csv_record(row))


def iter_ndjson(rows: Iterable[ChargebackRow]) -> Iterator[str]:
    """Yield NDJSON one sorted-key JSON object (``\\n``-terminated) at a time."""
    for row in rows:
        yield json.dumps(row.model_dump(), sort_keys=True, separators=(",", ":")) + "\n"


def iter_render(rows: Iterable[ChargebackRow], fmt: ExportFormat) -> Iterator[str]:
    """Yield *rows* rendered in *fmt*, line by line (streaming form of :func:`render`).

    ``"".join(iter_render(rows, fmt)) == render(rows, fmt)`` — the HTTP
    export streams exactly the bytes the CLI writes.
    """
    return iter_csv(rows) if fmt == "csv" else iter_ndjson(rows)


def to_csv(rows: Sequence[ChargebackRow]) -> str:
    """Render *rows* as RFC 4180 CSV (header + CRLF-terminated records)."""
    return "".join(iter_csv(rows))


def to_ndjson(rows: Iterable[ChargebackRow]) -> str:
    """Render *rows* as NDJSON (one sorted-key JSON object per ``\\n`` line)."""
    return "".join(iter_ndjson(rows))


def render(rows: Sequence[ChargebackRow], fmt: ExportFormat) -> str:
    """Render *rows* in *fmt* (``csv`` or ``ndjson``)."""
    return "".join(iter_render(rows, fmt))
