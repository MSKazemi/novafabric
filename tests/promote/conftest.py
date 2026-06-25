from __future__ import annotations

from pathlib import Path

import pytest

FIXTURE_KEYS = Path(__file__).parent.parent / "fixtures" / "promote" / "keys"


@pytest.fixture
def proposer_key_path() -> Path:
    return FIXTURE_KEYS / "proposer.pem"


@pytest.fixture
def proposer_cert_path() -> Path:
    return FIXTURE_KEYS / "proposer_cert.pem"


@pytest.fixture
def approver_key_path() -> Path:
    return FIXTURE_KEYS / "approver.pem"


@pytest.fixture
def approver_cert_path() -> Path:
    return FIXTURE_KEYS / "approver_cert.pem"


@pytest.fixture
def admin_key_path() -> Path:
    return FIXTURE_KEYS / "admin.pem"


@pytest.fixture
def admin_cert_path() -> Path:
    return FIXTURE_KEYS / "admin_cert.pem"


@pytest.fixture
def proposer_subject() -> str:
    return "proposer"  # CN from certificate


@pytest.fixture
def approver_subject() -> str:
    return "approver"


@pytest.fixture
def local_policy_store(tmp_path: Path):  # type: ignore[no-untyped-def]
    from novafabric.promote.policy_store import PolicyStore

    return PolicyStore(tmp_path / "test_merkle.db")


@pytest.fixture
def local_bundle_store(tmp_path: Path):  # type: ignore[no-untyped-def]
    from novafabric.promote.bundle_store import PromoteBundleStore

    return PromoteBundleStore(tmp_path)
