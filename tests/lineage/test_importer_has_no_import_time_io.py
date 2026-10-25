"""Importing ``novafabric.lineage._importer`` reads no file (review replay-lineage-registry-21).

The edge schema was loaded at import, so importing ``novafabric.lineage`` read a
file and a packaging slip became an ImportError for every lineage user. It loads on
first validation now.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from novafabric.lineage import _importer


def test_importing_the_module_reads_no_schema_file(monkeypatch: pytest.MonkeyPatch) -> None:
    real = Path.read_text

    def guarded(self: Path, *a: Any, **k: Any) -> str:
        if self.name == "lineage-edge.schema.json":
            raise AssertionError("schema read at import time")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", guarded)
    try:
        importlib.reload(_importer)
    finally:
        monkeypatch.undo()
        importlib.reload(_importer)


def test_the_schema_is_loaded_once_on_first_use() -> None:
    _importer._edge_schema.cache_clear()
    first = _importer._edge_schema()
    assert first is _importer._edge_schema()
    assert first["$schema"] or first  # a parsed JSON Schema object


def test_a_missing_schema_fails_at_validation_not_at_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(_importer, "_SCHEMA_PATH", tmp_path / "absent.json")
    _importer._edge_schema.cache_clear()
    try:
        with pytest.raises(FileNotFoundError):
            _importer._validate_edge_record({})
    finally:
        monkeypatch.undo()
        _importer._edge_schema.cache_clear()


def test_validation_still_rejects_a_bad_record() -> None:
    import jsonschema

    _importer._edge_schema.cache_clear()
    with pytest.raises(jsonschema.ValidationError):
        _importer._validate_edge_record({"not": "an edge"})
    assert json.dumps(_importer._edge_schema())  # serialisable
