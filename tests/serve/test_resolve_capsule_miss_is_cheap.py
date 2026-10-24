"""A run id that matches no capsule costs no manifest parse per request (review serve-10).

Every miss in ``_resolve_capsule`` fell back to parsing every manifest in the store,
synchronously on the event loop: an authenticated client could make each request
cost O(N) YAML/DSSE parses.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi import HTTPException

from novafabric.serve import app as serve_app

ULID_A = "01HZZZZZZZZZZZZZZZZZZZZZZA"
ULID_B = "01HZZZZZZZZZZZZZZZZZZZZZZB"


def _capsule(store: Path, dirname: str, run_id: str) -> Path:
    d = store / dirname
    d.mkdir()
    (d / "capsule.yaml").write_text(yaml.safe_dump({"run_id": run_id, "status": "success"}))
    return d


@pytest.fixture()
def parses(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    seen: list[Path] = []
    real = serve_app.load_capsule_manifest

    def counting(d: Path) -> dict[str, object]:
        seen.append(d)
        return real(d)

    monkeypatch.setattr(serve_app, "load_capsule_manifest", counting)
    return seen


def test_a_manifest_run_id_still_resolves(tmp_path: Path, parses: list[Path]) -> None:
    store = tmp_path / "store"
    store.mkdir()
    d = _capsule(store, "adapter-capsule", ULID_A)
    assert serve_app._resolve_capsule(ULID_A, store) == d


def test_repeated_misses_parse_the_store_at_most_once(tmp_path: Path, parses: list[Path]) -> None:
    store = tmp_path / "store"
    store.mkdir()
    for i in range(5):
        _capsule(store, f"c{i}", f"01HZZZZZZZZZZZZZZZZZZZZZZ{i}")
    for _ in range(3):
        with pytest.raises(HTTPException) as exc:
            serve_app._resolve_capsule(ULID_B, store)
        assert exc.value.status_code == 404
    assert len(parses) <= 5


def test_a_run_id_that_is_not_a_ulid_parses_nothing(tmp_path: Path, parses: list[Path]) -> None:
    store = tmp_path / "store"
    store.mkdir()
    _capsule(store, "c0", ULID_A)
    with pytest.raises(HTTPException):
        serve_app._resolve_capsule("not-a-run-id", store)
    assert parses == []


def test_a_capsule_added_after_a_miss_is_found(tmp_path: Path, parses: list[Path]) -> None:
    store = tmp_path / "store"
    store.mkdir()
    _capsule(store, "c0", ULID_A)
    with pytest.raises(HTTPException):
        serve_app._resolve_capsule(ULID_B, store)
    d = _capsule(store, "late", ULID_B)
    assert serve_app._resolve_capsule(ULID_B, store) == d


def test_a_manifest_written_into_an_existing_dir_after_a_miss_is_found(
    tmp_path: Path, parses: list[Path]
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    (store / "in-progress").mkdir()  # the capture made the dir, no manifest yet
    with pytest.raises(HTTPException):
        serve_app._resolve_capsule(ULID_B, store)
    (store / "in-progress" / "capsule.yaml").write_text(yaml.safe_dump({"run_id": ULID_B}))
    assert serve_app._resolve_capsule(ULID_B, store) == store / "in-progress"
