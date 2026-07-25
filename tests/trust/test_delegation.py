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
"""ADR-0106 §NF-084 — the "acted-as" delegation-chain evidence object + verifier.

The security core is **capability attenuation**: a delegation chain
user → agent → sub-agent is trustworthy only when every hop is signed by the
granter, the granted key/identity chains contiguously, the scope only ever
**narrows** (no hop can grant authority its granter did not hold), and no grant is
expired or outlives its parent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from novafabric.trust.delegation import (
    DelegationChain,
    DelegationError,
    Principal,
    issue_grant,
    verify_delegation_chain,
)


def _principal(name: str) -> tuple[Principal, Ed25519PrivateKey]:
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes_raw()
    return Principal(id=name, public_key=pub), key


def _future(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _chain():
    user, user_k = _principal("user:alice")
    agent, agent_k = _principal("agent:planner")
    sub, _sub_k = _principal("agent:worker")
    g1 = issue_grant(user_k, granter=user, grantee=agent,
                     scope={"read", "write", "deploy"}, not_after=_future(10))
    g2 = issue_grant(agent_k, granter=agent, grantee=sub,
                     scope={"read", "write"}, not_after=_future(5))
    return DelegationChain(grants=[g1, g2]), user, sub


class TestValidChain:
    def test_valid_chain_verifies_and_returns_effective_scope(self) -> None:
        chain, user, sub = _chain()
        result = verify_delegation_chain(chain, trusted_roots=[user])
        assert result.effective_principal.id == sub.id
        assert result.effective_scope == frozenset({"read", "write"})

    def test_scope_narrows_along_the_chain(self) -> None:
        chain, user, _ = _chain()
        result = verify_delegation_chain(chain, trusted_roots=[user])
        # The leaf scope is a strict subset of the root scope.
        assert result.effective_scope < frozenset({"read", "write", "deploy"})


class TestRejections:
    def test_scope_escalation_is_rejected(self) -> None:
        # An agent tries to grant MORE than it holds (privilege escalation).
        user, user_k = _principal("user:alice")
        agent, agent_k = _principal("agent:planner")
        sub, _ = _principal("agent:worker")
        g1 = issue_grant(user_k, granter=user, grantee=agent,
                         scope={"read"}, not_after=_future(10))
        g2 = issue_grant(agent_k, granter=agent, grantee=sub,
                         scope={"read", "admin"}, not_after=_future(5))  # admin not held
        with pytest.raises(DelegationError, match="attenuat|escalat|scope"):
            verify_delegation_chain(DelegationChain(grants=[g1, g2]), trusted_roots=[user])

    def test_tampered_signature_is_rejected(self) -> None:
        chain, user, _ = _chain()
        # Mutate the leaf grant's scope after signing.
        bad_leaf = chain.grants[1].model_copy(update={"scope": ["read", "write", "deploy"]})
        bad = DelegationChain(grants=[chain.grants[0], bad_leaf])
        with pytest.raises(DelegationError):
            verify_delegation_chain(bad, trusted_roots=[user])

    def test_broken_key_linkage_is_rejected(self) -> None:
        # g2's granter key is NOT the key g1 delegated to.
        user, user_k = _principal("user:alice")
        agent, _agent_k = _principal("agent:planner")
        impostor, impostor_k = _principal("agent:planner")  # same id, different key
        sub, _ = _principal("agent:worker")
        g1 = issue_grant(user_k, granter=user, grantee=agent,
                         scope={"read"}, not_after=_future(10))
        g2 = issue_grant(impostor_k, granter=impostor, grantee=sub,
                         scope={"read"}, not_after=_future(5))
        with pytest.raises(DelegationError, match="link|key|chain"):
            verify_delegation_chain(DelegationChain(grants=[g1, g2]), trusted_roots=[user])

    def test_untrusted_root_is_rejected(self) -> None:
        chain, _user, _ = _chain()
        other, _ = _principal("user:mallory")
        with pytest.raises(DelegationError, match="root|trust|anchor"):
            verify_delegation_chain(chain, trusted_roots=[other])

    def test_expired_grant_is_rejected(self) -> None:
        user, user_k = _principal("user:alice")
        agent, _ = _principal("agent:planner")
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        g1 = issue_grant(user_k, granter=user, grantee=agent, scope={"read"}, not_after=past)
        with pytest.raises(DelegationError, match="expir"):
            verify_delegation_chain(DelegationChain(grants=[g1]), trusted_roots=[user])

    def test_child_outliving_parent_is_rejected(self) -> None:
        user, user_k = _principal("user:alice")
        agent, agent_k = _principal("agent:planner")
        sub, _ = _principal("agent:worker")
        g1 = issue_grant(user_k, granter=user, grantee=agent, scope={"read"}, not_after=_future(2))
        g2 = issue_grant(agent_k, granter=agent, grantee=sub, scope={"read"}, not_after=_future(9))
        with pytest.raises(DelegationError, match="outlive|expir|parent"):
            verify_delegation_chain(DelegationChain(grants=[g1, g2]), trusted_roots=[user])

    def test_empty_chain_is_rejected(self) -> None:
        user, _ = _principal("user:alice")
        with pytest.raises(DelegationError):
            verify_delegation_chain(DelegationChain(grants=[]), trusted_roots=[user])
