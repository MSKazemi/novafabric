"""M8 — ``nova storage validate`` reports the bucket's real Object Lock mode.

``NovaObjectStore.validate`` checked only ``ObjectLockEnabled == "Enabled"`` and
then returned ``"object_lock": "COMPLIANCE"`` unconditionally. A bucket whose
default retention is GOVERNANCE (bypassable by anyone holding
``s3:BypassGovernanceRetention``) — or that has Object Lock enabled with NO
default retention, so ``NovaObjectStore.put_object`` (which sets no per-object
lock) writes objects nothing protects — was reported as COMPLIANCE WORM.

The truthful mode comes from ``Rule.DefaultRetention.Mode``. ``validate`` (and so
the CLI, exit 1, and ``GET /api/storage/validate``, ``ok: false``) now passes only
COMPLIANCE, and every failure names the mode it actually found.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.storage.nova_object_store import (
    NovaObjectStore,
    ObjectLockModeError,
    ObjectLockNotSupportedError,
)


def _store(config: dict[str, Any]) -> NovaObjectStore:
    client = MagicMock()
    client.get_bucket_object_lock_configuration.return_value = {
        "ObjectLockConfiguration": config
    }
    store = NovaObjectStore(bucket="b")
    store._client = client
    return store


def _rule(mode: str, **period: int) -> dict[str, Any]:
    return {"ObjectLockEnabled": "Enabled", "Rule": {"DefaultRetention": {"Mode": mode, **period}}}


@pytest.mark.parametrize(
    ("config", "mode"),
    [
        (_rule("COMPLIANCE", Days=30), "COMPLIANCE"),
        (_rule("GOVERNANCE", Years=1), "GOVERNANCE"),
        ({"ObjectLockEnabled": "Enabled"}, "none"),
        ({"ObjectLockEnabled": "Enabled", "Rule": {}}, "none"),
    ],
)
def test_inspect_reports_the_default_retention_mode(config: dict[str, Any], mode: str) -> None:
    assert _store(config).inspect_object_lock()["object_lock"] == mode


def test_inspect_reports_the_retention_period() -> None:
    info = _store(_rule("COMPLIANCE", Days=30)).inspect_object_lock()
    assert info["default_retention"] == {"days": 30, "years": None}


def test_validate_passes_only_compliance() -> None:
    info = _store(_rule("COMPLIANCE", Years=7)).validate()
    assert info["object_lock"] == "COMPLIANCE"


@pytest.mark.parametrize(
    ("config", "found"),
    [
        (_rule("GOVERNANCE", Days=1), "GOVERNANCE"),
        ({"ObjectLockEnabled": "Enabled"}, "no default retention"),
    ],
)
def test_validate_refuses_governance_and_no_default_retention(
    config: dict[str, Any], found: str
) -> None:
    with pytest.raises(ObjectLockModeError) as excinfo:
        _store(config).validate()
    # A subclass, so every existing ``except ObjectLockNotSupportedError`` still holds.
    assert isinstance(excinfo.value, ObjectLockNotSupportedError)
    assert found in str(excinfo.value)
    assert excinfo.value.info["object_lock"] in {"GOVERNANCE", "none"}


def test_cli_exits_1_and_names_the_real_mode_for_governance() -> None:
    store = _store(_rule("GOVERNANCE", Days=1))
    with patch("novafabric.storage.nova_object_store.NovaObjectStore", return_value=store):
        result = CliRunner().invoke(app, ["storage", "validate", "--bucket", "b"])
    assert isinstance(result.exception, SystemExit)
    assert result.exit_code == 1
    assert "GOVERNANCE" in result.output
    assert "validated" not in result.output
