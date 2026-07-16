"""JSONL dataset loader for the dataset-experiment harness (ADR-0120).

Covers validation (missing/duplicate ``item_id``, bad JSON, unknown keys,
empty file) and content pinning: ``dataset_hash`` is over raw file bytes,
``split_hash`` over the ordered ``item_id`` sequence only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novafabric.eval.experiment_dataset import (
    DatasetError,
    DatasetItem,
    load_dataset,
)


def _write(tmp_path: Path, lines: list[str], name: str = "items.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_load_valid_dataset(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        [
            json.dumps({"item_id": "a", "input": "hello", "expected": "hello"}),
            "",  # blank lines are skipped
            json.dumps({"item_id": "b", "input": {"q": 1}}),
        ],
    )
    ds = load_dataset(path)
    assert [item.item_id for item in ds.items] == ["a", "b"]
    assert ds.items[0].expected == "hello"
    assert ds.items[1].expected is None
    assert ds.dataset_ref.name == "items"
    assert ds.dataset_ref.dataset_hash.startswith("sha256:")
    assert ds.dataset_ref.split_hash.startswith("sha256:")
    # default version pins content, not a mutable label
    assert ds.dataset_ref.version == ds.dataset_ref.dataset_hash.removeprefix("sha256:")[:12]


def test_explicit_name_and_version(tmp_path: Path) -> None:
    path = _write(tmp_path, [json.dumps({"item_id": "a"})])
    ds = load_dataset(path, name="gaia-validation", version="2024-11")
    assert ds.dataset_ref.name == "gaia-validation"
    assert ds.dataset_ref.version == "2024-11"


def test_dataset_hash_tracks_bytes_split_hash_tracks_item_ids(tmp_path: Path) -> None:
    a = load_dataset(_write(tmp_path, [json.dumps({"item_id": "a", "input": "x"})], "a.jsonl"))
    b = load_dataset(_write(tmp_path, [json.dumps({"item_id": "a", "input": "y"})], "b.jsonl"))
    assert a.dataset_ref.dataset_hash != b.dataset_ref.dataset_hash
    assert a.dataset_ref.split_hash == b.dataset_ref.split_hash  # same item sequence
    c = load_dataset(_write(tmp_path, [json.dumps({"item_id": "c", "input": "x"})], "c.jsonl"))
    assert a.dataset_ref.split_hash != c.dataset_ref.split_hash


def test_input_text_renders_non_strings_canonically() -> None:
    assert DatasetItem(item_id="a", input="plain").input_text() == "plain"
    assert DatasetItem(item_id="a", input={"b": 1, "a": 2}).input_text() == '{"a":2,"b":1}'


def test_missing_file_raises() -> None:
    with pytest.raises(DatasetError, match="cannot read"):
        load_dataset("/nonexistent/items.jsonl")


def test_invalid_json_line_raises_with_line_number(tmp_path: Path) -> None:
    path = _write(tmp_path, [json.dumps({"item_id": "a"}), "{not json"])
    with pytest.raises(DatasetError, match=r":2: invalid JSON"):
        load_dataset(path)


def test_missing_item_id_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, [json.dumps({"input": "x"})])
    with pytest.raises(DatasetError, match="invalid dataset item"):
        load_dataset(path)


def test_unknown_item_key_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, [json.dumps({"item_id": "a", "surprise": 1})])
    with pytest.raises(DatasetError, match="invalid dataset item"):
        load_dataset(path)


def test_duplicate_item_id_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, [json.dumps({"item_id": "a"}), json.dumps({"item_id": "a"})])
    with pytest.raises(DatasetError, match="duplicate item_id"):
        load_dataset(path)


def test_empty_dataset_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, [""])
    with pytest.raises(DatasetError, match="no items"):
        load_dataset(path)
