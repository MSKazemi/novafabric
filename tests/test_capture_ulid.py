import re
import time

from novafabric.capture._ulid import new_span_id, new_ulid

ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
SPAN_RE = re.compile(r"^[0-9a-f]{16}$")


def test_new_ulid_format() -> None:
    u = new_ulid()
    assert ULID_RE.match(u), f"bad ULID: {u}"


def test_new_ulid_length() -> None:
    assert len(new_ulid()) == 26


def test_new_ulid_sortable() -> None:
    time.sleep(0.002)
    a = new_ulid()
    b = new_ulid()
    assert a <= b


def test_new_ulid_unique() -> None:
    ids = {new_ulid() for _ in range(100)}
    assert len(ids) == 100


def test_new_span_id_format() -> None:
    s = new_span_id()
    assert SPAN_RE.match(s), f"bad span id: {s}"


def test_new_span_id_length() -> None:
    assert len(new_span_id()) == 16


def test_new_span_id_unique() -> None:
    ids = {new_span_id() for _ in range(50)}
    assert len(ids) == 50
