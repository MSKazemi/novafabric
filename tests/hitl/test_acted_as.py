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

"""ADR-0150 P3 — acted-on-behalf binding to an NF-084 hop (NF-186), by reference."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from novafabric.hitl import (
    ActedOnBehalfRecord,
    IdentityRefError,
    RecordContentError,
    bindings_for_turn,
    digest_turn,
    load_acted_on_behalf,
    record_acted_on_behalf,
    resolve_hop,
)
from novafabric.hitl import acted_as as acted_mod
from novafabric.hitl.acted_as import MAX_HOPS, delegation_from_capsule

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "conversation"
DELEGATION_SCHEMA = REPO_ROOT / "schemas" / "features" / "delegation-chain-v0.schema.json"
P3 = FIXTURES / "p3-accountability-capsule.json"
OK_DOC = FIXTURES / "nf084-delegation-established.json"
BROKEN_DOC = FIXTURES / "nf084-delegation-broken.json"


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def _hop_ref() -> str:
    ref: str = _load(OK_DOC)["chain"][1]["grant_ref"]
    return ref


@pytest.mark.parametrize("path", [OK_DOC, BROKEN_DOC])
def test_nf084_fixtures_match_delegation_schema(path: Path) -> None:
    jsonschema.validate(_load(path), json.loads(DELEGATION_SCHEMA.read_text()))


# ── Reference, never re-derive (D4) ───────────────────────────────────────


def test_module_never_touches_delegation_verifier() -> None:
    source = Path(acted_mod.__file__).read_text()
    assert "verify_delegation_chain" not in source
    assert "import novafabric.trust.delegation" not in source
    assert "from novafabric.trust" not in source


def test_resolution_does_not_call_nf084_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    from novafabric.trust import delegation

    def forbidden(*_: Any, **__: Any) -> None:
        raise AssertionError("NF-186 must not re-derive NF-084 authority")

    monkeypatch.setattr(delegation, "verify_delegation_chain", forbidden)
    views, _ = bindings_for_turn(_load(P3), "t1", _load(BROKEN_DOC))
    assert views[0].resolution.state == "broken"


def test_established_hop_surfaces_nf084_state() -> None:
    res = resolve_hop(_load(OK_DOC), _hop_ref())
    assert res.established
    d = res.to_dict()
    assert d["present"] is True and d["hop_index"] == 1
    assert d["scope"] == ["spend:usd<=100"]
    assert d["broken_hop"] is None and d["walk_ok"] is True


def test_broken_hop_is_surfaced_verbatim() -> None:
    res = resolve_hop(_load(BROKEN_DOC), _hop_ref())
    assert res.state == "broken"
    assert res.broken_hop == 1
    assert not res.established


def test_broken_hop_elsewhere_still_not_established() -> None:
    doc = _load(BROKEN_DOC)
    res = resolve_hop(doc, doc["chain"][0]["grant_ref"])
    assert res.state == "broken" and res.broken_hop == 1


def test_walk_not_ok_without_broken_hop_is_broken() -> None:
    doc = _load(OK_DOC)
    doc["verified"]["walk_ok"] = False
    assert resolve_hop(doc, _hop_ref()).state == "broken"


@pytest.mark.parametrize(
    ("mutate", "state"),
    [
        (lambda d: None, "established"),
        (lambda d: d.pop("verified"), "unverified"),
        (lambda d: d.update(verified="yes"), "malformed"),
        (lambda d: d["verified"].update(walk_ok="true"), "malformed"),
        (lambda d: d["verified"].update(broken_hop=True), "malformed"),
        (lambda d: d["verified"].update(broken_hop="1"), "malformed"),
        (lambda d: d.update(chain=[]), "malformed"),
        (lambda d: d.update(chain="x"), "malformed"),
        (lambda d: d.update(chain=d["chain"] * (MAX_HOPS // 2 + 1)), "malformed"),
        (lambda d: d["chain"].append(copy.deepcopy(d["chain"][1])), "ambiguous"),
        (lambda d: d["chain"][1].update(grant_ref=digest_turn("other")), "absent"),
        (lambda d: d["chain"][1].update(scope="spend"), "malformed"),
        (lambda d: d["chain"][1].update(scope=[1]), "malformed"),
        (lambda d: d["chain"][1].update(scope=["x" * 300]), "malformed"),
        (lambda d: d["chain"][1].update(hop="1"), "malformed"),
    ],
)
def test_resolution_states(mutate: Any, state: str) -> None:
    doc = _load(OK_DOC)
    mutate(doc)
    assert resolve_hop(doc, _hop_ref()).state == state


def test_no_document_is_absent() -> None:
    res = resolve_hop(None, _hop_ref())
    assert res.state == "absent" and res.to_dict()["present"] is False


def test_delegation_from_capsule() -> None:
    assert delegation_from_capsule({}) is None
    assert delegation_from_capsule({"facets": "x"}) is None
    doc = _load(OK_DOC)
    assert delegation_from_capsule({"facets": {"delegation": doc}}) == doc


# ── Record model + storage ────────────────────────────────────────────────


def test_golden_binding_loads() -> None:
    loaded = load_acted_on_behalf(_load(P3))
    assert not loaded.defects
    assert [(r.turn_ref, r.principal_ref) for _, r in loaded.records] == [
        ("t1", "human:did:example:alice")
    ]


@pytest.mark.parametrize(
    ("override", "exc"),
    [
        ({"delegation_hop_ref": "grant-1"}, RecordContentError),
        ({"principal_ref": "alice"}, IdentityRefError),
        ({"principal_ref": "system:scheduler"}, IdentityRefError),
        ({"turn_ref": "a long sentence"}, RecordContentError),
        ({"Raw-Payload": "x"}, RecordContentError),
    ],
)
def test_invalid_binding_refused(override: dict[str, Any], exc: type[Exception]) -> None:
    base = {"turn_ref": "t1", "delegation_hop_ref": _hop_ref(), "principal_ref": "human:x:abc"}
    with pytest.raises(exc):
        ActedOnBehalfRecord.model_validate({**base, **override})


def test_record_is_fail_open_and_multiple_per_turn() -> None:
    capsule = _load(P3)
    rec = {"turn_ref": "t1", "delegation_hop_ref": digest_turn("g"), "principal_ref": "agent:a:b"}
    out = record_acted_on_behalf(capsule, rec)
    assert out.recorded
    assert len(out.capsule["facets"]["conversation"]["acted_on_behalf"]) == 2
    bad = record_acted_on_behalf(capsule, {**rec, "turn_ref": "t404"})
    assert not bad.recorded and bad.capsule is capsule
    worse = record_acted_on_behalf(capsule, {"turn_ref": "t1"})
    assert not worse.recorded


def test_view_to_dict() -> None:
    views, loaded = bindings_for_turn(_load(P3), "t1", _load(OK_DOC))
    assert not loaded.defects
    d = views[0].to_dict()
    assert d["nf084_hop_state"]["state"] == "established"
    assert d["principal_ref"] == "human:did:example:alice"
    assert bindings_for_turn(_load(P3), "t0", None)[0] == []


# ── Principal must match the hop's granter or the chain root (defect 4) ───


def test_golden_principal_matches_chain_root() -> None:
    res = resolve_hop(_load(OK_DOC), _hop_ref(), principal_ref="human:did:example:alice")
    assert res.established and res.principal_match == "chain_root"
    d = res.to_dict()
    assert d["recorded_by"] == "nf084_document" and d["reverified"] is False


def test_poc_principal_bound_to_someone_elses_grant_is_mismatch() -> None:
    doc = _load(OK_DOC)
    doc["chain"][0]["granter"] = "user:did:example:bob"
    res = resolve_hop(doc, _hop_ref(), principal_ref="human:did:example:alice")
    assert res.state == "principal_mismatch" and not res.established
    assert res.principal_match == "none" and res.walk_ok is True
    assert res.detail is not None and "NF-084 walk: established" in res.detail


def test_mismatch_keeps_broken_walk_visible() -> None:
    res = resolve_hop(_load(BROKEN_DOC), _hop_ref(), principal_ref="human:did:x:mallory")
    assert res.state == "principal_mismatch" and res.broken_hop == 1
    assert res.detail is not None and "NF-084 walk: broken" in res.detail


def test_principal_matches_hop_granter() -> None:
    principal = "agent:spiffe://acme.example/ns/agents/sa/planner"
    res = resolve_hop(_load(OK_DOC), _hop_ref(), principal_ref=principal)
    assert res.established and res.principal_match == "hop_granter"


def test_no_principal_skips_match() -> None:
    assert resolve_hop(_load(OK_DOC), _hop_ref()).principal_match is None


@pytest.mark.parametrize(
    ("principal", "identity", "expected"),
    [
        ("human:did:example:alice", "user:did:example:alice", True),
        ("human:did:example:alice", "human:DID:Example:Alice", True),
        ("human:did:example:alice", "did:example:alice", True),
        ("agent:spiffe://a/b", "spiffe://a/b", True),
        ("human:did:example:alice", "agent:did:example:alice", False),
        ("agent:did:example:alice", "user:did:example:alice", False),
        ("human:did:example:alice", "user:did:example:bob", False),
        ("human:did:example:alice", "did:example:alice:evil", False),
        ("human:fp:0123456789abcdef", "user:fp:0123456789abcdef0123", True),
        ("human:fp:0123456789abcdef", "user:fp:1123456789abcdef0123", False),
        ("human:did:example:alice", None, False),
    ],
)
def test_principal_matches(principal: str, identity: str | None, expected: bool) -> None:
    assert acted_mod.principal_matches(principal, identity) is expected


def test_non_mapping_root_does_not_match() -> None:
    doc = _load(OK_DOC)
    doc["chain"].insert(0, "junk")
    res = resolve_hop(doc, _hop_ref(), principal_ref="human:did:example:alice")
    assert res.state == "principal_mismatch"
