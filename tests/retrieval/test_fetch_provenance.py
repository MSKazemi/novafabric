# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""ADR-0153 P1 — web-fetch provenance (NF-218).

Tests are organised by the ADR's invariants, because those are what a
reviewer needs to be convinced of: I-1 record-only / never fetches,
I-2 no payloads and no credentials, I-3 fail-open & additive-first,
I-4 not adjudicated.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path

import pytest

from novafabric.retrieval import fetch_provenance as fp
from novafabric.retrieval.fetch_provenance import (
    FetchProvenance,
    ForbiddenFetchFieldError,
    InvalidRedirectChainError,
    SecretMaterialError,
    attach_facet,
    build_facet,
    digest_body,
    fingerprint_leaf_certificate,
    record_fetch,
    verify_body_binding,
)

BODY = "<html>The committee published its findings.</html>"
FIXTURES = Path(__file__).parent.parent / "fixtures" / "retrieval-source-authority"


def _fetch(**kw: object) -> FetchProvenance:
    base: dict[str, object] = {
        "final_url": "https://news.example/2026/07/report",
        "http_status": 200,
        "fetched_at": "2026-07-20T09:00:00Z",
    }
    base.update(kw)
    return FetchProvenance(**base)  # type: ignore[arg-type]


# ── Body binding ──────────────────────────────────────────────────────────


def test_body_digest_is_sha256_with_algorithm_prefix() -> None:
    assert digest_body(BODY) == f"sha256:{hashlib.sha256(BODY.encode()).hexdigest()}"


def test_binding_verifies_against_the_fetched_body() -> None:
    record = _fetch(body_content_hash=digest_body(BODY))
    assert verify_body_binding(record, BODY) is True


def test_binding_fails_when_the_body_changed() -> None:
    record = _fetch(body_content_hash=digest_body(BODY))
    assert verify_body_binding(record, BODY + "!") is False


def test_unbound_fetch_does_not_verify() -> None:
    # Absent is not false — but it is certainly not a pass either.
    assert verify_body_binding(_fetch(), BODY) is False


# ── Redirect chain ────────────────────────────────────────────────────────


def test_redirect_chain_preserves_order() -> None:
    record = record_fetch(
        final_url="https://news.example/2026/07/report",
        http_status=200,
        fetched_at="2026-07-20T09:00:00Z",
        redirect_chain=[
            ("http://news.example/report", 301),
            ("https://news.example/report", 302),
        ],
    )
    assert [h.url for h in record.redirect_chain] == [
        "http://news.example/report",
        "https://news.example/report",
    ]
    assert [h.status for h in record.redirect_chain] == [301, 302]


def test_non_redirect_hop_is_rejected() -> None:
    with pytest.raises(InvalidRedirectChainError):
        record_fetch(
            final_url="https://news.example/report",
            http_status=200,
            fetched_at="2026-07-20T09:00:00Z",
            redirect_chain=[("https://news.example/a", 200)],
        )


def test_a_fetch_without_redirects_has_an_empty_chain() -> None:
    assert _fetch().redirect_chain == []


# ── I-2 no payloads, no credentials ───────────────────────────────────────


def test_credential_shaped_extra_field_is_refused_loudly() -> None:
    with pytest.raises(ForbiddenFetchFieldError, match="cookie"):
        _fetch(cookie="session=abc123")


@pytest.mark.parametrize(
    "key", ["authorization", "token", "api_key", "private_key", "body", "Set_Cookie"]
)
def test_all_credential_shaped_keys_are_refused(key: str) -> None:
    with pytest.raises(ForbiddenFetchFieldError):
        _fetch(**{key: "x"})


def test_unrelated_extra_field_is_still_allowed() -> None:
    # extra="allow" keeps later ADR-0153 phases additive; only the
    # credential-shaped names are denied.
    record = _fetch(http_version="2")
    assert (record.model_extra or {})["http_version"] == "2"


def test_userinfo_credentials_in_a_url_are_refused() -> None:
    with pytest.raises(ForbiddenFetchFieldError, match="userinfo"):
        _fetch(final_url="https://user:pass@news.example/report")


def test_userinfo_credentials_in_a_redirect_hop_are_refused() -> None:
    with pytest.raises(ForbiddenFetchFieldError):
        record_fetch(
            final_url="https://news.example/report",
            http_status=200,
            fetched_at="2026-07-20T09:00:00Z",
            redirect_chain=[("https://user:pass@news.example/a", 301)],
        )


def test_query_string_is_preserved_verbatim() -> None:
    # The exact URL fetched is the evidence; rewriting it would break the
    # identity the record establishes.
    url = "https://news.example/report?id=17&utm_source=agent"
    assert _fetch(final_url=url).final_url == url


def test_private_key_material_is_never_fingerprinted() -> None:
    pem = b"-----BEGIN RSA PRIVATE KEY-----\nMIIB...\n-----END RSA PRIVATE KEY-----"
    with pytest.raises(SecretMaterialError):
        fingerprint_leaf_certificate(pem)


def test_leaf_certificate_fingerprint_is_a_sha256_ref() -> None:
    der = b"\x30\x82\x01\x0a fake der bytes"
    assert fingerprint_leaf_certificate(der) == f"sha256:{hashlib.sha256(der).hexdigest()}"


def test_raw_certificate_offered_as_a_fingerprint_is_rejected() -> None:
    with pytest.raises(SecretMaterialError):
        _fetch(tls_cert_fingerprint="-----BEGIN CERTIFICATE-----MIIB")


def test_fetched_body_is_present_only_as_a_digest() -> None:
    record = _fetch(body_content_hash=digest_body(BODY))
    assert BODY not in json.dumps(record.model_dump(exclude_none=True))


# ── I-1 record-only / never fetches ───────────────────────────────────────


def test_module_never_fetches_on_novafabrics_behalf() -> None:
    src = inspect.getsource(fp)
    for forbidden in ("requests", "httpx", "urllib.request", "aiohttp", "socket"):
        assert forbidden not in src


def test_module_exposes_no_fetching_surface() -> None:
    assert not [n for n in dir(fp) if n.startswith(("fetch_", "get_", "download"))]


# ── I-3 additive-first / fail-open ────────────────────────────────────────


def test_capsule_without_fetch_material_is_untouched() -> None:
    capsule = {"capsule_id": "cap-a"}
    assert attach_facet(capsule, build_facet([])) is capsule


def test_attach_does_not_mutate_the_input_capsule() -> None:
    capsule: dict[str, object] = {"capsule_id": "cap-a"}
    attach_facet(capsule, build_facet([_fetch()]))
    assert capsule == {"capsule_id": "cap-a"}


def test_attach_preserves_sibling_facets() -> None:
    capsule = {"facets": {"safety": {"decisions": []}}}
    out = attach_facet(capsule, build_facet([_fetch()]))
    assert set(out["facets"]) == {"safety", "fetch_provenance"}


def test_facet_orders_fetches_by_fetch_time() -> None:
    late = _fetch(fetched_at="2026-07-20T09:00:02Z", final_url="https://b.example/")
    early = _fetch(fetched_at="2026-07-20T09:00:01Z", final_url="https://a.example/")
    facet = build_facet([late, early])
    assert [f.final_url for f in facet.fetches] == [
        "https://a.example/",
        "https://b.example/",
    ]


def test_facet_carries_a_schema_version() -> None:
    assert build_facet([]).schema_version == fp.SCHEMA_VERSION


# ── Golden fixtures ───────────────────────────────────────────────────────


def test_golden_text_only_capsule_carries_no_fetch_facet() -> None:
    capsule = json.loads((FIXTURES / "valid-text-only.json").read_text())
    assert "facets" not in capsule


def test_golden_reference_only_capsule_round_trips() -> None:
    capsule = json.loads(
        (FIXTURES / "valid-source-authority-reference-only.json").read_text()
    )
    facet = fp.FetchProvenanceFacet(**capsule["facets"]["fetch_provenance"])
    record = facet.fetches[0]
    assert record.http_status == 200
    assert [h.status for h in record.redirect_chain] == [301, 302]
    assert record.tls_cert_fingerprint is not None
    assert record.tls_cert_fingerprint.startswith("sha256:")
    assert record.archival_ref is not None
    # The pinned document and the fetched body are the same bytes, by digest.
    assert record.body_content_hash == capsule["retrieval"]["documents"][0][
        "content_hash"
    ]


def test_golden_reference_only_capsule_holds_no_secret_material() -> None:
    capsule = json.loads(
        (FIXTURES / "valid-source-authority-reference-only.json").read_text()
    )
    capsule.pop("_fixture")  # the human-readable note names what is absent
    raw = json.dumps(capsule).lower()
    for forbidden in ("cookie", "authorization", "bearer", "private key", "password"):
        assert forbidden not in raw
