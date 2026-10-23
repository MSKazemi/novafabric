"""``POST /api/compliance/pii/erase`` validates the caller's ``retention_months``.

Adversarial review (2026-10-22): the route read ``int(body.get("retention_months", 6) or 6)``.
A negative value moved the Art.17(3)(b) retention window into the past, so erasure was
never deferred; a non-numeric string raised outside the handler's error mapping (500);
a float was silently truncated and ``true`` read as one month.

Pinned here: the value is an integer (not a bool, float or string) in
``0..MAX_RETENTION_MONTHS``, otherwise **422** and no key is touched. Absent or ``null``
keeps the documented default of 6. ``0`` means "no retention window", the same contract
as ``nova pii erase --retention-months 0``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from novafabric.pii.dek import DEKStore
from novafabric.pii.erasure_queue import MAX_RETENTION_MONTHS, parse_retention_months

SUBJECT = "user-77"
RUN = "01M5JDD2Y0VKD0XK5YZ4CZNND9"
TOKEN = "erasure-months-token-0123456789abcdef"
H = {"host": "127.0.0.1:4321", "authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    (h / "capsules" / RUN).mkdir(parents=True)
    (h / "capsules" / RUN / "redaction_manifest.json").write_text(
        json.dumps({"capsule_id": RUN})
    )
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("NOVAFABRIC_HOME", str(h))
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    store = DEKStore(h / "dek.db")
    store.get_or_create_dek(SUBJECT)
    store.close()
    return h


@pytest.fixture
def client(home: Path, tmp_path: Path) -> Iterator[Any]:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from novafabric.serve.app import create_app

    app_ = create_app(
        token=TOKEN, capsule_dir=home / "capsules", db_path=tmp_path / "r.db", static_dir=None
    )
    with TestClient(app_) as c:
        yield c


def _dek_exists(home: Path) -> bool:
    store = DEKStore(home / "dek.db")
    try:
        return store.get_dek(SUBJECT) is not None
    finally:
        store.close()


def _erase(client: Any, **extra: Any) -> Any:
    return client.post(
        "/api/compliance/pii/erase",
        headers=H,
        json={"subject_id": SUBJECT, "capsule_ids": [RUN], **extra},
    )


@pytest.mark.parametrize(
    "bad",
    [-1, -1000, MAX_RETENTION_MONTHS + 1, 10**12, "6", "abc", 1.5, 6.0, True, False, [6], {}],
)
def test_an_invalid_retention_months_is_422_and_no_key_is_touched(
    home: Path, client: Any, bad: Any
) -> None:
    r = _erase(client, retention_months=bad)
    assert r.status_code == 422, r.text
    assert "retention_months" in r.json()["detail"]
    assert _dek_exists(home)


def test_absent_or_null_keeps_the_default_window(home: Path, client: Any) -> None:
    for extra in ({}, {"retention_months": None}):
        r = _erase(client, **extra)
        assert r.status_code == 200, r.text
        assert r.json()["erased"] is False  # a fresh capsule is inside the 6-month window
        assert r.json()["receipt"]["retention_months"] == 6
    assert _dek_exists(home)


def test_zero_means_no_window_like_the_cli(home: Path, client: Any) -> None:
    r = _erase(client, retention_months=0)
    assert r.status_code == 200, r.text
    assert r.json()["erased"] is True
    assert not _dek_exists(home)


def test_an_in_range_value_is_honoured(home: Path, client: Any) -> None:
    r = _erase(client, retention_months=120)
    assert r.status_code == 200, r.text
    assert r.json()["receipt"]["retention_months"] == 120
    assert _dek_exists(home)


@pytest.mark.parametrize(
    ("value", "expected"), [(None, 6), (0, 0), (6, 6), (MAX_RETENTION_MONTHS, MAX_RETENTION_MONTHS)]
)
def test_parse_retention_months_accepts(value: Any, expected: int) -> None:
    assert parse_retention_months(value, default=6) == expected


@pytest.mark.parametrize("value", [-1, True, 1.0, "1", MAX_RETENTION_MONTHS + 1])
def test_parse_retention_months_refuses(value: Any) -> None:
    with pytest.raises(ValueError, match="retention_months"):
        parse_retention_months(value, default=6)
