"""Round-4 default: the default evidence key is the one ``nova init`` writes.

``_paths.local_key_path()`` named ``$NOVAFABRIC_HOME/keys/local-key.pem`` while
``nova init`` writes ``keys/signing_key.pem``. The dashboard and server evidence exports
then found no key at the default and auto-generated a second, unrelated keypair, so a
bundle exported from the dashboard was signed by a key the operator never initialised or
pinned. The default is now ``signing_key.pem``; a pre-existing ``local-key.pem`` (an
auto-generated key from an earlier version) is still used when ``signing_key.pem`` is
absent, so existing deployments keep signing with the key they have pinned.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric import _paths
from novafabric.cli.main import app

runner = CliRunner()


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "nova-home"
    monkeypatch.setenv("NOVAFABRIC_HOME", str(h))
    return h


def test_fresh_home_defaults_to_the_init_key_name(home: Path) -> None:
    assert _paths.local_key_path() == home / "keys" / "signing_key.pem"


def test_default_is_the_key_nova_init_wrote(home: Path) -> None:
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    key = _paths.local_key_path()
    assert key.is_file()
    assert key == home / "keys" / "signing_key.pem"


def test_legacy_local_key_still_used_when_it_is_the_only_one(home: Path) -> None:
    legacy = home / "keys" / "local-key.pem"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy")
    assert _paths.local_key_path() == legacy


def test_init_key_wins_over_legacy(home: Path) -> None:
    keys = home / "keys"
    keys.mkdir(parents=True)
    (keys / "local-key.pem").write_text("legacy")
    (keys / "signing_key.pem").write_text("init")
    assert _paths.local_key_path() == keys / "signing_key.pem"


def test_serve_evidence_cart_default_follows(home: Path) -> None:
    from novafabric.serve.routers.evidence_cart import _default_key_path

    assert _default_key_path() == home / "keys" / "signing_key.pem"
