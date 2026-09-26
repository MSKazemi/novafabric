"""Regression: anchored validators must reject a single trailing newline.

In Python ``re``, ``$`` also matches just before a final ``"\\n"``, so
``re.compile(r"^sha256:[0-9a-f]{64}$").match("sha256:<hex>\\n")`` succeeds.
Every validator below guards an id, digest, ref, period, version, name or
header; each is anchored with ``\\Z`` (or used via ``fullmatch``) so the
newline-suffixed value is rejected while the clean value still passes.
``\\d`` in id/period/version validators is ``[0-9]`` so non-ASCII Unicode
digits are rejected too.
"""

from __future__ import annotations

import importlib
import re

import pytest

_HEX = "sha256:" + "a" * 64
_ULID = "01HZX5V8Q3M0000000000000AB"
_UUID7 = "01890a5d-ac96-774b-bcce-b302099a8057"
_UUID4 = "123e4567-e89b-42d3-a456-426614174000"
_ARABIC_INDIC_2 = "٢"

# (module, attribute, a value the validator must accept)
CASES: list[tuple[str, str, str]] = [
    ("novafabric._hashutil", "DIGEST_RE", _HEX),
    ("novafabric.capsule.schema", "_ULID_RE", _ULID),
    ("novafabric.capsule.schema", "_UUID_V7_RE", _UUID7),
    ("novafabric.capsule.ulid_util", "_ULID_RE", _ULID),
    ("novafabric.capsule.ulid_util", "_UUID_V7_RE", _UUID7),
    ("novafabric.capsule.comments", "_ULID_RE", _ULID),
    ("novafabric.capsule.comments", "_SHA256_RE", _HEX),
    ("novafabric.capsule.comments", "_ASSET_RE", "asset://prompt/greeting@v1"),
    ("novafabric.server.request_id", "_SAFE_ID", "req-123.abc_X"),
    ("novafabric.science.fair_rocrate", "_DOI_RE", "10.1234/abc.def"),
    ("novafabric.science.fair_rocrate", "_ORCID_RE", "0000-0002-1825-009X"),
    ("novafabric.science.fair_rocrate", "_ROR_RE", "05dxps055"),
    ("novafabric.science.provenance", "_DIGEST_RE", _HEX),
    ("novafabric.spec.prompt_composition", "_CONTENT_HASH_RE", _HEX),
    ("novafabric.spec.prompt_asset", "_PROMPT_ID_RE", "my-prompt"),
    ("novafabric.spec.prompt_asset", "_CONTENT_HASH_RE", _HEX),
    ("novafabric.spec.asset_labels", "LABEL_RE", "prod.v1"),
    ("novafabric.spec.asset_labels", "_CONTENT_HASH_RE", _HEX),
    ("novafabric.spec.asset_labels", "_ULID_RE", _ULID),
    ("novafabric.spec.models", "_SEMVER_RE", "v1.2.3-rc.1+build.5"),
    ("novafabric.trend.report", "_METRIC_RE", "score:accuracy"),
    ("novafabric.retention.windows", "_DURATION_RE", "P1Y2M3DT4H"),
    ("novafabric.retention.windows", "_DATE_RE", "2026-09-25"),
    ("novafabric.serve.app", "_BUNDLE_ID_RE", "bundle_01-A"),
    ("novafabric.retrieval.fetch_provenance", "_SHA256_REF", _HEX),
    ("novafabric.query.parser", "_PERCENTILE_RE", "p95"),
    ("novafabric.query.parser", "_DURATION_RE", "7d"),
    ("novafabric.query.parser", "_ISO_DURATION_RE", "P1DT2H"),
    ("novafabric.safety._primitives", "DIGEST_RE", _HEX),
    ("novafabric.events.hygiene", "_DIGEST_RE", "blake3:" + "ab" * 32),
    ("novafabric.session.bundle", "_ULID_RE", _ULID),
    ("novafabric.session.manifest", "_ULID_PATTERN", _ULID),
    ("novafabric.session.manifest", "_CAPSULE_REF_PATTERN", "run.json@" + _HEX),
    (
        "novafabric.envelope.cloudevents",
        "_TRACEPARENT_RE",
        "00-" + "a" * 32 + "-" + "b" * 16 + "-01",
    ),
    ("novafabric.envelope.models", "_ULID_RE", _ULID),
    ("novafabric.envelope.models", "_UUID_V7_RE", _UUID7),
    ("novafabric.envelope.models", "_TRACE_ID_RE", "a" * 32),
    ("novafabric.envelope.models", "_SPAN_ID_RE", "b" * 16),
    ("novafabric.envelope.models", "_PAYLOAD_HASH_RE", _HEX),
    ("novafabric.envelope.models", "_AGENT_ID_RE", "agent-1"),
    ("novafabric.envelope.models", "_UUID_RE", _UUID4),
    ("novafabric.eval.annotation_queue", "_ULID_RE", _ULID),
    ("novafabric.eval.annotation_queue", "_SHA256_RE", _HEX),
    ("novafabric.eval.score_config", "_ULID_RE", _ULID),
    ("novafabric.eval.score_config", "_SHA256_RE", _HEX),
    ("novafabric.eval.experiment", "_ULID_RE", _ULID),
    ("novafabric.eval.experiment", "_SHA256_RE", _HEX),
    ("novafabric.eval.scores", "_ULID_RE", _ULID),
    ("novafabric.eval.scores", "_SHA256_RE", _HEX),
    ("novafabric.eval.card", "_SEMVER_RE", "1.2.3"),
    ("novafabric.capture.deployment_env", "ENVIRONMENT_VALUE_PATTERN", "prod-eu:1"),
    ("novafabric.capture.media", "_ULID_RE", _ULID),
    ("novafabric.capture.media", "_CONTENT_HASH_RE", _HEX),
    ("novafabric.capture.media", "_MEDIA_TYPE_RE", "image/png"),
    ("novafabric.capture.session", "ULID_PATTERN", _ULID),
    ("novafabric.frontier_safety._common", "_DIGEST_RE", _HEX),
    ("novafabric.frontier_safety._common", "_URI_RE", "https://example.org/x"),
    ("novafabric.frontier_safety.alignment", "_AUTONOMY_IDENTIFIER_RE", "agent:v1/x"),
    ("novafabric.trust.tenant_keys", "_TENANT_NAME", "acme.corp"),
    ("novafabric.provenance.model_provenance", "_DIGEST_RE", _HEX),
    ("novafabric.provenance.model_provenance", "_URI_RE", "hf://org/model"),
    ("novafabric.context._refs", "DIGEST_RE", _HEX),
    ("novafabric.server.routes.scim", "_MEMBER_PATH_RE", 'members[value eq "u1"]'),
    ("novafabric.serve.topology.router_tv5", "WINDOW_ID_RE", "win-001"),
    ("novafabric.trust.provenance.c2pa_bind", "_CONTENT_HASH_RE", _HEX),
]


def _regex(module: str, attr: str) -> re.Pattern[str]:
    pattern = getattr(importlib.import_module(module), attr)
    assert isinstance(pattern, re.Pattern)
    return pattern


@pytest.mark.parametrize(("module", "attr", "value"), CASES, ids=[f"{m}.{a}" for m, a, _ in CASES])
def test_validator_accepts_clean_value(module: str, attr: str, value: str) -> None:
    assert _regex(module, attr).match(value) is not None


@pytest.mark.parametrize(("module", "attr", "value"), CASES, ids=[f"{m}.{a}" for m, a, _ in CASES])
@pytest.mark.parametrize("suffix", ["\n", "\r\n"])
def test_validator_rejects_trailing_newline(
    module: str, attr: str, value: str, suffix: str
) -> None:
    pattern = _regex(module, attr)
    assert pattern.match(value + suffix) is None
    assert pattern.search(value + suffix) is None


def test_tsa_gentime_rejects_trailing_newline() -> None:
    from novafabric.trust.novaseal import tsa_token

    assert tsa_token._GENTIME_RE.match(b"20260925120000Z") is not None
    assert tsa_token._GENTIME_RE.match(b"20260925120000Z\n") is None


@pytest.mark.parametrize(
    ("module", "attr", "value"),
    [
        ("novafabric.science.fair_rocrate", "_ORCID_RE", f"0000-0002-1825-00{_ARABIC_INDIC_2}X"),
        ("novafabric.science.fair_rocrate", "_DOI_RE", f"10.123{_ARABIC_INDIC_2}/abc"),
        ("novafabric.retention.windows", "_DATE_RE", f"2026-09-{_ARABIC_INDIC_2}5"),
        ("novafabric.retention.windows", "_DURATION_RE", f"P{_ARABIC_INDIC_2}D"),
        ("novafabric.spec.models", "_SEMVER_RE", f"1.{_ARABIC_INDIC_2}.3"),
        ("novafabric.eval.card", "_SEMVER_RE", f"1.{_ARABIC_INDIC_2}.3"),
        ("novafabric.query.parser", "_DURATION_RE", f"{_ARABIC_INDIC_2}d"),
        ("novafabric.query.parser", "_PERCENTILE_RE", f"p9{_ARABIC_INDIC_2}"),
        ("novafabric.cost.pricing_catalog", "_DATE_RE", f"2026-09-{_ARABIC_INDIC_2}5"),
    ],
)
def test_numeric_validators_reject_unicode_digits(module: str, attr: str, value: str) -> None:
    assert _regex(module, attr).fullmatch(value) is None


def test_public_helpers_reject_trailing_newline() -> None:
    from novafabric._hashutil import is_canonical_digest
    from novafabric.server.request_id import sanitize_request_id
    from novafabric.trust.tenant_keys import tenant_from_object_key
    from novafabric.views.model import is_valid_view_id

    assert is_canonical_digest(_HEX)
    assert not is_canonical_digest(_HEX + "\n")
    assert sanitize_request_id("req-1") == "req-1"
    assert sanitize_request_id("req-1\n") != "req-1\n"
    assert is_valid_view_id("my-view")
    assert not is_valid_view_id("my-view\n")
    assert tenant_from_object_key("capsules/acme/x.json") == "acme"
    assert tenant_from_object_key("capsules/acme\n/x.json") is None
