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
"""Unit tests for the persisted erasure-request queue (ADR-0210 P1)."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import pytest

from novafabric.pii import erasure_queue as eq
from novafabric.pii.dek import DEKStore

SUBJECT = "seeded-subject-f9@example.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_dek(home: Path, subject: str = SUBJECT) -> None:
    store = DEKStore(home / "dek.db")
    store.get_or_create_dek(subject)
    store.close()


def _dek_exists(home: Path, subject: str = SUBJECT) -> bool:
    store = DEKStore(home / "dek.db")
    try:
        return store.get_dek(subject) is not None
    finally:
        store.close()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "nova-home"


@pytest.fixture
def queue(home: Path) -> eq.ErasureQueue:
    q = eq.open_erasure_queue(home)
    yield q
    q.close()


# ---------------------------------------------------------------------------
# Helpers under test
# ---------------------------------------------------------------------------


def test_subject_sha256_is_hex_sha256() -> None:
    assert eq.subject_sha256("abc") == hashlib.sha256(b"abc").hexdigest()


def test_receipt_sha256_is_over_canonical_bytes() -> None:
    receipt = {"b": 1, "a": [2, 3]}
    canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    assert eq.canonical_receipt_json(receipt) == canonical
    assert (
        eq.receipt_sha256_of(receipt)
        == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    )
    # Key order must not matter.
    assert eq.receipt_sha256_of({"a": [2, 3], "b": 1}) == eq.receipt_sha256_of(receipt)


def test_sanitize_error_detail_replaces_raw_subject() -> None:
    detail = f"No DEK found for subject_id={SUBJECT!r}."
    out = eq.sanitize_error_detail(detail, SUBJECT)
    assert SUBJECT not in out
    assert "sha256:" + eq.subject_sha256(SUBJECT)[:12] in out


def test_sanitize_error_detail_bounds_length() -> None:
    assert len(eq.sanitize_error_detail("x" * 10_000, "s")) == 500


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", 6), ("12", 12), ("bogus", 6), ("-3", 0)],
)
def test_retention_months_from_env(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: int
) -> None:
    monkeypatch.setenv(eq.RETENTION_MONTHS_ENV, raw)
    assert eq.retention_months_from_env() == expected


def test_retention_months_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(eq.RETENTION_MONTHS_ENV, raising=False)
    assert eq.retention_months_from_env() == 6


# ---------------------------------------------------------------------------
# Queue persistence
# ---------------------------------------------------------------------------


def test_create_request_persists_pending_before_execution(home: Path) -> None:
    q = eq.open_erasure_queue(home)
    rec = q.create_request(subject_id=SUBJECT, capsule_ids=["c1"], requested_by="fp1")
    q.close()

    # Reopen — the PENDING row was committed (crash-visible, ADR-0210 D2 step 2).
    q2 = eq.open_erasure_queue(home)
    try:
        got = q2.get(rec.request_id)
    finally:
        q2.close()
    assert got is not None
    assert got.state == eq.STATE_PENDING
    assert got.subject_id == SUBJECT
    assert got.subject_sha256 == eq.subject_sha256(SUBJECT)
    assert got.capsule_ids == ["c1"]
    assert got.reason == eq.DEFAULT_REASON
    assert got.requested_by == "fp1"
    assert got.executed_at is None
    assert got.receipt is None


def test_erasure_db_is_separate_from_registry(home: Path, queue: eq.ErasureQueue) -> None:
    assert (home / "erasure.db").exists()
    assert not (home / "registry.db").exists()


def test_finalize_completed_sets_receipt_and_hash(queue: eq.ErasureQueue) -> None:
    rec = queue.create_request(subject_id=SUBJECT)
    receipt = {"subject_id": SUBJECT, "method": "aes-256-gcm-dek-destruction"}
    final = queue.finalize(rec.request_id, state=eq.STATE_COMPLETED, receipt=receipt)
    assert final.state == eq.STATE_COMPLETED
    assert final.executed_at is not None
    assert final.receipt == receipt
    assert final.receipt_sha256 == eq.receipt_sha256_of(receipt)


def test_finalize_rejects_non_terminal_state(queue: eq.ErasureQueue) -> None:
    rec = queue.create_request(subject_id=SUBJECT)
    with pytest.raises(eq.ErasureQueueError, match="not a terminal state"):
        queue.finalize(rec.request_id, state=eq.STATE_PENDING, receipt=None)


def test_finalize_rejects_unknown_request_id(queue: eq.ErasureQueue) -> None:
    with pytest.raises(eq.ErasureQueueError, match="unknown request_id"):
        queue.finalize("01NOPE", state=eq.STATE_FAILED, receipt=None)


def test_find_pending_and_latest_completed(queue: eq.ErasureQueue) -> None:
    first = queue.create_request(subject_id=SUBJECT)
    assert queue.find_pending(SUBJECT).request_id == first.request_id
    assert queue.find_pending("other") is None

    done = queue.finalize(
        first.request_id, state=eq.STATE_COMPLETED, receipt={"k": "v"}
    )
    assert queue.find_pending(SUBJECT) is None
    assert queue.latest_completed(SUBJECT).request_id == done.request_id
    # The row itself must be excludable (already-erased check, ADR-0210 D3).
    assert (
        queue.latest_completed(SUBJECT, exclude_request_id=done.request_id) is None
    )


def test_list_requests_newest_first_filter_and_limit(queue: eq.ErasureQueue) -> None:
    a = queue.create_request(subject_id="s-a")
    b = queue.create_request(subject_id="s-b")
    c = queue.create_request(subject_id="s-a")
    ids = [r.request_id for r in queue.list_requests()]
    assert ids == [c.request_id, b.request_id, a.request_id]
    assert [r.request_id for r in queue.list_requests(subject_id="s-a")] == [
        c.request_id,
        a.request_id,
    ]
    assert len(queue.list_requests(limit=2)) == 2


def test_api_view_is_hash_only(queue: eq.ErasureQueue) -> None:
    rec = queue.create_request(subject_id=SUBJECT)
    view = rec.api_view()
    assert "subject_id" not in view
    assert view["subject_sha256"] == eq.subject_sha256(SUBJECT)
    assert view["state"] == eq.STATE_PENDING


# ---------------------------------------------------------------------------
# execute_request outcomes (ADR-0210 D3 state machine)
# ---------------------------------------------------------------------------


def test_execute_completed_destroys_dek_and_writes_receipt_file(home: Path) -> None:
    _seed_dek(home)
    q = eq.open_erasure_queue(home)
    try:
        rec = q.create_request(subject_id=SUBJECT, capsule_ids=["cap-1"])
        final = eq.execute_request(q, rec, home=home, retention_months=0)
    finally:
        q.close()

    assert final.state == eq.STATE_COMPLETED
    assert final.receipt is not None
    assert final.receipt["subject_id"] == SUBJECT
    assert final.receipt["method"] == "aes-256-gcm-dek-destruction"
    assert final.receipt["capsule_ids_affected"] == ["cap-1"]
    assert final.receipt_sha256 == eq.receipt_sha256_of(final.receipt)
    assert final.error_class is None
    # The DEK is provably gone.
    assert not _dek_exists(home)
    # Receipt file lands in the retention receipt dir with retention naming.
    receipt_path = home / "evidence" / "erasure" / f"{final.request_id}.receipt.json"
    assert receipt_path.is_file()
    assert json.loads(receipt_path.read_text())["subject_id"] == SUBJECT


def test_receipt_file_resolvable_by_restore_replay(home: Path) -> None:
    """ADR-0181 restore-time crypto-shred replay can resolve the subject."""
    from novafabric.backup.restore import _resolve_subject

    _seed_dek(home)
    q = eq.open_erasure_queue(home)
    try:
        rec = q.create_request(subject_id=SUBJECT)
        final = eq.execute_request(q, rec, home=home, retention_months=0)
    finally:
        q.close()
    receipt_ref = str(
        home / "evidence" / "erasure" / f"{final.request_id}.receipt.json"
    )
    assert _resolve_subject({"erasure_receipt_ref": receipt_ref}) == SUBJECT


def test_execute_deferred_within_retention_window(home: Path) -> None:
    _seed_dek(home)  # fresh DEK — always inside a 6-month window
    q = eq.open_erasure_queue(home)
    try:
        rec = q.create_request(subject_id=SUBJECT)
        final = eq.execute_request(q, rec, home=home, retention_months=6)
    finally:
        q.close()

    assert final.state == eq.STATE_DEFERRED
    assert "earliest_erasure_at" in final.receipt
    assert final.receipt["retention_months"] == 6
    # DEK untouched.
    assert _dek_exists(home)
    deferred_path = (
        home / "evidence" / "erasure" / f"{final.request_id}.deferred.receipt.json"
    )
    assert deferred_path.is_file()


def test_execute_unknown_subject_fails_closed(home: Path) -> None:
    q = eq.open_erasure_queue(home)
    try:
        rec = q.create_request(subject_id=SUBJECT)
        final = eq.execute_request(q, rec, home=home, retention_months=0)
    finally:
        q.close()

    assert final.state == eq.STATE_FAILED
    assert final.error_class == "subject_not_found"
    assert final.receipt["kind"] == "error"
    # Sanitized: raw subject never in the error surfaces.
    assert SUBJECT not in (final.error_detail or "")
    assert SUBJECT not in json.dumps(final.receipt)


def test_execute_unknown_subject_after_completion_is_already_erased(home: Path) -> None:
    _seed_dek(home)
    q = eq.open_erasure_queue(home)
    try:
        first = q.create_request(subject_id=SUBJECT)
        first_final = eq.execute_request(q, first, home=home, retention_months=0)
        assert first_final.state == eq.STATE_COMPLETED

        second = q.create_request(subject_id=SUBJECT)
        second_final = eq.execute_request(q, second, home=home, retention_months=0)
    finally:
        q.close()

    assert second_final.state == eq.STATE_COMPLETED
    assert second_final.receipt["kind"] == "already_erased"
    assert second_final.receipt["prior_request_id"] == first_final.request_id
    assert (
        second_final.receipt["prior_receipt_sha256"] == first_final.receipt_sha256
    )


def test_execute_machinery_failure_marks_failed_never_pending(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _BrokenStore:
        def erase_subject(self, **_kwargs: object) -> None:
            raise RuntimeError(f"disk exploded while erasing {SUBJECT}")

        def close(self) -> None:
            pass

    monkeypatch.setattr(eq, "open_dek_store", lambda _home: _BrokenStore())
    q = eq.open_erasure_queue(home)
    try:
        rec = q.create_request(subject_id=SUBJECT)
        final = eq.execute_request(q, rec, home=home, retention_months=0)
        # Fail-closed: the persisted row is FAILED, never left PENDING.
        assert q.get(rec.request_id).state == eq.STATE_FAILED
    finally:
        q.close()

    assert final.state == eq.STATE_FAILED
    assert final.error_class == "RuntimeError"
    assert SUBJECT not in (final.error_detail or "")
    assert "sha256:" in final.error_detail


def test_execute_on_terminal_record_is_a_noop(home: Path) -> None:
    q = eq.open_erasure_queue(home)
    try:
        rec = q.create_request(subject_id=SUBJECT)
        final = q.finalize(rec.request_id, state=eq.STATE_FAILED, receipt=None)
        again = eq.execute_request(q, final, home=home, retention_months=0)
    finally:
        q.close()
    assert again == final


def test_receipt_file_write_failure_does_not_undo_completion(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dek(home)
    # Make the receipt *directory path* an existing file so mkdir/write fails.
    evidence = home / "evidence"
    evidence.mkdir(parents=True)
    (evidence / "erasure").write_text("not a directory")

    q = eq.open_erasure_queue(home)
    try:
        rec = q.create_request(subject_id=SUBJECT)
        final = eq.execute_request(q, rec, home=home, retention_months=0)
    finally:
        q.close()
    # The erase happened; the row is the durable record.
    assert final.state == eq.STATE_COMPLETED
    assert not _dek_exists(home)


# ---------------------------------------------------------------------------
# Hash-only structured logging (normative, ADR-0210 D1)
# ---------------------------------------------------------------------------


def test_structured_logs_never_carry_raw_subject(
    home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Seeded-grep over every log record of the full flow (incl. DEK store)."""
    with caplog.at_level(logging.DEBUG):
        _seed_dek(home)
        q = eq.open_erasure_queue(home)
        try:
            rec = q.create_request(subject_id=SUBJECT)
            eq.execute_request(q, rec, home=home, retention_months=0)
            # Failure path logs too.
            rec2 = q.create_request(subject_id=SUBJECT + "-missing")
            eq.execute_request(q, rec2, home=home, retention_months=0)
        finally:
            q.close()

    assert caplog.records, "expected structured log records from the erasure flow"
    for record in caplog.records:
        blob = record.getMessage() + repr(vars(record))
        assert SUBJECT not in blob, f"raw subject_id leaked into log: {blob[:200]}"
