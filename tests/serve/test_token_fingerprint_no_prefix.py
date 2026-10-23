"""No plaintext prefix of the ``nova serve`` token ever leaves the process.

``verify_token`` used to return ``candidate[:8]`` -- the first eight characters of
the *plaintext* server token -- as the audit "fingerprint". That value went to the
dashboard audit log, from there to the SIEM export's ``user.uid``, to the
``session_token_fingerprint`` field of ``GET /api/admin/tokens`` and to the
evidence-cart audit record. A token prefix is part of the secret; for a pinned,
low-entropy ``NOVAFABRIC_SERVE_TOKEN`` it can be most of it.

The fingerprint is now a domain-separated SHA-256 digest
(:func:`novafabric.serve.auth.token_fingerprint`): stable for the same token, and
not a substring of it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve import audit  # noqa: E402
from novafabric.serve.app import create_app  # noqa: E402
from novafabric.serve.auth import token_fingerprint  # noqa: E402

TOKEN = "prefix-leak-server-token-0123456789"
H = {"host": "127.0.0.1:4321"}
REGISTER_YAML = """\
novafabric_spec_version: "1"
asset_type: model
name: fp-model
version: 1.0.0
status: development
description: fingerprint test asset
spec:
  framework: pytorch
  artifact_path: /tmp/fp-model
"""


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    monkeypatch.setenv(audit.AUDIT_ENV, str(tmp_path / "audit.jsonl"))
    capsules = tmp_path / "runs"
    capsules.mkdir()
    app = create_app(
        token=TOKEN, capsule_dir=capsules, db_path=tmp_path / "reg.db", static_dir=None
    )
    with TestClient(app) as c:
        yield c


def _prefixes(secret: str) -> list[str]:
    """Every prefix of *secret* at least four characters long."""
    return [secret[:n] for n in range(4, len(secret) + 1)]


# ---------------------------------------------------------------------------
# The helper itself
# ---------------------------------------------------------------------------


def test_fingerprint_is_stable_for_the_same_token() -> None:
    assert token_fingerprint(TOKEN) == token_fingerprint(TOKEN)


def test_fingerprint_differs_between_tokens() -> None:
    assert token_fingerprint(TOKEN) != token_fingerprint(TOKEN + "x")


def test_fingerprint_is_not_a_prefix_or_substring_of_the_token() -> None:
    fp = token_fingerprint(TOKEN)
    assert fp not in TOKEN
    assert not TOKEN.startswith(fp)
    assert fp != TOKEN[:8]


def test_fingerprint_is_short_lowercase_hex() -> None:
    fp = token_fingerprint(TOKEN)
    assert len(fp) == 12
    int(fp, 16)  # raises if not hex
    assert fp == fp.lower()


def test_fingerprint_is_domain_separated_from_a_plain_digest() -> None:
    """A plain SHA-256 prefix would equal the digest other surfaces publish."""
    plain = hashlib.sha256(TOKEN.encode()).hexdigest()
    assert not plain.startswith(token_fingerprint(TOKEN))


def test_fingerprint_handles_non_ascii_and_empty_tokens() -> None:
    assert len(token_fingerprint("")) == 12
    assert len(token_fingerprint("tökén-ü")) == 12


# ---------------------------------------------------------------------------
# Every surface the prefix used to reach
# ---------------------------------------------------------------------------


def test_audit_log_carries_the_digest_fingerprint_not_a_prefix(client: TestClient) -> None:
    res = client.post(
        "/api/assets",
        params={"token": TOKEN},
        headers=H,
        json={"spec_yaml": REGISTER_YAML, "confirmed": True},
    )
    assert res.status_code == 200, res.text
    entries = audit.read_recent(20)
    assert entries, "the mutation should have been audited"
    fps = {e.get("actor_token_fp") for e in entries}
    assert token_fingerprint(TOKEN) in fps
    blob = json.dumps(entries)
    for prefix in _prefixes(TOKEN)[4:]:  # 8+ characters
        assert prefix not in blob, f"audit log leaks token prefix {prefix!r}"


def test_session_token_fingerprint_is_the_digest(client: TestClient) -> None:
    res = client.get("/api/admin/tokens", params={"token": TOKEN}, headers=H)
    assert res.status_code == 200, res.text
    fp = res.json()["session_token_fingerprint"]
    assert fp == token_fingerprint(TOKEN)
    assert fp not in TOKEN


def test_audit_endpoint_and_siem_view_never_carry_a_prefix(client: TestClient) -> None:
    client.post(
        "/api/assets",
        params={"token": TOKEN},
        headers=H,
        json={"spec_yaml": REGISTER_YAML, "confirmed": True},
    )
    res = client.get("/api/audit", params={"token": TOKEN}, headers=H)
    assert res.status_code == 200
    assert TOKEN[:8] not in res.text
    from novafabric.audit.siem_export import to_ocsf

    entries = audit.read_recent(20)
    assert entries
    for entry in entries:
        event = to_ocsf(entry, source="dashboard")
        assert event["actor"]["user"]["uid"] == token_fingerprint(TOKEN)
        assert TOKEN[:8] not in json.dumps(event)
