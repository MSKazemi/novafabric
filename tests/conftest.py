import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

# Real shutil.which captured at import, before any test can monkeypatch it (TEST-OPA-1).
_REAL_WHICH = shutil.which


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_opa: do NOT force NoopEngine — let get_policy_engine() select the "
        "real OpaEngine when the opa binary is installed.",
    )


@pytest.fixture(autouse=True)
def _default_noop_policy_engine(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Default the policy engine to NoopEngine (allow-all) — CI parity (TEST-OPA-1).

    ``get_policy_engine()`` returns a real ``OpaEngine`` iff the ``opa`` binary is on
    PATH. CI never installs opa, so gated operations run under ``NoopEngine``; but a
    dev machine *with* opa installed flips every un-seeded gated test to a policy
    DENY. We hide ``opa`` from ``shutil.which`` by default so the suite passes
    regardless of whether opa is installed locally.

    Unaffected (intentionally): tests that inject their own engine via
    ``patch("…service.get_policy_engine", …)`` and tests that construct
    ``OpaEngine()`` directly (which resolves ``opa`` through the OS, not
    ``shutil.which``). Opt out with ``@pytest.mark.real_opa``.
    """
    if request.node.get_closest_marker("real_opa"):
        yield
        return

    def _which(
        cmd: str, mode: int = os.F_OK | os.X_OK, path: str | None = None
    ) -> str | None:
        if cmd == "opa":
            return None
        return _REAL_WHICH(cmd, mode, path)

    monkeypatch.setattr(shutil, "which", _which)
    yield


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "test_registry.db"


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def valid_model_yaml(fixtures_dir: Path) -> Path:
    return fixtures_dir / "valid_model.yaml"


@pytest.fixture
def valid_agent_yaml(fixtures_dir: Path) -> Path:
    return fixtures_dir / "valid_agent.yaml"
