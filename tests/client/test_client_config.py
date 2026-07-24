"""Construction + configuration resolution (ADR-0202 D3/D8; spec §Configuration).

Covers: no-default-URL error, env resolution precedence, credential mutual
exclusion, the NOVAFABRIC_API_KEY-wins warning, no request at construction,
and request-header behavior (Accept / User-Agent / Authorization / bearer).
"""

from __future__ import annotations

import warnings

import httpx
import pytest

from novafabric.client import (
    NovaFabricClient,
    NovaFabricConfigError,
)

from .conftest import ScriptedTransport


def _ok_transport() -> ScriptedTransport:
    return ScriptedTransport(httpx.Response(200, json={"items": [], "next_cursor": None, "total": 0}))


class TestBaseUrl:
    def test_missing_base_url_raises_config_error_at_construction(self) -> None:
        with pytest.raises(NovaFabricConfigError) as exc_info:
            NovaFabricClient()
        # The example URL must be in the message (spec §Class surface).
        assert "https://nova.example.com/v0" in str(exc_info.value)

    def test_empty_base_url_raises(self) -> None:
        with pytest.raises(NovaFabricConfigError):
            NovaFabricClient("")

    def test_env_base_url_used_when_no_argument(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVAFABRIC_SERVER_URL", "http://env.example/v0")
        transport = _ok_transport()
        client = NovaFabricClient(transport=transport)
        client.list_capsules()
        assert str(transport.requests[0].url) == "http://env.example/v0/capsules"

    def test_argument_wins_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOVAFABRIC_SERVER_URL", "http://env.example/v0")
        transport = _ok_transport()
        client = NovaFabricClient("http://arg.example/v0", transport=transport)
        client.list_capsules()
        assert str(transport.requests[0].url) == "http://arg.example/v0/capsules"

    def test_trailing_slash_stripped(self) -> None:
        transport = _ok_transport()
        client = NovaFabricClient("http://x.example/v0/", transport=transport)
        client.list_capsules()
        assert str(transport.requests[0].url) == "http://x.example/v0/capsules"


class TestCredentials:
    def test_api_key_and_token_together_raise(self) -> None:
        with pytest.raises(NovaFabricConfigError):
            NovaFabricClient(
                "http://x.example/v0", api_key="nvfk_abc_def", token="tok"
            )

    def test_api_key_sent_as_bearer(self) -> None:
        transport = _ok_transport()
        client = NovaFabricClient(
            "http://x.example/v0", api_key="nvfk_abc_secret", transport=transport
        )
        client.list_capsules()
        assert transport.requests[0].headers["Authorization"] == "Bearer nvfk_abc_secret"

    def test_static_token_sent_as_bearer(self) -> None:
        transport = _ok_transport()
        client = NovaFabricClient(
            "http://x.example/v0", token="my-token", transport=transport
        )
        client.list_capsules()
        assert transport.requests[0].headers["Authorization"] == "Bearer my-token"

    def test_token_provider_called_per_request(self) -> None:
        transport = _ok_transport()
        calls: list[int] = []

        def provider() -> str:
            calls.append(1)
            return f"tok-{len(calls)}"

        client = NovaFabricClient(
            "http://x.example/v0", token=provider, transport=transport
        )
        assert calls == []  # construction never invokes the provider
        client.list_capsules()
        assert transport.requests[0].headers["Authorization"] == "Bearer tok-1"

    def test_no_credential_sends_no_authorization_header(self) -> None:
        transport = _ok_transport()
        client = NovaFabricClient("http://x.example/v0", transport=transport)
        client.list_capsules()
        assert "Authorization" not in transport.requests[0].headers


class TestEnvCredentialResolution:
    def test_env_api_key_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOVAFABRIC_API_KEY", "nvfk_env_key")
        transport = _ok_transport()
        client = NovaFabricClient("http://x.example/v0", transport=transport)
        client.list_capsules()
        assert transport.requests[0].headers["Authorization"] == "Bearer nvfk_env_key"

    def test_env_token_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOVAFABRIC_TOKEN", "env-token")
        transport = _ok_transport()
        client = NovaFabricClient("http://x.example/v0", transport=transport)
        client.list_capsules()
        assert transport.requests[0].headers["Authorization"] == "Bearer env-token"

    def test_both_env_vars_api_key_wins_with_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVAFABRIC_API_KEY", "nvfk_env_key")
        monkeypatch.setenv("NOVAFABRIC_TOKEN", "env-token")
        transport = _ok_transport()
        with pytest.warns(UserWarning, match="NOVAFABRIC_TOKEN is ignored"):
            client = NovaFabricClient("http://x.example/v0", transport=transport)
        client.list_capsules()
        assert transport.requests[0].headers["Authorization"] == "Bearer nvfk_env_key"

    def test_explicit_credential_suppresses_env_lookup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVAFABRIC_API_KEY", "nvfk_env_key")
        transport = _ok_transport()
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # no API-key-wins warning either
            client = NovaFabricClient(
                "http://x.example/v0", token="explicit-token", transport=transport
            )
        client.list_capsules()
        assert transport.requests[0].headers["Authorization"] == "Bearer explicit-token"

    def test_server_token_env_var_is_not_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # NOVAFABRIC_SERVER_TOKEN is the ADR-0184 *server-side* pin.
        monkeypatch.setenv("NOVAFABRIC_SERVER_TOKEN", "server-pin")
        transport = _ok_transport()
        client = NovaFabricClient("http://x.example/v0", transport=transport)
        client.list_capsules()
        assert "Authorization" not in transport.requests[0].headers


class TestNoImplicitIO:
    def test_construction_sends_no_request(self) -> None:
        transport = _ok_transport()
        NovaFabricClient("http://x.example/v0", api_key="nvfk_a_b", transport=transport)
        assert transport.requests == []

    def test_import_sends_no_request(self) -> None:
        # The module is already imported; re-importing must not do IO either.
        import importlib

        import novafabric.client

        importlib.reload(novafabric.client)


class TestHeaders:
    def test_get_headers_accept_and_user_agent(self) -> None:
        transport = _ok_transport()
        client = NovaFabricClient("http://x.example/v0", transport=transport)
        client.list_capsules()
        request = transport.requests[0]
        assert request.headers["Accept"] == "application/json"
        assert request.headers["User-Agent"].startswith("novafabric-python/")
        assert "Content-Type" not in request.headers  # GET sends no body

    def test_query_params_omit_none(self) -> None:
        transport = _ok_transport()
        client = NovaFabricClient("http://x.example/v0", transport=transport)
        client.list_capsules(limit=7)
        assert str(transport.requests[0].url) == "http://x.example/v0/capsules?limit=7"


class TestLifecycle:
    def test_context_manager_closes(self) -> None:
        transport = _ok_transport()
        with NovaFabricClient("http://x.example/v0", transport=transport) as client:
            client.list_capsules()
        assert client._http.is_closed  # noqa: SLF001

    def test_timeout_float_and_default(self) -> None:
        client = NovaFabricClient("http://x.example/v0", timeout=2.5)
        assert client._http.timeout == httpx.Timeout(2.5)  # noqa: SLF001
        client_default = NovaFabricClient("http://x.example/v0")
        assert client_default._http.timeout == httpx.Timeout(  # noqa: SLF001
            connect=5.0, read=30.0, write=30.0, pool=5.0
        )
        explicit = httpx.Timeout(1.0, read=9.0)
        client_explicit = NovaFabricClient("http://x.example/v0", timeout=explicit)
        assert client_explicit._http.timeout == explicit  # noqa: SLF001
