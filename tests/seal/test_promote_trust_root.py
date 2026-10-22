"""Promote signatures need a trust root; a bypass needs a trusted approver (ADR-0315).

Before ADR-0315, ``verify_promote_envelope`` checked a signature against the
certificate carried *inside the same envelope* and read the signer's name from it, so
the name the policy matched on was whatever the signer typed into a self-signed
certificate. ``verify_sod`` accepted a seal bypass signed by any such key, read its
expiry from an unsigned store file, and unwrapped a signed policy without checking its
signature. These tests pin, against a real CA chain:

* no trust root and no explicit insecure opt-in ⇒ every signer is refused;
* with ``--ca-bundle`` / ``NOVAFABRIC_PROMOTE_CA_BUNDLE`` the signer must chain to it;
* a bypass counts only if trusted, signed by a policy approver, for *this* capsule,
  and unexpired by its *signed* ``valid_until``;
* a signed policy whose payload was edited is refused.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.promote.bundle_store import PromoteBundleStore
from novafabric.promote.policy_store import PolicyStore
from novafabric.promote.predicates import (
    BYPASS_PAYLOAD_TYPE,
    POLICY_PAYLOAD_TYPE,
    PROMOTE_CA_BUNDLE_ENV,
    PROMOTE_INSECURE_ENV,
    PromoteTrust,
    UntrustedSignerError,
    build_bypass_predicate,
    build_policy_predicate,
    sign_promote_envelope,
    verify_promote_envelope,
)
from novafabric.promote.verifier import verify_sod

from ._x509_pki import Node, make_cert

runner = CliRunner()
CAPSULE = "cap-trust-root"
REASON = "Production incident: the release must ship before the approver is reachable again."


@pytest.fixture(autouse=True)
def _no_ambient_trust(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PROMOTE_CA_BUNDLE_ENV, raising=False)
    monkeypatch.delenv(PROMOTE_INSECURE_ENV, raising=False)


class Pki:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.root = make_cert("promote-root-ca", issuer=None, ca=True)
        self.other_root = make_cert("someone-elses-ca", issuer=None, ca=True)
        self.bundle = tmp / "promote-ca.pem"
        self.bundle.write_bytes(self.root.cert_pem)

    def identity(self, cn: str, *, issuer: Node | None = None, self_signed: bool = False) -> Node:
        return make_cert(cn, issuer=None if self_signed else (issuer or self.root), ca=False)

    def files(self, node: Node) -> tuple[Path, Path]:
        cn = node.cert.subject.rfc4514_string().replace("=", "_").replace(",", "_")
        key, cert = self.tmp / f"{cn}.key", self.tmp / f"{cn}.crt"
        key.write_bytes(node.key_pem)
        cert.write_bytes(node.cert_pem)
        return key, cert

    def sign(self, node: Node, predicate: dict, payload_type: str) -> bytes:
        key, cert = self.files(node)
        return sign_promote_envelope(json.dumps(predicate).encode(), payload_type, key, cert)


@pytest.fixture()
def pki(tmp_path: Path) -> Pki:
    return Pki(tmp_path)


def _trust(pki: Pki) -> PromoteTrust:
    return PromoteTrust.resolve(pki.bundle)


# ── the trust root ─────────────────────────────────────────────────────────────


def test_with_no_trust_root_every_signer_is_refused(pki: Pki) -> None:
    env = pki.sign(pki.identity("alice"), {"x": 1}, BYPASS_PAYLOAD_TYPE)
    with pytest.raises(UntrustedSignerError, match="no promote trust root"):
        verify_promote_envelope(env, BYPASS_PAYLOAD_TYPE)


def test_the_insecure_opt_in_is_explicit(pki: Pki, monkeypatch: pytest.MonkeyPatch) -> None:
    env = pki.sign(pki.identity("alice", self_signed=True), {"x": 1}, BYPASS_PAYLOAD_TYPE)
    _, who = verify_promote_envelope(
        env, BYPASS_PAYLOAD_TYPE, trust=PromoteTrust(insecure_self_signed=True)
    )
    assert who == "alice"
    monkeypatch.setenv(PROMOTE_INSECURE_ENV, "1")
    assert verify_promote_envelope(env, BYPASS_PAYLOAD_TYPE)[1] == "alice"


def test_a_signer_issued_by_the_pinned_ca_is_accepted(pki: Pki) -> None:
    env = pki.sign(pki.identity("alice"), {"x": 1}, BYPASS_PAYLOAD_TYPE)
    assert verify_promote_envelope(env, BYPASS_PAYLOAD_TYPE, trust=_trust(pki))[1] == "alice"


@pytest.mark.parametrize("who", ["self-signed", "other-ca"])
def test_a_signer_outside_the_pinned_ca_is_refused_even_with_the_right_name(
    pki: Pki, who: str
) -> None:
    forged = (
        pki.identity("alice", self_signed=True)
        if who == "self-signed"
        else pki.identity("alice", issuer=pki.other_root)
    )
    env = pki.sign(forged, {"x": 1}, BYPASS_PAYLOAD_TYPE)
    with pytest.raises(UntrustedSignerError, match="does not chain"):
        verify_promote_envelope(env, BYPASS_PAYLOAD_TYPE, trust=_trust(pki))


def test_the_ca_bundle_env_var_is_the_default_trust_root(
    pki: Pki, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PROMOTE_CA_BUNDLE_ENV, str(pki.bundle))
    env = pki.sign(pki.identity("alice"), {"x": 1}, BYPASS_PAYLOAD_TYPE)
    assert verify_promote_envelope(env, BYPASS_PAYLOAD_TYPE)[1] == "alice"


# ── the bypass ─────────────────────────────────────────────────────────────────


def _policy(pki: Pki, approvers: list[str], *, signer: Node | None = None) -> PolicyStore:
    store = PolicyStore(pki.tmp / "policy.db")
    predicate = build_policy_predicate(["proposer"], approvers)
    if signer is None:
        store.put(json.dumps(predicate))
    else:
        store.put(pki.sign(signer, predicate, POLICY_PAYLOAD_TYPE).decode())
    return store


def _bypass(
    pki: Pki,
    signer: Node,
    *,
    capsule: str = CAPSULE,
    signed_hours: float = 2,
    stored_until: str | None = None,
) -> PromoteBundleStore:
    signed_until = (datetime.now(UTC) + timedelta(hours=signed_hours)).isoformat()
    predicate = build_bypass_predicate(
        capsule, "ab" * 32, "production", REASON, "alice", signed_until, []
    )
    store = PromoteBundleStore(pki.tmp / "bundles")
    store.put_bypass(CAPSULE, pki.sign(signer, predicate, BYPASS_PAYLOAD_TYPE),
                     stored_until or signed_until)
    return store


def test_a_trusted_approver_bypass_skips_sod(pki: Pki) -> None:
    alice = pki.identity("alice")
    result = verify_sod(CAPSULE, _policy(pki, ["alice"]), _bypass(pki, alice), trust=_trust(pki))
    assert result.passed and result.bypass_used, result.message
    assert "authorized by alice" in result.message


def test_a_bypass_by_a_trusted_non_approver_is_ignored(pki: Pki) -> None:
    mallory = pki.identity("mallory")
    result = verify_sod(CAPSULE, _policy(pki, ["alice"]), _bypass(pki, mallory), trust=_trust(pki))
    assert not result.bypass_used and not result.passed


def test_a_bypass_by_an_untrusted_key_named_like_an_approver_is_ignored(pki: Pki) -> None:
    fake_alice = pki.identity("alice", self_signed=True)
    result = verify_sod(
        CAPSULE, _policy(pki, ["alice"]), _bypass(pki, fake_alice), trust=_trust(pki)
    )
    assert not result.bypass_used and not result.passed


def test_the_unsigned_store_cannot_extend_an_expired_bypass(pki: Pki) -> None:
    alice = pki.identity("alice")
    store = _bypass(pki, alice, signed_hours=-1, stored_until="2099-01-01T00:00:00+00:00")
    result = verify_sod(CAPSULE, _policy(pki, ["alice"]), store, trust=_trust(pki))
    assert not result.bypass_used


def test_a_bypass_signed_for_another_capsule_is_ignored(pki: Pki) -> None:
    alice = pki.identity("alice")
    store = _bypass(pki, alice, capsule="some-other-capsule")
    result = verify_sod(CAPSULE, _policy(pki, ["alice"]), store, trust=_trust(pki))
    assert not result.bypass_used


def test_a_bypass_with_no_policy_is_ignored(pki: Pki) -> None:
    alice = pki.identity("alice")
    empty = PolicyStore(pki.tmp / "empty-policy.db")
    result = verify_sod(CAPSULE, empty, _bypass(pki, alice), trust=_trust(pki))
    assert not result.bypass_used


def test_a_signed_policy_with_an_edited_payload_is_refused(pki: Pki) -> None:
    """The signed policy was base64-unwrapped without verification before ADR-0315."""
    alice = pki.identity("alice")
    store = PolicyStore(pki.tmp / "policy.db")
    env = json.loads(pki.sign(alice, build_policy_predicate(["p"], ["alice"]), POLICY_PAYLOAD_TYPE))
    import base64

    forged = build_policy_predicate(["p"], ["alice", "mallory"])
    env["payload"] = base64.urlsafe_b64encode(json.dumps(forged).encode()).decode().rstrip("=")
    store.put(json.dumps(env))
    mallory = pki.identity("mallory")
    result = verify_sod(CAPSULE, store, _bypass(pki, mallory), trust=_trust(pki))
    assert not result.bypass_used


# ── nova seal verify ───────────────────────────────────────────────────────────


def _cli_bypass(pki: Pki) -> tuple[Path, Path]:
    alice = pki.identity("alice")
    _policy(pki, ["alice"])
    _bypass(pki, alice)
    return pki.tmp / "policy.db", pki.tmp / "bundles"


def test_cli_without_a_trust_root_refuses_and_says_how_to_fix_it(pki: Pki) -> None:
    db, data = _cli_bypass(pki)
    result = runner.invoke(app, ["seal", "verify", CAPSULE, "--db", str(db), "--data-dir", str(data)])
    assert result.exit_code != 0, result.output
    text = " ".join(result.output.split())
    assert "--ca-bundle" in text


def test_cli_with_the_ca_bundle_accepts_the_trusted_bypass(pki: Pki) -> None:
    db, data = _cli_bypass(pki)
    result = runner.invoke(
        app,
        ["seal", "verify", CAPSULE, "--db", str(db), "--data-dir", str(data),
         "--ca-bundle", str(pki.bundle)],
    )
    assert result.exit_code == 0, result.output
    assert "bypass active" in result.output


def test_cli_an_unreadable_ca_bundle_exits_2(pki: Pki) -> None:
    db, data = _cli_bypass(pki)
    result = runner.invoke(
        app,
        ["seal", "verify", CAPSULE, "--db", str(db), "--data-dir", str(data),
         "--ca-bundle", str(pki.tmp / "missing.pem")],
    )
    assert result.exit_code == 2, result.output
