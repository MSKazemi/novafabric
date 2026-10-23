"""serve-01 (P0): caller-supplied Rego could read the server's environment.

``POST /api/policy/check`` is ``read``-scoped. It used to evaluate a request's
``policy_source`` with ``opa eval`` in a subprocess that inherited the server's
environment, and it returned the decision ``reason`` verbatim. A read token
posting::

    allow := {"allow": true, "reason": json.marshal(opa.runtime().env)}

got ``NOVAFABRIC_SERVE_TOKEN`` (the admin session token) back: read became admin.
``http.send`` gave the same caller SSRF from the server.

These tests attack each layer of the fix independently:

* **scope** — caller Rego is refused below ``admin`` (403, audited);
* **environment** — ``opa`` never sees the server's environment, whoever asks;
* **capabilities** — ``opa`` runs with a capabilities file that drops
  ``opa.runtime``, ``http.send`` and ``net.lookup_ip_addr`` and allows no network;
* **event loop** — the subprocess runs in a worker thread, never on the loop.

The fake ``opa`` below plays the attacker's best case: whatever ``eval`` is
asked, it answers with the environment it was started with, exactly as the
policy above would under a real ``opa`` that still had ``opa.runtime``. A real
``opa`` test runs the same attack end to end when the binary is available.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from _fake_opa import (  # noqa: E402
    FakeOpa,
    assert_no_server_env,
    assert_restricted_capabilities,
    clear_capabilities_cache,
)
from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve import audit, token_store  # noqa: E402
from novafabric.serve.app import create_app  # noqa: E402

SERVER_TOKEN = "admin-session-token-SECRET-0123456789"
CANARY_ENV = "NOVA_TEST_SERVER_ONLY_CANARY"
CANARY_VALUE = "canary-value-that-must-never-reach-opa"
HEADERS = {"host": "127.0.0.1:4321"}

ENV_LEAK_REGO = (
    "package leak\n"
    'allow := {"allow": true, "reason": json.marshal(opa.runtime().env)}\n'
)
SSRF_REGO = (
    "package ssrf\n"
    'allow := {"allow": true, "reason": json.marshal('
    'http.send({"method": "GET", "url": "http://127.0.0.1:9/"}))}\n'
)
DNS_REGO = (
    "package dns\n"
    'allow := {"allow": true, "reason": json.marshal(net.lookup_ip_addr("localhost"))}\n'
)

POLICY_INPUT: dict[str, Any] = {
    "action": "promote",
    "subject": {"user": "alice", "roles": ["platform-engineer"]},
    "resource": {"kind": "asset", "ref": "m@v1", "eval_score": 0.9, "unsafe_skips": 0},
    "context": {},
}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    h = tmp_path / "home"
    (h / ".novafabric").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    monkeypatch.setenv("NOVAFABRIC_HOME", str(h / ".novafabric"))
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(h / ".novafabric" / "audit.jsonl"))
    # The secrets a server process really holds. None may ever reach `opa`.
    monkeypatch.setenv("NOVAFABRIC_SERVE_TOKEN", SERVER_TOKEN)
    monkeypatch.setenv(CANARY_ENV, CANARY_VALUE)
    yield h


@pytest.fixture
def fake_opa(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[FakeOpa]:
    fake = FakeOpa(tmp_path)
    monkeypatch.setenv("PATH", f"{fake.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    # Every test using this is marked ``real_opa``, so conftest leaves
    # shutil.which alone and the fake on PATH is what the server resolves.
    clear_capabilities_cache()
    yield fake
    clear_capabilities_cache()


@pytest.fixture
def client(tmp_path: Path, home: Path) -> Iterator[TestClient]:
    capsules = tmp_path / "runs"
    capsules.mkdir()
    app = create_app(token=SERVER_TOKEN, capsule_dir=capsules, static_mounted_by_caller=True)
    with TestClient(app) as c:
        yield c


def _mint(scope: str) -> str:
    secret = f"issued-{scope}-token-abcdefghijklmnop"
    token_store.issue(f"test-{scope}", secret, scope)
    return secret


def _check(client: TestClient, token: str, **extra: Any) -> Any:
    return client.post(
        "/api/policy/check",
        headers={**HEADERS, "Authorization": f"Bearer {token}"},
        json={**POLICY_INPUT, **extra},
    )


def _audit_records() -> list[dict[str, Any]]:
    path = Path(audit.__dict__["_path"]())
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _assert_no_server_env(env: dict[str, str]) -> None:
    assert_no_server_env(env, SERVER_TOKEN, CANARY_VALUE)


# ---------------------------------------------------------------------------
# (a) scope: caller Rego is admin-only
# ---------------------------------------------------------------------------


@pytest.mark.real_opa
@pytest.mark.parametrize("scope", ["read", "operate", "audit"])
@pytest.mark.parametrize("rego", [ENV_LEAK_REGO, SSRF_REGO], ids=["env", "ssrf"])
def test_caller_rego_below_admin_is_refused(
    client: TestClient, fake_opa: FakeOpa, scope: str, rego: str
) -> None:
    r = _check(client, _mint(scope), policy_source=rego)
    assert r.status_code == 403, r.text[:300]
    assert SERVER_TOKEN not in r.text and CANARY_VALUE not in r.text
    assert "admin" in r.json()["detail"]
    assert fake_opa.evals() == [], "opa must not even start for a refused request"


@pytest.mark.real_opa
def test_refused_caller_rego_is_audited(client: TestClient, fake_opa: FakeOpa) -> None:
    _check(client, _mint("read"), policy_source=ENV_LEAK_REGO)
    denials = [r for r in _audit_records() if r.get("action") == "authz.denied"]
    assert denials, "a refused privilege escalation must leave an audit record"
    last = denials[-1]
    assert last["required_scope"] == "admin"
    assert last["held_scope"] == "read"
    assert "policy_source" in json.dumps(last["args"])


@pytest.mark.real_opa
@pytest.mark.parametrize("blank", ["", "   \n\t"])
def test_blank_policy_source_is_the_packaged_policy_not_caller_rego(
    client: TestClient, fake_opa: FakeOpa, blank: str
) -> None:
    r = _check(client, _mint("read"), policy_source=blank)
    assert r.status_code == 200, r.text[:300]


@pytest.mark.real_opa
def test_non_string_policy_source_is_rejected(client: TestClient, fake_opa: FakeOpa) -> None:
    r = _check(client, SERVER_TOKEN, policy_source={"rego": ENV_LEAK_REGO})
    assert r.status_code == 422
    assert fake_opa.evals() == []


# ---------------------------------------------------------------------------
# (b) environment + capabilities: even admin Rego cannot read secrets
# ---------------------------------------------------------------------------


@pytest.mark.real_opa
def test_admin_rego_cannot_see_the_server_environment(
    client: TestClient, fake_opa: FakeOpa
) -> None:
    r = _check(client, SERVER_TOKEN, policy_source=ENV_LEAK_REGO)
    assert r.status_code == 200, r.text[:300]
    assert SERVER_TOKEN not in r.text
    assert CANARY_VALUE not in r.text
    calls = fake_opa.calls()
    assert calls, "the fake opa never ran"
    for call in calls:  # capabilities probe included
        _assert_no_server_env(call["env"])
    for call in fake_opa.evals():
        assert_restricted_capabilities(call)


@pytest.mark.real_opa
def test_admin_explain_trace_is_sandboxed_too(client: TestClient, fake_opa: FakeOpa) -> None:
    r = _check(client, SERVER_TOKEN, policy_source=ENV_LEAK_REGO, explain=True)
    assert r.status_code == 200, r.text[:300]
    assert SERVER_TOKEN not in r.text and CANARY_VALUE not in r.text
    evals = fake_opa.evals()
    assert len(evals) == 2, "decision + --explain trace"
    for call in evals:
        _assert_no_server_env(call["env"])
        assert_restricted_capabilities(call)


@pytest.mark.real_opa
def test_packaged_policy_for_a_read_token_is_sandboxed(
    client: TestClient, fake_opa: FakeOpa
) -> None:
    r = _check(client, _mint("read"))
    assert r.status_code == 200, r.text[:300]
    assert SERVER_TOKEN not in r.text and CANARY_VALUE not in r.text
    (call,) = fake_opa.evals()
    _assert_no_server_env(call["env"])
    assert_restricted_capabilities(call)
    assert "--data" in call["argv"]


@pytest.mark.real_opa
def test_policy_test_route_runs_opa_sandboxed(client: TestClient, fake_opa: FakeOpa) -> None:
    r = client.post(
        "/api/policy/test",
        headers={**HEADERS, "Authorization": f"Bearer {_mint('read')}"},
        json={},
    )
    assert r.status_code == 200, r.text[:300]
    assert SERVER_TOKEN not in r.text and CANARY_VALUE not in r.text
    (call,) = fake_opa.evals()
    assert call["argv"][0] == "test"
    _assert_no_server_env(call["env"])
    assert_restricted_capabilities(call)


@pytest.mark.real_opa
@pytest.mark.parametrize("scope", ["read", "operate", "audit"])
def test_policy_test_with_a_chosen_bundle_needs_admin(
    client: TestClient, fake_opa: FakeOpa, home: Path, scope: str
) -> None:
    """A chosen bundle_path is Rego the server did not ship — same rule as policy_source."""
    bundle = home / ".novafabric" / "exports" / "planted"
    bundle.mkdir(parents=True)
    (bundle / "x_test.rego").write_text(ENV_LEAK_REGO)
    r = client.post(
        "/api/policy/test",
        headers={**HEADERS, "Authorization": f"Bearer {_mint(scope)}"},
        json={"bundle_path": str(bundle)},
    )
    assert r.status_code == 403, r.text[:300]
    assert fake_opa.evals() == []
    admin = client.post(
        "/api/policy/test",
        headers={**HEADERS, "Authorization": f"Bearer {SERVER_TOKEN}"},
        json={"bundle_path": str(bundle)},
    )
    assert admin.status_code == 200, admin.text[:300]
    assert SERVER_TOKEN not in admin.text and CANARY_VALUE not in admin.text
    (call,) = fake_opa.evals()
    assert str(bundle.resolve()) in call["argv"]
    _assert_no_server_env(call["env"])
    assert_restricted_capabilities(call)


# ---------------------------------------------------------------------------
# (c) off the event loop
# ---------------------------------------------------------------------------


def test_policy_check_evaluates_off_the_event_loop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.policy import OpaEngine, PolicyDecision

    seen: dict[str, bool] = {}

    def _evaluate(self: OpaEngine, *_a: object, **_k: object) -> PolicyDecision:
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        return PolicyDecision(allow=True, reason="", decision_id="d")

    monkeypatch.setattr(OpaEngine, "evaluate", _evaluate)
    assert _check(client, SERVER_TOKEN).status_code == 200
    assert seen == {"on_loop": False}, "opa must never block the event loop"


# ---------------------------------------------------------------------------
# Real opa, end to end (skipped when no binary is available)
# ---------------------------------------------------------------------------


def _real_opa_bin() -> str | None:
    explicit = os.environ.get("NOVA_TEST_OPA_BIN")
    if explicit and Path(explicit).is_file():
        return explicit
    return shutil.which("opa")


@pytest.fixture
def real_opa(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    exe = _real_opa_bin()
    if exe is None:
        pytest.skip("no opa binary (set NOVA_TEST_OPA_BIN or put opa on PATH)")
    bin_dir = tmp_path / "realbin"
    bin_dir.mkdir()
    shutil.copy2(exe, bin_dir / "opa")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    clear_capabilities_cache()
    yield str(bin_dir / "opa")
    clear_capabilities_cache()


@pytest.mark.real_opa
@pytest.mark.parametrize(
    ("rego", "builtin"),
    [(ENV_LEAK_REGO, "opa.runtime"), (SSRF_REGO, "http.send"), (DNS_REGO, "net.lookup_ip_addr")],
    ids=["env", "ssrf", "dns"],
)
def test_real_opa_refuses_the_dangerous_builtins_for_admin(
    client: TestClient, real_opa: str, rego: str, builtin: str
) -> None:
    r = _check(client, SERVER_TOKEN, policy_source=rego)
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["allow"] is False, body
    assert SERVER_TOKEN not in r.text and CANARY_VALUE not in r.text
    assert builtin in body["reason"], body["reason"][:500]


@pytest.mark.real_opa
def test_real_opa_still_evaluates_ordinary_admin_rego(client: TestClient, real_opa: str) -> None:
    rego = 'package ok\nallow if input.subject.user == "alice"\n'
    r = _check(client, SERVER_TOKEN, policy_source=rego)
    assert r.status_code == 200, r.text[:300]
    assert r.json()["allow"] is True, r.json()


@pytest.mark.real_opa
@pytest.mark.parametrize("action", ["promote", "evidence_export", "replay_mutating"])
def test_real_opa_packaged_policies_compile_under_the_restricted_capabilities(
    client: TestClient, real_opa: str, action: str
) -> None:
    r = _check(client, _mint("read"), action=action)
    assert r.status_code == 200, r.text[:300]
    reason = r.json()["reason"]
    assert "opa eval error" not in reason, reason[:500]
    assert "sandbox" not in reason, reason[:500]
