"""ADR-0172 (data slice) — the ``nova merkle-tree`` CLI.

Read-only. Loads a JSON document with a capsule's leaf hashes (+ optional labels / sealed root /
TSR) and renders the Merkle proof tree. Exit ``1`` only on a seal-root ``mismatch`` (tamper);
``0`` otherwise; ``2`` on missing/malformed input. Full leaf hashes are never printed — only
short prefixes (the rendering half of the ADR-0009 invariant).
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.trust.novaseal.merkle import _compute_root, _leaf_hash

runner = CliRunner()


def _leaves(n: int) -> list[str]:
    return [_leaf_hash(f"entry-{i}".encode()) for i in range(n)]


def _write(tmp_path: Path, doc) -> Path:
    p = tmp_path / "tree.json"
    p.write_text(json.dumps(doc))
    return p


def test_sealed_matching_exits_zero(tmp_path):
    leaves = _leaves(4)
    doc = {"leaf_hashes": leaves, "sealed_root": _compute_root(leaves)}
    result = runner.invoke(app, ["merkle-tree", str(_write(tmp_path, doc))])
    assert result.exit_code == 0, result.output
    assert "seal-root" in result.output.lower()
    assert "verified" in result.output.lower()


def test_mismatch_exits_one(tmp_path):
    leaves = _leaves(4)
    doc = {"leaf_hashes": leaves, "sealed_root": "00" * 32}
    result = runner.invoke(app, ["merkle-tree", str(_write(tmp_path, doc))])
    assert result.exit_code == 1, result.output
    assert "mismatch" in result.output.lower()


def test_unsealed_exits_zero(tmp_path):
    doc = {"leaf_hashes": _leaves(2)}
    result = runner.invoke(app, ["merkle-tree", str(_write(tmp_path, doc))])
    assert result.exit_code == 0, result.output
    assert "unsealed" in result.output.lower()


def test_full_leaf_hash_is_not_printed(tmp_path):
    leaves = _leaves(2)
    doc = {"leaf_hashes": leaves}
    result = runner.invoke(app, ["merkle-tree", str(_write(tmp_path, doc))])
    assert leaves[0] not in result.output  # only the prefix is shown


def test_json_output(tmp_path):
    leaves = _leaves(2)
    doc = {"leaf_hashes": leaves, "sealed_root": _compute_root(leaves)}
    result = runner.invoke(
        app, ["merkle-tree", str(_write(tmp_path, doc)), "--json", "--capsule-id", "run-3"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["capsule_id"] == "run-3"
    assert payload["computed_root"] == _compute_root(leaves)
    assert payload["sealed"] is True


def test_missing_file_exits_two(tmp_path):
    result = runner.invoke(app, ["merkle-tree", str(tmp_path / "nope.json")])
    assert result.exit_code == 2


def test_malformed_exits_two(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{nope")
    result = runner.invoke(app, ["merkle-tree", str(p)])
    assert result.exit_code == 2
