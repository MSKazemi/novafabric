"""Property test: the Hugging Face URL classification can only ever name the two inference hosts.

``match_kind: regex`` is anchored, so a classified URL must have, once *parsed*, the scheme https and
exactly the router host or a ``*.endpoints.huggingface.cloud`` host. Adversarial URLs are assembled
from the pieces an attacker controls (userinfo, ports, paths, query, fragment, backslashes, case).
"""
from __future__ import annotations

from urllib.parse import urlparse

from hypothesis import given, settings
from hypothesis import strategies as st

from novafabric.capture.event_recorder import _is_ai_api
from novafabric.capture.hooks._url_registry import load_url_registry

_REGISTRY = load_url_registry()
_HOSTS = ["router.huggingface.co", "x.endpoints.huggingface.cloud", "huggingface.co", "evil.example"]
_piece = st.sampled_from([
    "https://", "http://", "//", "", "evil@", "router.huggingface.co@", "user:pw@",
    "router.huggingface.co", "ROUTER.HUGGINGFACE.CO", "my.endpoints.huggingface.cloud",
    "xendpoints.huggingface.cloud", "huggingface.co", "evil.example", ".attacker.example",
    ":443", ":8443", ":", "/v1/chat/completions", "/together/v1/x", "/api/models", "?next=", "?",
    "#", "/", "\\", "%2f", " ", "\t", "@",
])


@settings(max_examples=2000, deadline=None)
@given(parts=st.lists(_piece, min_size=1, max_size=8))
def test_a_classified_huggingface_url_always_parses_to_one_of_the_two_inference_hosts(
    parts: list[str],
) -> None:
    url = "".join(parts)
    system = _REGISTRY.gen_ai_system(url)
    if not system.startswith("huggingface"):
        return
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.username is None and parsed.password is None  # no userinfo trickery
    host = (parsed.hostname or "").lower()
    if system == "huggingface":
        assert host == "router.huggingface.co"
    else:
        assert system == "huggingface.endpoint" and host.endswith(".endpoints.huggingface.cloud")
    assert _is_ai_api(url)  # the two matchers agree on everything the registry accepts
