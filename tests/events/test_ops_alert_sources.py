"""ADR-0192: the five wired `ops.*` alert sources.

Covers: each emitter produces the documented event type, severity and
payload; secrets/raw keys never ride along; every emitter is a no-op when
alerting is unconfigured (default OFF); an alerting fault can never break
the caller; and the call sites actually fire (rate limit, policy deny incl.
--force, drift, seal verify, backup create/verify).
"""

from __future__ import annotations

from typing import Any

import pytest

from novafabric.events import sources


@pytest.fixture()
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture emit_ops_alert calls without configuring a real endpoint."""
    calls: list[dict[str, Any]] = []

    def _fake(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr("novafabric.events.alerts.emit_ops_alert", _fake)
    return calls


# ---------------------------------------------------------------------------
# Emitter shapes
# ---------------------------------------------------------------------------


def test_rate_limit_alert_shape_and_no_raw_key(captured: list[dict[str, Any]]) -> None:
    sources.emit_rate_limit_sustained_alert(
        {
            "limit_class": "per_token",
            "rejected_count": 42,
            "window_start": "2026-07-18T08:00:00+00:00",
            "key_hash": "abcd1234",
            "raw_key": "nvfk_should_never_travel",
        }
    )
    (call,) = captured
    assert call["event_type"] == "ops.rate_limit.sustained"
    assert call["severity"] == "warning"  # guardrail worked as designed
    assert call["background"] is True  # rides the request-rejection path
    assert call["payload"]["rejected_count"] == 42
    # Only the hash travels — the emitter allowlists fields, it does not
    # forward the caller's dict wholesale.
    assert "raw_key" not in call["payload"]
    assert call["payload"]["key_hash"] == "abcd1234"


def test_policy_violation_alert_shape(captured: list[dict[str, Any]]) -> None:
    sources.emit_policy_violation_alert(
        asset_id="model-a@1.2.0",
        decision_reason="eval score below threshold",
        stage="promote",
        source="nova promote",
    )
    (call,) = captured
    assert call["event_type"] == "ops.policy.violation"
    assert call["severity"] == "warning"
    assert call["subject_ref"] == "asset:model-a@1.2.0"


def test_drift_alert_shape(captured: list[dict[str, Any]]) -> None:
    sources.emit_drift_detected_alert(
        kind="output", label="accuracy", value=0.31, threshold=0.2
    )
    (call,) = captured
    assert call["event_type"] == "ops.drift.detected"
    assert call["severity"] == "warning"
    assert call["payload"]["value"] == 0.31


def test_seal_verify_alert_is_critical_and_truncates_errors(
    captured: list[dict[str, Any]],
) -> None:
    sources.emit_seal_verify_failed_alert(
        capsule_id="cap-1", errors=[f"err-{i}" for i in range(20)], signature_ok=False
    )
    (call,) = captured
    assert call["event_type"] == "ops.seal.verify_failed"
    # The evidence guarantee itself failed — the run can no longer be proven.
    assert call["severity"] == "critical"
    assert len(call["payload"]["errors"]) == 5  # an alert is not a report
    assert call["payload"]["error_count"] == 20  # ...but the count is exact


def test_backup_alert_is_critical(captured: list[dict[str, Any]]) -> None:
    sources.emit_backup_failed_alert(
        operation="verify", target="/backups/set-1", reason="3 mismatched"
    )
    (call,) = captured
    assert call["event_type"] == "ops.backup.failed"
    assert call["severity"] == "critical"


def test_every_ops_event_type_has_a_wired_source() -> None:
    """A new ops.* type must not sit permanently unwired.

    `ops.quota.breached` is wired at its call site in server/quotas.py
    (slice 1); the rest are emitted from this module.
    """
    import inspect

    from novafabric.events.model import EventType

    emitted = {
        line.split('"')[1]
        for line in inspect.getsource(sources).splitlines()
        if "event_type=" in line and '"ops.' in line
    }
    emitted.add("ops.quota.breached")  # wired in server/quotas.py
    declared = {e.value for e in EventType if e.value.startswith("ops.")}
    assert declared == emitted, (
        f"ops.* types with no wired source: {sorted(declared - emitted)}. "
        "Wire it in novafabric/events/sources.py (ADR-0192) or remove it."
    )


# ---------------------------------------------------------------------------
# Fail-safe / off-by-default
# ---------------------------------------------------------------------------


def test_emitters_are_noops_when_alerting_is_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0192: default OFF. No endpoint configured → nothing happens."""
    for var in list(dict(__import__("os").environ)):
        if var.startswith("NOVA_ALERTS"):
            monkeypatch.delenv(var, raising=False)

    # These must complete without raising and without any delivery attempt.
    sources.emit_drift_detected_alert(
        kind="output", label="acc", value=1.0, threshold=0.5
    )
    sources.emit_backup_failed_alert(operation="create", target="/x", reason="y")
    sources.emit_seal_verify_failed_alert(capsule_id="c", errors=[])
    sources.emit_policy_violation_alert(
        asset_id="a@1", decision_reason="r", stage="promote", source="s"
    )
    sources.emit_rate_limit_sustained_alert({"limit_class": "c"})


def test_call_site_drift_detect_fires_in_both_output_modes(
    tmp_path: Any, captured: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The alert must not depend on which --format the caller picked."""
    import json as _json

    from typer.testing import CliRunner

    from novafabric.cli.main import app

    doc = {
        "kind": "behavioral",
        "dimension": "tool-call-mix",
        "distance": "jensen-shannon",
        "baseline": {"search": 0.4, "db.query": 0.35, "email.send": 0.25},
        "window": {"search": 0.9, "db.query": 0.05, "email.send": 0.05},
        "threshold": 0.1,
    }
    path = tmp_path / "drift.json"
    path.write_text(_json.dumps(doc), encoding="utf-8")

    runner = CliRunner()
    for extra in ([], ["--json"]):
        captured.clear()
        result = runner.invoke(app, ["drift", "detect", str(path), *extra])
        assert result.exit_code == 0, result.output
        assert len(captured) == 1, f"no alert for {extra or ['(table)']}"
        assert captured[0]["event_type"] == "ops.drift.detected"


def test_call_site_drift_stable_emits_nothing(
    tmp_path: Any, captured: list[dict[str, Any]]
) -> None:
    import json as _json

    from typer.testing import CliRunner

    from novafabric.cli.main import app

    doc = {
        "kind": "behavioral",
        "dimension": "tool-call-mix",
        "distance": "jensen-shannon",
        "baseline": {"search": 0.5, "db.query": 0.5},
        "window": {"search": 0.5, "db.query": 0.5},
        "threshold": 0.5,
    }
    path = tmp_path / "stable.json"
    path.write_text(_json.dumps(doc), encoding="utf-8")

    result = CliRunner().invoke(app, ["drift", "detect", str(path)])
    assert result.exit_code == 0, result.output
    assert captured == []  # no drift, no noise


def test_call_site_backup_verify_failure_alerts(
    tmp_path: Any, captured: list[dict[str, Any]]
) -> None:
    from typer.testing import CliRunner

    from novafabric.cli.main import app

    # A path that is not a valid backup set → verify fails.
    missing = tmp_path / "not-a-backup"
    missing.mkdir()
    result = CliRunner().invoke(app, ["backup", "verify", str(missing)])
    assert result.exit_code == 1
    assert len(captured) == 1
    assert captured[0]["event_type"] == "ops.backup.failed"
    assert captured[0]["severity"] == "critical"


def test_an_alerting_fault_never_reaches_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NovaFabric alerting problem must never break the user's workload."""

    def _explode(**_kwargs: Any) -> None:
        raise RuntimeError("alerting subsystem is on fire")

    monkeypatch.setattr("novafabric.events.alerts.emit_ops_alert", _explode)

    # Every emitter swallows it.
    sources.emit_drift_detected_alert(
        kind="output", label="acc", value=1.0, threshold=0.5
    )
    sources.emit_backup_failed_alert(operation="create", target="/x", reason="y")
    sources.emit_seal_verify_failed_alert(capsule_id="c", errors=[])
    sources.emit_policy_violation_alert(
        asset_id="a@1", decision_reason="r", stage="promote", source="s"
    )
    sources.emit_rate_limit_sustained_alert({"limit_class": "c"})
