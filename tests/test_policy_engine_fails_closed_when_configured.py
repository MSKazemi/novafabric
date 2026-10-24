"""A configured policy bundle that cannot be evaluated refuses; unconfigured local mode stays open.

Review X4 / rlr-03. ``get_policy_engine`` returned :class:`NoopEngine` (allow
everything) whenever ``opa`` was not on PATH — including when the operator had
pointed ``NOVAFABRIC_POLICY_BUNDLE_PATH`` at their own policies, so every gate
those policies were meant to close (``promote``, ``evidence_export``,
``replay_mutating``) silently opened because a binary was missing. Local mode
without any configured policy keeps the documented behaviour: OPA is optional
and the built-in gates are then disabled, with a warning.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from novafabric.policy import (
    FailClosedEngine,
    NoopEngine,
    PolicyInput,
    PolicyResource,
    PolicySubject,
    get_policy_engine,
)

_INPUT = PolicyInput(
    action="evidence_export",
    subject=PolicySubject(user="u"),
    resource=PolicyResource(kind="capsule", ref="r1"),
)


@pytest.fixture
def no_opa(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)


def test_configured_bundle_without_opa_denies_every_gate(
    no_opa: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVAFABRIC_POLICY_BUNDLE_PATH", str(tmp_path))
    with pytest.warns(UserWarning, match="fail closed"):
        engine = get_policy_engine()
    assert isinstance(engine, FailClosedEngine)
    decision = engine.evaluate(_INPUT)
    assert decision.allow is False
    assert "NOVAFABRIC_POLICY_BUNDLE_PATH" in decision.reason and "opa" in decision.reason
    assert decision.decision_id  # still a recorded, auditable decision


def test_blank_bundle_variable_counts_as_unset(
    no_opa: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVAFABRIC_POLICY_BUNDLE_PATH", "  ")
    with pytest.warns(UserWarning, match="policy gates disabled"):
        assert isinstance(get_policy_engine(), NoopEngine)


def test_unconfigured_local_mode_without_opa_stays_open(
    no_opa: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOVAFABRIC_POLICY_BUNDLE_PATH", raising=False)
    with pytest.warns(UserWarning, match="policy gates disabled"):
        engine = get_policy_engine()
    assert isinstance(engine, NoopEngine)
    assert engine.evaluate(_INPUT).allow is True


def test_fail_closed_engine_is_not_mistaken_for_an_allow_by_the_promote_gate() -> None:
    # registry/service.py treats only an explicit allow from a real engine as an allow.
    assert not issubclass(FailClosedEngine, NoopEngine)
    assert FailClosedEngine("why").evaluate(_INPUT).allow is False
