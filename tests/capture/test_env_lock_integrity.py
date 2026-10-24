"""``env.lock`` hashes the whole lock file and records every package (review capture-runners-11).

The lock-file hash covered only the first 64 KiB — two ``uv.lock`` files that differ
past byte 65 536 hashed the same, and a replay attestation then certified unequal
environments as equal. ``installed_packages`` was cut to 200 entries in arbitrary
order with no marker while the lock said ``deterministic``. And ADR-0007 accepts
``poetry.lock``, ``pdm.lock`` and ``requirements.txt`` only "with a best-effort
downgrade note", but they were marked ``deterministic``.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
from pathlib import Path

import pytest

from novafabric.capture.env import capture_environment

CREATED = "2026-10-24T00:00:00Z"
RUN = "01HXAY7M5JZ8R7K4P9DPBYK2WX"


def _lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, body: bytes) -> dict:  # type: ignore[type-arg]
    work = tmp_path / name.replace(".", "_")
    work.mkdir(parents=True)
    (work / name).write_bytes(body)
    monkeypatch.chdir(work)
    return capture_environment(CREATED, RUN)


def test_lock_files_that_differ_past_64_kib_hash_differently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = b"# uv.lock\n" + b"x" * 70_000
    a = _lock(tmp_path / "a", monkeypatch, "uv.lock", head + b"\npkg==1\n")
    b = _lock(tmp_path / "b", monkeypatch, "uv.lock", head + b"\npkg==2\n")
    assert a["python"]["lock_file_hash"] != b["python"]["lock_file_hash"]


def test_the_hash_is_the_sha256_of_the_raw_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"version = 1\n" + bytes(range(256)) * 400  # not UTF-8, > 64 KiB
    env = _lock(tmp_path, monkeypatch, "uv.lock", body)
    assert env["python"]["lock_file_hash"] == "sha256:" + hashlib.sha256(body).hexdigest()
    assert env["mode"] == "deterministic"


@pytest.mark.parametrize("name", ["poetry.lock", "pdm.lock", "requirements.txt"])
def test_a_lock_that_is_not_uv_is_best_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    env = _lock(tmp_path, monkeypatch, name, b"pkg==1\n")
    assert env["mode"] == "best-effort"
    (reason,) = env["best_effort_reasons"]
    assert reason["kind"] == "other" and name in reason["detail"]


def test_every_installed_package_is_recorded_in_name_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _lock(tmp_path, monkeypatch, "uv.lock", b"version = 1\n")
    names = [p["name"] for p in env["python"]["installed_packages"]]
    expected = {d.metadata.get("Name") for d in importlib.metadata.distributions()} - {None, ""}
    assert set(names) == expected
    assert names == sorted(names, key=str.lower)
