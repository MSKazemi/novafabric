from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from novafabric.policy._engine import record_tool_permission_event
from novafabric.policy._models import (
    PolicyDecision,
    PolicyInput,
    PolicyResource,
    PolicySubject,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_input(
    action: str = "read",
    kind: str = "asset",
    ref: str = "model@v1",
    context: dict | None = None,
) -> PolicyInput:
    return PolicyInput(
        action=action,
        subject=PolicySubject(user="alice", roles=["viewer"]),
        resource=PolicyResource(kind=kind, ref=ref),
        context=context or {},
    )


def _allow(policy_path: str = "bundle/allow") -> PolicyDecision:
    return PolicyDecision(allow=True, reason="ok", policy_path=policy_path)


def _deny(policy_path: str = "bundle/deny") -> PolicyDecision:
    return PolicyDecision(allow=False, reason="blocked", policy_path=policy_path)


# ---------------------------------------------------------------------------
# Basic allowed / denied decisions
# ---------------------------------------------------------------------------


def test_allowed_decision_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="novafabric.policy._engine"):
        record_tool_permission_event(_allow(), _make_input(), capsule_id="cap-001")
    assert any("tool_permission_event_recorded" in r.message for r in caplog.records)


def test_denied_decision_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="novafabric.policy._engine"):
        record_tool_permission_event(_deny(), _make_input(action="write"), capsule_id="cap-002")
    assert any("tool_permission_event_recorded" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Permission level inference
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action,kind,expected_level",
    [
        ("network_call", "asset", "network"),
        ("read_file", "file", "filesystem"),
        ("exec_script", "asset", "execute"),
        ("write_result", "asset", "write"),
        ("read_data", "asset", "read"),
        ("something_else", "other", "execute"),  # conservative default
    ],
)
def test_permission_level_inference(
    action: str, kind: str, expected_level: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="novafabric.policy._engine"):
        record_tool_permission_event(
            _allow(),
            _make_input(action=action, kind=kind),
            capsule_id="cap-003",
        )
    # permission_level is passed via extra= so it's a LogRecord attribute, not in getMessage()
    assert any(
        getattr(r, "permission_level", None) == expected_level for r in caplog.records
    )


def test_explicit_permission_level_in_context(caplog: pytest.LogCaptureFixture) -> None:
    ctx = {"permission_level": "filesystem"}
    with caplog.at_level(logging.INFO, logger="novafabric.policy._engine"):
        record_tool_permission_event(
            _allow(),
            _make_input(context=ctx),
            capsule_id="cap-004",
        )
    assert any(
        getattr(r, "permission_level", None) == "filesystem" for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Human approval context fields
# ---------------------------------------------------------------------------


def test_human_approval_fields_propagated(caplog: pytest.LogCaptureFixture) -> None:
    ctx = {
        "human_approval_required": True,
        "human_approval_obtained": True,
        "approval_principal": "bob",
        "tool_name": "bash_runner",
        "tool_id": "tool-xyz",
    }
    with caplog.at_level(logging.INFO, logger="novafabric.policy._engine"):
        record_tool_permission_event(
            _allow(),
            _make_input(context=ctx),
            capsule_id="cap-005",
        )
    assert any("tool_permission_event_recorded" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# SQLite persistence via db_path
# ---------------------------------------------------------------------------


def test_records_to_sqlite_when_db_path_given(tmp_path: Path) -> None:
    db = tmp_path / "perm.db"
    record_tool_permission_event(
        _allow(),
        _make_input(action="read"),
        capsule_id="cap-sqlite-001",
        db_path=db,
    )
    from novafabric.compliance.tool_permission.index import PermissionEventIndex

    with PermissionEventIndex(db_path=db) as idx:
        rows = idx.query_by_capsule("cap-sqlite-001")
    assert len(rows) == 1
    assert rows[0].decision == "allowed"


def test_denied_event_persisted(tmp_path: Path) -> None:
    db = tmp_path / "perm.db"
    record_tool_permission_event(
        _deny(),
        _make_input(action="write"),
        capsule_id="cap-sqlite-002",
        db_path=db,
    )
    from novafabric.compliance.tool_permission.index import PermissionEventIndex

    with PermissionEventIndex(db_path=db) as idx:
        rows = idx.query_by_capsule("cap-sqlite-002")
    assert len(rows) == 1
    assert rows[0].decision == "denied"


def test_decision_latency_ms_accepted(tmp_path: Path) -> None:
    db = tmp_path / "perm.db"
    record_tool_permission_event(
        _allow(),
        _make_input(),
        capsule_id="cap-lat-001",
        decision_latency_ms=42,
        db_path=db,
    )
    from novafabric.compliance.tool_permission.index import PermissionEventIndex

    with PermissionEventIndex(db_path=db) as idx:
        rows = idx.query_by_capsule("cap-lat-001")
    assert rows[0].decision_latency_ms == 42


# ---------------------------------------------------------------------------
# Observational: errors never propagate
# ---------------------------------------------------------------------------


def test_exception_in_recording_is_swallowed(caplog: pytest.LogCaptureFixture) -> None:
    """Errors in event recording must never raise — only log at WARNING."""
    # ToolPermissionEvent is a local import inside the function, patch at source module.
    with (
        patch(
            "novafabric.compliance.tool_permission.models.ToolPermissionEvent",
            side_effect=RuntimeError("boom"),
        ),
        caplog.at_level(logging.WARNING, logger="novafabric.policy._engine"),
    ):
        # Must not raise
        record_tool_permission_event(_allow(), _make_input(), capsule_id="cap-err")
    assert any("tool_permission_event_recording_failed" in r.message for r in caplog.records)


def test_db_write_failure_is_swallowed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        patch(
            "novafabric.compliance.tool_permission.index.PermissionEventIndex.record",
            side_effect=OSError("disk full"),
        ),
        caplog.at_level(logging.WARNING, logger="novafabric.policy._engine"),
    ):
        record_tool_permission_event(
            _allow(),
            _make_input(),
            capsule_id="cap-dberr",
            db_path=tmp_path / "perm.db",
        )
    assert any("tool_permission_event_recording_failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# No db_path — log-only path
# ---------------------------------------------------------------------------


def test_no_db_path_does_not_persist(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="novafabric.policy._engine"):
        record_tool_permission_event(
            _allow(), _make_input(), capsule_id="cap-nodb", db_path=None
        )
    assert any("tool_permission_event_recorded" in r.message for r in caplog.records)
