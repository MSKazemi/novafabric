"""Loader for the SLO catalog (ADR-0248) — gates read thresholds from here.

The catalog is the single source: a gate that hardcodes its threshold can
silently disagree with the published number, so gate tests call
``slo_value("<entry id>")`` instead of carrying a constant.
"""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

CATALOG_PATH = Path(__file__).with_name("slo_catalog.toml")

VALID_STATUSES = frozenset({"gated", "measured", "target"})


@lru_cache(maxsize=1)
def load_catalog() -> dict[str, dict[str, Any]]:
    """Parse the catalog and return entries keyed by id."""
    doc = tomllib.loads(CATALOG_PATH.read_text())
    entries = doc.get("entry", [])
    by_id: dict[str, dict[str, Any]] = {}
    for entry in entries:
        eid = entry["id"]
        if eid in by_id:
            raise ValueError(f"duplicate SLO catalog id: {eid}")
        by_id[eid] = entry
    return by_id


def slo_value(entry_id: str) -> float:
    """The catalog value for one entry — gate tests import this."""
    entry = load_catalog().get(entry_id)
    if entry is None:
        raise KeyError(
            f"SLO catalog has no entry {entry_id!r} — add it to {CATALOG_PATH}"
        )
    return float(entry["value"])
