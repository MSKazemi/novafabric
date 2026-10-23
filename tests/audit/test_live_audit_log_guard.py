"""No test can write the developer's live audit log (review r6, 2026-10-23).

``tests/conftest.py`` pointed the audit log at a per-test tmp dir, yet a full run still
appended ``actor: cli`` ``policy.allow`` entries for ``evidence_export`` to
``~/.local/share/novafabric/audit.jsonl``. Two causes, both pinned here:

1. **Module-scoped fixtures ran outside the per-test environment.** Pytest sets up a
   module-scoped fixture *before* the function-scoped autouse fixture, so a fixture that
   ran ``nova export-evidence`` (``tests/seal/test_verify_evidence_bundle_signatures.py``,
   ``tests/serve/test_evidence_verify_matches_cli.py``) saw the developer's shell and its
   policy decision went to the live log. A session-scoped hermetic environment now
   covers everything that runs before or between tests.
2. **The export's policy gate ignored the caller's audit path.**
   ``EvidenceBundleBuilder(audit_log_path=...)`` used that path for the bundle's own
   audit section but appended the policy decision to ``resolve_audit_log_path()``.

And a guard: inside the test session, opening ``AuditLog`` on the live path raises and
fails the test, whatever code path got there.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import _live_audit_guard as guard
import pytest

from novafabric.audit import AuditLog, resolve_audit_log_path


@pytest.fixture(scope="module")
def module_scoped_audit_path() -> Path:
    """What a module-scoped fixture resolves: it runs before any function fixture."""
    return resolve_audit_log_path()


def test_a_module_scoped_fixture_resolves_the_audit_log_under_the_test_tmp(
    module_scoped_audit_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    base = tmp_path_factory.getbasetemp().resolve()
    assert module_scoped_audit_path.resolve().is_relative_to(base), (
        f"a module-scoped fixture would write the audit log at {module_scoped_audit_path}"
    )


def test_the_guard_refuses_to_open_the_live_audit_log() -> None:
    live = guard.LIVE_AUDIT_LOGS[0]
    before = (live.stat().st_size, live.stat().st_mtime_ns) if live.exists() else None
    try:
        with pytest.raises(guard.LiveAuditLogWriteError):
            AuditLog(live)
    finally:
        guard.take_violations()  # this test provoked it on purpose
    after = (live.stat().st_size, live.stat().st_mtime_ns) if live.exists() else None
    assert before == after


def test_the_guard_protects_the_real_home_default() -> None:
    import os
    import pwd

    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    expected = real_home / ".local" / "share" / "novafabric" / "audit.jsonl"
    assert expected in guard.LIVE_AUDIT_LOGS


def test_evidence_export_appends_its_policy_decision_to_the_callers_audit_log(
    tmp_path: Path,
) -> None:
    from novafabric.capture.orchestrator import CaptureOrchestrator
    from novafabric.evidence.bundle import EvidenceBundleBuilder
    from novafabric.evidence.signing import LocalSigner, generate_keypair

    capsule = (
        CaptureOrchestrator(base_dir=tmp_path / "runs")
        .run(command=[sys.executable, "-c", "pass"])
        .capsule_dir
    )
    priv, _ = generate_keypair(tmp_path / "keys")
    callers_log = tmp_path / "callers-audit.jsonl"

    EvidenceBundleBuilder(
        capsule, LocalSigner(priv), tmp_path / "b.zip", audit_log_path=callers_log
    ).build()

    rows = [json.loads(line) for line in callers_log.read_text().splitlines()]
    assert [r["details"]["action"] for r in rows if r["event_type"] == "policy.allow"] == [
        "evidence_export"
    ]
    resolved = resolve_audit_log_path()
    assert not resolved.exists() or "evidence_export" not in resolved.read_text()
