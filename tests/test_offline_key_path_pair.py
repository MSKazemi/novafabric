"""L22: ``NOVAFABRIC_OFFLINE_KEY_PATH`` names one keypair, by either of its files.

``nova server issue-token`` read the variable as the PRIVATE signing key, and the server
read it as the key to VERIFY with. The CLI's own hint told the operator to export
``NOVAFABRIC_OFFLINE_KEY_PATH=<key>.pub`` for the server; in that shell the next
``issue-token`` then tried to load the public key as a private key and failed — or, when
the ``.pub`` did not exist yet, generated a keypair at the ``.pub`` path that wrote the
public key over the private one.

Now every consumer accepts either file of the pair: issuing resolves ``X.pub`` to its
private sibling ``X.pem``; verifying needs only the public key. A ``.pub`` with no private
sibling is refused with a message, never regenerated over.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

pytest.importorskip("jwt")

from novafabric.cli.main import app  # noqa: E402
from novafabric.server.offline_tokens import (  # noqa: E402
    OfflineKeyError,
    generate_keypair,
    issue_token,
    signing_key_path,
    verify_offline_token,
)

runner = CliRunner()


def _token(output: str) -> str:
    return output.strip().splitlines()[-1]


def test_signing_key_path_maps_public_to_private(tmp_path: Path) -> None:
    priv = tmp_path / "offline-key.pem"
    generate_keypair(priv)
    assert signing_key_path(priv) == priv
    assert signing_key_path(priv.with_suffix(".pub")) == priv


def test_issue_token_with_env_naming_the_public_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    priv = tmp_path / "offline-key.pem"
    first = runner.invoke(app, ["server", "issue-token", "--subject", "a", "--key-path", str(priv)])
    assert first.exit_code == 0, first.output
    pem_before = priv.read_bytes()
    pub = priv.with_suffix(".pub")
    pub_before = pub.read_bytes()

    # Follow the CLI's own hint in the same shell, then issue again.
    monkeypatch.setenv("NOVAFABRIC_OFFLINE_KEY_PATH", str(pub))
    second = runner.invoke(app, ["server", "issue-token", "--subject", "b"])
    assert second.exit_code == 0, second.output
    assert second.exception is None or isinstance(second.exception, SystemExit)
    assert priv.read_bytes() == pem_before and pub.read_bytes() == pub_before
    # The server, configured with the .pub, accepts it.
    ctx = verify_offline_token(_token(second.output), pub)
    assert ctx.subject == "b"


def test_public_key_without_private_sibling_is_refused_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    priv = tmp_path / "offline-key.pem"
    generate_keypair(priv)
    pub = priv.with_suffix(".pub")
    pub_before = pub.read_bytes()
    priv.unlink()  # verify-only host: just the public key

    monkeypatch.setenv("NOVAFABRIC_OFFLINE_KEY_PATH", str(pub))
    result = runner.invoke(app, ["server", "issue-token", "--subject", "c"])
    assert result.exit_code == 2, result.output
    assert isinstance(result.exception, SystemExit)
    assert "public key" in result.output and "private" in result.output
    assert pub.read_bytes() == pub_before
    assert not priv.exists()


def test_public_key_under_a_private_name_gives_a_named_error(tmp_path: Path) -> None:
    priv = tmp_path / "offline-key.pem"
    generate_keypair(priv)
    disguised = tmp_path / "server.key"
    disguised.write_bytes(priv.with_suffix(".pub").read_bytes())
    with pytest.raises(OfflineKeyError, match="public key"):
        issue_token("x", [], 1, disguised)

    result = runner.invoke(
        app, ["server", "issue-token", "--subject", "x", "--key-path", str(disguised)]
    )
    assert result.exit_code == 2, result.output
    assert isinstance(result.exception, SystemExit)
    assert "public key" in result.output


def test_issue_token_library_accepts_the_public_path(tmp_path: Path) -> None:
    """The server's service-account route passes its configured path straight through."""
    priv = tmp_path / "offline-key.pem"
    generate_keypair(priv)
    pub = priv.with_suffix(".pub")
    token = issue_token("svc:x", [], 1, pub)
    assert verify_offline_token(token, pub).subject == "svc:x"
    assert verify_offline_token(token, priv).subject == "svc:x"


def test_service_account_signing_key_refuses_a_lone_public_key(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from novafabric.server.errors import BadRequestError
    from novafabric.server.routes.service_accounts import _signing_key_path

    priv = tmp_path / "offline-key.pem"
    generate_keypair(priv)
    priv.unlink()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                config=SimpleNamespace(offline_key_path=str(priv.with_suffix(".pub")))
            )
        )
    )
    with pytest.raises(BadRequestError, match="private"):
        _signing_key_path(request)  # type: ignore[arg-type]


def test_env_naming_a_not_yet_existing_public_key_generates_the_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pub = tmp_path / "fresh.pub"
    monkeypatch.setenv("NOVAFABRIC_OFFLINE_KEY_PATH", str(pub))
    result = runner.invoke(app, ["server", "issue-token", "--subject", "d"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "fresh.pem").is_file()
    assert pub.read_bytes().startswith(b"-----BEGIN PUBLIC KEY-----")
    assert verify_offline_token(_token(result.output), pub).subject == "d"
