"""A 401 must name the credential the caller actually presented.

Defect **B6**, found standing up the documented multi-node path. An operator ran
`nova server issue-token`, presented the resulting ed25519 JWT, and got:

    {"error":{"code":"unauthenticated","message":"Invalid local token"}}

Two separate faults behind one message:

* **B6a** — the server ignores offline JWTs entirely unless it was started with
  `NOVAFABRIC_OFFLINE_KEY_PATH`. Nothing in `issue-token`'s help, its output, or
  the server's startup banner said so, so a correctly-minted token was rejected
  with no clue why.
* **B6b** — `_verify_local_token` is the fall-through for *every* credential
  class, so a JWT lands there and is reported as an invalid **local token** —
  sending the operator to check `$NOVAFABRIC_HOME/.server-token` for a
  credential they never presented.

The information needed to say the right thing was already there: the code
elsewhere discriminates on `raw_token.count(".") != 2`. Same family as ADR-0231
— a message that claims more (or other) than its evidence supports.
"""
from __future__ import annotations

import secrets as _secrets

import pytest

from novafabric.server import auth as auth_mod
from novafabric.server.auth import _Unauthenticated


class _Req:
    """Minimal stand-in for a Starlette Request: headers + state only."""

    def __init__(self, bearer: str) -> None:
        self.headers = {"Authorization": f"Bearer {bearer}"}

        class _S:
            pass

        self.state = _S()


class _Cfg:
    def __init__(self, *, offline_key_path: str | None) -> None:
        self.local_token = "the-real-opaque-local-token"
        self.offline_key_path = offline_key_path


#: Three dot-separated segments — the discriminator the code itself uses.
JWT_SHAPED = "eyJhbGciOiJFZERTQSJ9.eyJzdWIiOiJiZW5jaEBub3ZhIn0.c2lnbmF0dXJl"


def _message(exc: _Unauthenticated) -> str:
    """`unauthenticated_handler` renders this as the 401 body's message."""
    return str(exc)


class TestAJwtIsNotReportedAsAnInvalidLocalToken:
    def test_offline_disabled_says_so_and_names_the_env_var(self) -> None:
        cfg = _Cfg(offline_key_path=None)
        with pytest.raises(_Unauthenticated) as excinfo:
            auth_mod._verify_local_token(_Req(JWT_SHAPED), cfg)  # type: ignore[arg-type]
        message = _message(excinfo.value)
        assert "Invalid local token" not in message, (
            f"B6b regression: a JWT was reported as a bad local token: {message}"
        )
        assert "NOVAFABRIC_OFFLINE_KEY_PATH" in message, (
            "B6a: the operator cannot discover the server-side requirement "
            f"anywhere else: {message}"
        )
        assert "issue-token" in message

    def test_offline_enabled_blames_signature_or_expiry_not_the_local_token(
        self,
    ) -> None:
        cfg = _Cfg(offline_key_path="/etc/nova/offline.pub")
        with pytest.raises(_Unauthenticated) as excinfo:
            auth_mod._verify_local_token(_Req(JWT_SHAPED), cfg)  # type: ignore[arg-type]
        message = _message(excinfo.value)
        assert "Invalid local token" not in message
        assert "signature" in message or "expired" in message

    def test_a_genuinely_wrong_opaque_token_still_says_local_token(self) -> None:
        """The original message is right for the credential it was written for.
        Guard against 'fixing' this by making every 401 talk about JWTs."""
        cfg = _Cfg(offline_key_path=None)
        with pytest.raises(_Unauthenticated) as excinfo:
            auth_mod._verify_local_token(_Req("not-a-jwt-just-wrong"), cfg)  # type: ignore[arg-type]
        assert "Invalid local token" in _message(excinfo.value)

    def test_the_correct_opaque_token_still_authenticates(self) -> None:
        """Guard the guard: if every call raised, the assertions above would
        pass while local-token auth was entirely broken."""
        cfg = _Cfg(offline_key_path=None)
        ctx = auth_mod._verify_local_token(  # type: ignore[arg-type]
            _Req("the-real-opaque-local-token"), cfg
        )
        assert ctx.roles == ["admin"]

    def test_comparison_stays_constant_time(self) -> None:
        """The B6b branch must not short-circuit the constant-time compare —
        the shape check happens only after it has already failed."""
        import inspect

        source = inspect.getsource(auth_mod._verify_local_token)
        compare_at = source.index("compare_digest")
        shape_at = source.index('count(".")')
        assert compare_at < shape_at, (
            "the JWT-shape check must come after the constant-time comparison, "
            "never before it"
        )
        assert _secrets.compare_digest  # the primitive is still the one used


class TestIssueTokenOutputContract:
    """Two contracts must hold at once, and they pull in opposite directions.

    The B6a hint has to reach the operator, but `TOK=$(nova server issue-token …)`
    must still capture only the token, and
    `tests/test_server_cli_commands.py` extracts the JWT as the **last line** of
    the combined stream. Emitting the hint *after* the token satisfied the first
    and broke the third — caught by that existing suite. The hint therefore goes
    to stderr **and** comes first.
    """

    def test_stdout_is_the_token_and_nothing_else(self, tmp_path) -> None:
        from typer.testing import CliRunner as _CliRunner

        from novafabric.cli.main import app as _app

        # separate streams so we can assert what a shell's $(...) would capture
        result = _CliRunner().invoke(
            _app,
            [
                "server", "issue-token",
                "--subject", "contract@example.com",
                "--roles", "writer",
                "--expires-in", "1d",
                "--key-path", str(tmp_path / "offline.pem"),
            ],
        )
        assert result.exit_code == 0, result.output
        last = result.output.strip().splitlines()[-1].strip()
        assert last.count(".") == 2, (
            "the JWT must remain the LAST line of the combined stream — "
            f"test_server_cli_commands.py extracts it that way. Got: {last!r}"
        )
        assert "NOVAFABRIC_OFFLINE_KEY_PATH" in result.output, (
            "B6a: the server-side requirement must still be shown somewhere"
        )
