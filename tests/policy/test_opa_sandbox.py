"""serve-01 defence in depth: every ``opa`` NovaFabric starts is sandboxed.

The HTTP attack is pinned in ``tests/serve/test_policy_rego_sandbox.py``. These
tests pin the layer underneath, for every caller of ``opa``: the policy engine
(used by promote, replay, evidence export and ``nova serve``), the
``nova policy test`` CLI, and the module's own failure modes. The sandbox must
fail **closed**: an ``opa`` that cannot be restricted does not run.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from _fake_opa import (
    FakeOpa,
    assert_no_server_env,
    assert_restricted_capabilities,
    clear_capabilities_cache,
)
from typer.testing import CliRunner

from novafabric.policy import (
    OpaEngine,
    OpaSandboxError,
    PolicyInput,
    PolicyResource,
    PolicySubject,
    _opa_sandbox,
)

SECRET = "server-secret-that-opa-must-never-see"

# Every test here resolves the fake through shutil.which, which conftest hides
# for `opa` unless a test opts out.
pytestmark = pytest.mark.real_opa


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeOpa]:
    f = FakeOpa(tmp_path)
    monkeypatch.setenv("PATH", f"{f.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("NOVAFABRIC_SERVE_TOKEN", SECRET)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", SECRET)
    clear_capabilities_cache()
    yield f
    clear_capabilities_cache()


def _input() -> PolicyInput:
    return PolicyInput(
        action="promote",
        subject=PolicySubject(user="alice", roles=["admin"]),
        resource=PolicyResource(kind="capsule", ref="c-1", eval_score=0.9, unsafe_skips=0),
    )


# -- the environment ---------------------------------------------------------


def test_scrubbed_env_is_a_constant_path_and_nothing_else() -> None:
    assert _opa_sandbox.scrubbed_env() == {"PATH": os.defpath}


@pytest.mark.parametrize("policy_source", [None, "package p\nallow := true\n"])
def test_engine_never_passes_the_process_environment(
    fake: FakeOpa, policy_source: str | None
) -> None:
    decision = OpaEngine().evaluate(_input(), explain=True, policy_source=policy_source)
    assert SECRET not in decision.reason
    assert SECRET not in (decision.trace_text or "")
    calls = fake.calls()
    assert [c["argv"][0] for c in calls] == ["capabilities", "eval", "eval"]
    for call in calls:
        assert_no_server_env(call["env"], SECRET)
    for call in fake.evals():
        assert_restricted_capabilities(call)


# -- the capabilities file ---------------------------------------------------


def test_capabilities_file_is_private_and_cached(fake: FakeOpa) -> None:
    exe = str(fake.exe)
    first = _opa_sandbox.restricted_capabilities(exe)
    second = _opa_sandbox.restricted_capabilities(exe)
    assert first == second
    assert [c["argv"][0] for c in fake.calls()] == ["capabilities"], "probe runs once"
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    assert stat.S_IMODE(first.parent.stat().st_mode) == 0o700
    doc = json.loads(first.read_text())
    assert {b["name"] for b in doc["builtins"]} == {"abs", "json.marshal"}
    assert doc["allow_net"] == []


def test_capabilities_are_rederived_when_the_binary_changes(fake: FakeOpa) -> None:
    exe = str(fake.exe)
    _opa_sandbox.restricted_capabilities(exe)
    st = fake.exe.stat()
    os.utime(fake.exe, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    _opa_sandbox.restricted_capabilities(exe)
    assert [c["argv"][0] for c in fake.calls()] == ["capabilities", "capabilities"]


def test_clearing_the_cache_deletes_the_files(fake: FakeOpa) -> None:
    path = _opa_sandbox.restricted_capabilities(str(fake.exe))
    clear_capabilities_cache()
    assert not path.exists()


@pytest.mark.parametrize(
    "doc",
    [
        [],
        {},
        {"builtins": "abs"},
        {"builtins": []},
        {"builtins": [{"name": "opa.runtime"}, {"name": "http.send"}]},
    ],
    ids=["list", "empty", "not-a-list", "no-builtins", "only-denied"],
)
def test_unusable_capabilities_documents_are_refused(doc: object) -> None:
    with pytest.raises(OpaSandboxError):
        _opa_sandbox._restrict(doc)


def test_restrict_keeps_everything_but_the_denied_builtins() -> None:
    doc = {
        "builtins": [{"name": "abs"}, {"name": "opa.runtime"}, {"name": "http.send"}],
        "features": ["rego_v1"],
        "future_keywords": ["if"],
        "wasm_abi_versions": [{"version": 1}],
    }
    out = _opa_sandbox._restrict(doc)
    assert out["builtins"] == [{"name": "abs"}]
    assert out["features"] == ["rego_v1"]
    assert out["future_keywords"] == ["if"]
    assert out["allow_net"] == []
    assert len(doc["builtins"]) == 3, "the input document is not mutated"


# -- failing closed ----------------------------------------------------------


def test_an_opa_that_cannot_be_restricted_never_evaluates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An opa too old for `capabilities --current` is refused, never run unrestricted."""
    old = FakeOpa(tmp_path, capabilities_exit=1)
    monkeypatch.setenv("PATH", f"{old.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    clear_capabilities_cache()
    decision = OpaEngine().evaluate(_input(), policy_source="package p\nallow := true\n")
    assert decision.allow is False
    assert "sandbox unavailable" in decision.reason
    assert old.evals() == [], "no eval may run without the restricted capabilities"


def test_missing_opa_is_still_opa_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    from novafabric.policy import OpaNotFoundError

    monkeypatch.setattr(_opa_sandbox.shutil, "which", lambda *_a, **_k: None)
    with pytest.raises(OpaNotFoundError):
        _opa_sandbox.prepare()


# -- the query built from caller Rego ----------------------------------------


@pytest.mark.parametrize(
    "package",
    ["x; opa.runtime()", "x.allow == true", "x[0]", "1x", "x..y", "x-y"],
)
def test_a_package_name_that_is_not_dotted_identifiers_is_refused(
    fake: FakeOpa, package: str
) -> None:
    decision = OpaEngine().evaluate(
        _input(), policy_source=f"package {package}\nallow := true\n"
    )
    assert decision.allow is False
    assert "invalid Rego package name" in decision.reason
    assert fake.evals() == []


def test_opa_compile_errors_on_stdout_reach_the_reason(opa_sandbox_stub: object) -> None:
    """--format json reports compile errors on stdout; the reason must not be blank."""
    from unittest.mock import MagicMock, patch

    m = MagicMock(returncode=1, stderr="", stdout='{"errors":[{"message":"undefined function opa.runtime"}]}')
    with patch("subprocess.run", return_value=m):
        decision = OpaEngine().evaluate(_input(), policy_source="package p\nallow := true\n")
    assert decision.allow is False
    assert "undefined function opa.runtime" in decision.reason


# -- the CLI -----------------------------------------------------------------


def test_nova_policy_test_runs_opa_sandboxed(fake: FakeOpa, tmp_path: Path) -> None:
    from novafabric.cli.main import app

    result = CliRunner().invoke(app, ["policy", "test", "--bundle", str(tmp_path)])
    assert result.exit_code == 0, result.output
    (call,) = fake.evals()
    assert call["argv"][0] == "test"
    assert_no_server_env(call["env"], SECRET)
    assert_restricted_capabilities(call)


def test_nova_policy_test_refuses_an_unrestrictable_opa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.cli.main import app

    old = FakeOpa(tmp_path, capabilities_exit=1)
    monkeypatch.setenv("PATH", f"{old.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    clear_capabilities_cache()
    result = CliRunner().invoke(app, ["policy", "test"])
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "sandbox unavailable" in result.output
    assert old.evals() == []
