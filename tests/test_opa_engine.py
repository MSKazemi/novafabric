"""Tests for OpaEngine subprocess integration (Track P-1, v0.8)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from novafabric.policy import (
    OpaEngine,
    OpaNotFoundError,
    PolicyInput,
    PolicyResource,
    PolicySubject,
)


def _make_input(action: str = "promote") -> PolicyInput:
    return PolicyInput(
        action=action,
        subject=PolicySubject(user="alice", roles=["admin"]),
        resource=PolicyResource(
            kind="capsule",
            ref="capsule-001",
            eval_score=0.95,
            unsafe_skips=0,
        ),
    )


def _opa_ok(allow: bool, reason: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = 0
    m.stdout = json.dumps(
        {"result": [{"expressions": [{"value": {"allow": allow, "reason": reason}}]}]}
    )
    m.stderr = ""
    return m


def test_opa_engine_allow() -> None:
    """OpaEngine returns allow=True when opa returns allow=true."""
    with patch("subprocess.run", return_value=_opa_ok(allow=True, reason="")) as mock_run:
        engine = OpaEngine()
        decision = engine.evaluate(_make_input("promote"))

    assert decision.allow is True
    assert decision.decision_id != ""
    mock_run.assert_called_once()


def test_opa_engine_deny() -> None:
    """OpaEngine returns allow=False when opa returns allow=false."""
    with patch(
        "subprocess.run",
        return_value=_opa_ok(allow=False, reason="eval score below threshold"),
    ):
        engine = OpaEngine()
        decision = engine.evaluate(_make_input("promote"))

    assert decision.allow is False
    assert "eval score" in decision.reason


def test_opa_engine_missing_binary() -> None:
    """FileNotFoundError from subprocess must be re-raised as OpaNotFoundError."""
    with patch("subprocess.run", side_effect=FileNotFoundError("opa not found")):
        engine = OpaEngine()
        with pytest.raises(OpaNotFoundError):
            engine.evaluate(_make_input("promote"))


def test_opa_engine_unknown_action() -> None:
    """An action not in the registry must return allow=False without calling opa."""
    with patch("subprocess.run") as mock_run:
        engine = OpaEngine()
        decision = engine.evaluate(_make_input("unknown_action_xyz"))

    assert decision.allow is False
    assert "no policy registered" in decision.reason
    mock_run.assert_not_called()


def test_opa_engine_opa_error() -> None:
    """OPA exits non-zero → allow=False with error message."""
    m = MagicMock()
    m.returncode = 1
    m.stdout = ""
    m.stderr = "bundle not found: /nonexistent"
    with patch("subprocess.run", return_value=m):
        engine = OpaEngine()
        d = engine.evaluate(_make_input())
    assert not d.allow
    assert "opa eval error" in d.reason


def test_opa_engine_parse_error() -> None:
    """OPA returns non-standard JSON → allow=False with parse error."""
    m = MagicMock()
    m.returncode = 0
    m.stdout = "{}"  # missing result key
    m.stderr = ""
    with patch("subprocess.run", return_value=m):
        engine = OpaEngine()
        d = engine.evaluate(_make_input())
    assert not d.allow
    assert "parse error" in d.reason


def test_find_bundle_path_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """NOVAFABRIC_POLICY_BUNDLE_PATH env var overrides default bundle location."""
    monkeypatch.setenv("NOVAFABRIC_POLICY_BUNDLE_PATH", "/custom/policy/bundle")
    from novafabric.policy import _opa_engine

    path = _opa_engine._find_bundle_path()
    assert path == Path("/custom/policy/bundle")


def test_opa_engine_timeout() -> None:
    """TimeoutExpired from subprocess returns allow=False with timeout reason."""
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="opa eval", timeout=30),
    ):
        engine = OpaEngine()
        d = engine.evaluate(_make_input("promote"))
    assert not d.allow
    assert "timed out" in d.reason


def test_opa_engine_explain_true() -> None:
    """explain=True calls _explain and populates trace_text in the decision."""
    with patch(
        "subprocess.run",
        side_effect=[
            _opa_ok(allow=True),  # main eval call
            MagicMock(stdout="Enter data.novafabric\n", stderr=""),  # _explain call
        ],
    ):
        engine = OpaEngine()
        d = engine.evaluate(_make_input("promote"), explain=True)
    assert d.allow is True
    assert d.trace_text is not None
    assert "Enter" in d.trace_text


def test_explain_fallback_on_file_not_found() -> None:
    """_explain returns fallback string when opa binary is missing."""
    with patch("subprocess.run", side_effect=FileNotFoundError):
        engine = OpaEngine()
        trace = engine._explain("{}", "data.novafabric.defaults.promote_gate")
    assert trace == "(trace unavailable)"


def test_explain_fallback_on_timeout() -> None:
    """_explain returns fallback string on TimeoutExpired."""
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="opa eval", timeout=30),
    ):
        engine = OpaEngine()
        trace = engine._explain("{}", "data.novafabric.defaults.promote_gate")
    assert trace == "(trace unavailable)"


# ---------- custom policy_source tests ----------

def _opa_bool_ok(allow: bool) -> MagicMock:
    """Simulate OPA returning a bare boolean (data.pkg.allow query)."""
    m = MagicMock()
    m.returncode = 0
    m.stdout = json.dumps(
        {"result": [{"expressions": [{"value": allow}]}]}
    )
    m.stderr = ""
    return m


def test_parse_package_from_rego_standard() -> None:
    from novafabric.policy._opa_engine import _parse_package_from_rego
    src = "package novafabric.authz\ndefault allow := false"
    assert _parse_package_from_rego(src) == "novafabric.authz"


def test_parse_package_from_rego_missing() -> None:
    from novafabric.policy._opa_engine import _parse_package_from_rego
    assert _parse_package_from_rego("default allow := false") is None


def test_opa_engine_custom_source_allow() -> None:
    """When policy_source is provided it is evaluated instead of the bundle."""
    rego = "package novafabric.authz\ndefault allow := true"
    with patch("subprocess.run", return_value=_opa_bool_ok(allow=True)) as mock_run:
        engine = OpaEngine()
        d = engine.evaluate(_make_input("promote"), policy_source=rego)

    assert d.allow is True
    assert d.policy_path == "custom:data.novafabric.authz.allow"
    # OPA must have been called with a temp-dir path (not the bundle)
    call_args = mock_run.call_args[0][0]
    assert "--data" in call_args
    bundle_arg = call_args[call_args.index("--data") + 1]
    assert "novafabric/policies" not in bundle_arg


def test_opa_engine_custom_source_deny() -> None:
    rego = "package novafabric.authz\ndefault allow := false"
    with patch("subprocess.run", return_value=_opa_bool_ok(allow=False)):
        engine = OpaEngine()
        d = engine.evaluate(_make_input("promote"), policy_source=rego)

    assert d.allow is False
    assert d.policy_path == "custom:data.novafabric.authz.allow"


def test_opa_engine_custom_source_opa_error() -> None:
    """OPA error during custom evaluation returns DENY with error reason."""
    m = MagicMock()
    m.returncode = 1
    m.stdout = ""
    m.stderr = "parse error"
    with patch("subprocess.run", return_value=m):
        engine = OpaEngine()
        d = engine.evaluate(_make_input("promote"), policy_source="bad rego")
    assert not d.allow
    assert "opa eval error" in d.reason


def test_opa_engine_custom_source_missing_binary() -> None:
    rego = "package novafabric.authz\ndefault allow := true"
    with patch("subprocess.run", side_effect=FileNotFoundError("opa not found")):
        engine = OpaEngine()
        with pytest.raises(OpaNotFoundError):
            engine.evaluate(_make_input("promote"), policy_source=rego)


def test_opa_engine_empty_policy_source_uses_bundle() -> None:
    """Empty/whitespace policy_source falls back to bundled policy evaluation."""
    with patch("subprocess.run", return_value=_opa_ok(allow=True)) as mock_run:
        engine = OpaEngine()
        d = engine.evaluate(_make_input("promote"), policy_source="   ")

    assert d.allow is True
    # bundle path (not a temp dir) must have been used
    call_args = mock_run.call_args[0][0]
    bundle_arg = call_args[call_args.index("--data") + 1]
    assert "novafabric" in bundle_arg
