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

"""ADR-0165 P2 — format-migration provenance chain (NF-332).

Organised by the spec §3 req. 7 properties a verifier must establish — the
walk detects a broken/missing parent, the chain terminates at
``original_root``, it is acyclic, it is monotonic — plus the I-1 additive-only
and I-2 references-only invariants.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from novafabric.preservation import (
    CHAIN_FIELD,
    MIGRATION_EVENT,
    BrokenMigrationChainError,
    FormatMigrationHop,
    FormatMigrationRewriteError,
    InvalidDigestError,
    InvalidFormatVersionError,
    PayloadCaptureError,
    PreservationError,
    PreservationFacet,
    append_fixity_check,
    append_format_migration,
    attach_facet,
    build_anchor,
    chain_from_facet,
    check_fixity,
    digest_artifact,
    facet_from_capsule,
    parse_format_version,
    plan_next_hop,
    verify_append_only,
    verify_format_migration_chain,
    verify_migration_append_only,
)
from novafabric.preservation import format_migration as fm

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "run-capsule.schema.json"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "preservation"
VALID_CHAIN = FIXTURES / "valid-format-migration-chain.json"
INVALID_CHAIN = FIXTURES / "invalid-format-migration-chain-broken-parent.json"
PRE_ANCHOR_CAPSULE = FIXTURES / "valid-pre-anchor-capsule.json"

ROOT = digest_artifact("sealed-capsule-2026")
TOOL = digest_artifact("migrator")
D03 = digest_artifact("migrated-0.3.0")
D04 = digest_artifact("migrated-0.4.0")
D05 = digest_artifact("migrated-0.5.0")


def _anchor() -> PreservationFacet:
    return build_anchor("presv-1", ROOT, fixity_digest=ROOT)


def _hop(**kw: Any) -> FormatMigrationHop:
    base: dict[str, Any] = {
        "from_version": "run-capsule@0.2.0",
        "to_version": "run-capsule@0.3.0",
        "migrated_at": "2029-02-01T00:00:00Z",
        "tool_ref": TOOL,
        "pre_digest": ROOT,
        "post_digest": D03,
        "parent": None,
    }
    base.update(kw)
    return FormatMigrationHop(**base)


def _three_hops() -> list[FormatMigrationHop]:
    """The spec §6 acceptance chain: 0.2.0 → 0.3.0 → 0.4.0 → 0.5.0."""
    return [
        _hop(),
        _hop(
            from_version="run-capsule@0.3.0",
            to_version="run-capsule@0.4.0",
            migrated_at="2033-01-01T00:00:00Z",
            pre_digest=D03,
            post_digest=D04,
            parent=D03,
        ),
        _hop(
            from_version="run-capsule@0.4.0",
            to_version="run-capsule@0.5.0",
            migrated_at="2037-01-01T00:00:00Z",
            pre_digest=D04,
            post_digest=D05,
            parent=D04,
        ),
    ]


def _codes(hops: list[FormatMigrationHop]) -> list[str]:
    return [f.code for f in verify_format_migration_chain(hops, ROOT).findings]


def _grow(facet: PreservationFacet, to: str, post: str, at: str, **kw: Any) -> PreservationFacet:
    hop = plan_next_hop(facet, to_version=to, tool_ref=TOOL, post_digest=post, migrated_at=at, **kw)
    return append_format_migration(facet, hop)


# ── Golden fixtures ───────────────────────────────────────────────────────


def test_golden_valid_chain_verifies_and_validates_against_real_schema() -> None:
    data = json.loads(VALID_CHAIN.read_text())
    facet = PreservationFacet.model_validate(data)
    result = verify_format_migration_chain(chain_from_facet(facet), facet.original_root)
    assert result.ok and result.hop_count == 2
    capsule = json.loads(PRE_ANCHOR_CAPSULE.read_text())
    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.validate(attach_facet(capsule, facet), schema)


def test_golden_invalid_chain_fails_with_broken_parent() -> None:
    facet = PreservationFacet.model_validate(json.loads(INVALID_CHAIN.read_text()))
    result = verify_format_migration_chain(chain_from_facet(facet), facet.original_root)
    assert not result.ok
    assert result.chain_walk_ok is False
    assert result.reaches_original_root is False
    assert {f.code for f in result.findings} == {"missing_parent", "does_not_reach_original_root"}


# ── Walk: success and reaching original_root ──────────────────────────────


def test_empty_chain_is_ok() -> None:
    result = verify_format_migration_chain([], ROOT)
    assert result.ok and result.hop_count == 0 and result.findings == []


def test_three_hop_chain_reaches_original_root() -> None:
    result = verify_format_migration_chain(_three_hops(), ROOT)
    assert result.ok
    assert (result.chain_walk_ok, result.reaches_original_root) == (True, True)
    assert (result.acyclic, result.monotonic) == (True, True)


def test_chain_anchored_elsewhere_does_not_reach_original_root() -> None:
    hops = _three_hops()
    hops[0] = _hop(pre_digest=digest_artifact("someone-else"))
    result = verify_format_migration_chain(hops, ROOT)
    assert result.reaches_original_root is False
    assert "pre_digest_mismatch" in _codes(hops)
    assert "does_not_reach_original_root" in _codes(hops)


def test_verify_rejects_a_malformed_original_root() -> None:
    with pytest.raises(InvalidDigestError):
        verify_format_migration_chain([], "https://not-a-digest.example")


# ── Broken / missing parent ───────────────────────────────────────────────


def test_first_hop_with_parent_is_broken() -> None:
    hops = [_hop(parent=D04)]
    assert "first_hop_has_parent" in _codes(hops)
    result = verify_format_migration_chain(hops, ROOT)
    assert result.chain_walk_ok is False
    assert result.reaches_original_root is False  # parent D04 resolves nowhere


def test_later_hop_with_null_parent_is_missing_parent() -> None:
    hops = _three_hops()
    hops[2] = hops[2].model_copy(update={"parent": None})
    assert "missing_parent" in _codes(hops)
    # Walking back from the tip stops at hop 2, whose pre_digest is not the root.
    assert verify_format_migration_chain(hops, ROOT).reaches_original_root is False


def test_parent_resolving_to_nothing_is_missing_parent() -> None:
    hops = _three_hops()
    hops[1] = hops[1].model_copy(update={"parent": digest_artifact("ghost")})
    codes = _codes(hops)
    assert "missing_parent" in codes and "does_not_reach_original_root" in codes


def test_parent_skipping_a_hop_is_a_fork() -> None:
    hops = _three_hops()
    hops[2] = hops[2].model_copy(update={"parent": D03})
    codes = _codes(hops)
    assert "parent_not_previous_hop" in codes
    # The walk still resolves back to the root through hop 0, so reachability
    # holds even though the linear chain is broken — reported separately.
    result = verify_format_migration_chain(hops, ROOT)
    assert result.reaches_original_root is True
    assert result.chain_walk_ok is False


def test_pre_digest_must_equal_parent() -> None:
    hops = _three_hops()
    hops[1] = hops[1].model_copy(update={"pre_digest": digest_artifact("other")})
    assert "pre_digest_mismatch" in _codes(hops)


def test_parent_resolving_forward_does_not_reach_root() -> None:
    # Hop 1 claims a parent that is only produced later (by hop 2).
    hops = _three_hops()
    hops[1] = hops[1].model_copy(update={"parent": D05})
    assert verify_format_migration_chain(hops, ROOT).reaches_original_root is False


# ── Acyclic ───────────────────────────────────────────────────────────────


def test_hop_returning_to_original_root_is_a_cycle() -> None:
    hops = _three_hops()
    hops[1] = hops[1].model_copy(update={"post_digest": ROOT})
    assert "cycle" in _codes(hops)
    assert verify_format_migration_chain(hops, ROOT).acyclic is False


def test_hop_repeating_an_earlier_representation_is_a_cycle() -> None:
    hops = _three_hops()
    hops[2] = hops[2].model_copy(update={"post_digest": D03})
    assert "cycle" in _codes(hops)


def test_identity_hop_is_a_cycle() -> None:
    assert "cycle" in _codes([_hop(post_digest=ROOT)])


# ── Monotonic ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("frm", "to"),
    [("run-capsule@0.3.0", "run-capsule@0.2.0"), ("run-capsule@1", "run-capsule@1.0.0")],
)
def test_version_must_move_forward(frm: str, to: str) -> None:
    assert "version_not_increasing" in _codes([_hop(from_version=frm, to_version=to)])


def test_short_and_long_versions_compare_numerically() -> None:
    hops = [_hop(from_version="evidence-bundle@1", to_version="evidence-bundle@2")]
    assert verify_format_migration_chain(hops, ROOT).ok
    hops = [_hop(from_version="run-capsule@0.9.0", to_version="run-capsule@0.10.0")]
    assert verify_format_migration_chain(hops, ROOT).ok


def test_family_change_is_not_monotonic() -> None:
    codes = _codes([_hop(from_version="run-capsule@0.2.0", to_version="evidence-bundle@2")])
    assert codes == ["family_mismatch"]


def test_version_discontinuity_between_hops() -> None:
    hops = _three_hops()
    hops[1] = hops[1].model_copy(update={"from_version": "run-capsule@0.3.5"})
    assert "version_discontinuity" in _codes(hops)


def test_time_running_backwards_is_not_monotonic() -> None:
    hops = _three_hops()
    hops[2] = hops[2].model_copy(update={"migrated_at": "2030-01-01T00:00:00+00:00"})
    result = verify_format_migration_chain(hops, ROOT)
    assert result.monotonic is False
    assert [f.code for f in result.findings] == ["time_not_monotonic"]


def test_overlong_chain_is_refused_without_walking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fm, "MAX_CHAIN_LENGTH", 2)
    result = verify_format_migration_chain(_three_hops(), ROOT)
    assert not result.ok and [f.code for f in result.findings] == ["chain_too_long"]


def test_every_finding_code_falsifies_some_verdict() -> None:
    mapped = fm._LINK_CODES | fm._ROOT_CODES | fm._CYCLE_CODES | fm._MONOTONIC_CODES
    assert set(fm.ChainFindingCode.__args__) == mapped  # type: ignore[attr-defined]


# ── Hop shape (I-2: references and digests only) ──────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        "run-capsule",
        "run-capsule@",
        "Run-Capsule@1",
        "run-capsule@1.0-rc1",
        "@1",
        "run-capsule@1.0\n",  # `$` would accept a trailing newline; `\Z` does not
    ],
)
def test_bad_format_version_rejected(bad: str) -> None:
    with pytest.raises(InvalidFormatVersionError):
        parse_format_version(bad)
    with pytest.raises(ValueError):
        _hop(to_version=bad)


def test_parse_format_version() -> None:
    assert parse_format_version("run-capsule@0.3.0") == ("run-capsule", (0, 3, 0))
    with pytest.raises(InvalidFormatVersionError):
        parse_format_version(3)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["yesterday", "2029-02-01T00:00:00"])
def test_migrated_at_must_be_tz_aware_rfc3339(bad: str) -> None:
    with pytest.raises(ValueError, match="migrated_at"):
        _hop(migrated_at=bad)


def test_tool_ref_rejects_bytes() -> None:
    with pytest.raises(PayloadCaptureError):
        _hop(tool_ref=b"migrator-binary")


def test_tool_ref_rejects_embedded_credentials() -> None:
    with pytest.raises(ValueError, match="credentials"):
        _hop(tool_ref="https://user:s3cret@tools.example.org/migrator")


def test_tool_ref_accepts_uri() -> None:
    assert _hop(tool_ref="oci://registry.example.org/migrator@v1").tool_ref.startswith("oci://")


def test_digests_must_be_sha256_content_digests() -> None:
    with pytest.raises(InvalidDigestError):
        _hop(post_digest="https://example.org/artifact")
    with pytest.raises(InvalidDigestError):
        _hop(parent="sha256:ABC")


def test_parent_is_required_even_when_null() -> None:
    data = _hop().model_dump()
    del data["parent"]
    with pytest.raises(ValueError, match="parent"):
        FormatMigrationHop.model_validate(data)


# ── Reading the chain from a facet (I-3 fail-open) ────────────────────────


def test_anchor_without_chain_has_empty_chain() -> None:
    assert chain_from_facet(_anchor()) == []


def test_non_list_chain_is_rejected() -> None:
    facet = PreservationFacet.model_validate(
        {**_anchor().model_dump(), CHAIN_FIELD: {"not": "a list"}}
    )
    with pytest.raises(PreservationError, match="must be a list"):
        chain_from_facet(facet)


def test_malformed_hop_is_rejected_with_index() -> None:
    facet = PreservationFacet.model_validate(
        {**_anchor().model_dump(), CHAIN_FIELD: [{"from_version": "x"}]}
    )
    with pytest.raises(PreservationError, match=r"\[0\] is malformed"):
        chain_from_facet(facet)


def test_chain_is_returned_in_recorded_order() -> None:
    hops = list(reversed(_three_hops()))
    facet = PreservationFacet.model_validate(
        {**_anchor().model_dump(), CHAIN_FIELD: [h.model_dump() for h in hops]}
    )
    assert [h.post_digest for h in chain_from_facet(facet)] == [D05, D04, D03]


# ── Planning and appending ────────────────────────────────────────────────


def test_plan_first_hop_links_to_original_root() -> None:
    hop = plan_next_hop(
        _anchor(),
        to_version="run-capsule@0.3.0",
        tool_ref=TOOL,
        post_digest=D03,
        migrated_at="2029-02-01T00:00:00Z",
        from_version="run-capsule@0.2.0",
    )
    assert hop.parent is None and hop.pre_digest == ROOT


def test_plan_first_hop_needs_from_version() -> None:
    with pytest.raises(InvalidFormatVersionError, match="first migration hop"):
        plan_next_hop(
            _anchor(),
            to_version="run-capsule@0.3.0",
            tool_ref=TOOL,
            post_digest=D03,
            migrated_at="2029-02-01T00:00:00Z",
        )


def test_plan_later_hop_derives_links_and_from_version() -> None:
    facet = _grow(
        _anchor(),
        "run-capsule@0.3.0",
        D03,
        "2029-02-01T00:00:00Z",
        from_version="run-capsule@0.2.0",
    )
    hop = plan_next_hop(
        facet,
        to_version="run-capsule@0.4.0",
        tool_ref=TOOL,
        post_digest=D04,
        migrated_at="2033-01-01T00:00:00Z",
    )
    assert (hop.from_version, hop.parent, hop.pre_digest) == ("run-capsule@0.3.0", D03, D03)
    # A matching explicit from_version is accepted; a contradicting one is not.
    plan_next_hop(
        facet,
        to_version="run-capsule@0.4.0",
        tool_ref=TOOL,
        post_digest=D04,
        migrated_at="2033-01-01T00:00:00Z",
        from_version="run-capsule@0.3.0",
    )
    with pytest.raises(InvalidFormatVersionError, match="contradicts"):
        plan_next_hop(
            facet,
            to_version="run-capsule@0.4.0",
            tool_ref=TOOL,
            post_digest=D04,
            migrated_at="2033-01-01T00:00:00Z",
            from_version="run-capsule@0.2.0",
        )


def test_append_builds_the_spec_chain_and_records_premis_events() -> None:
    facet = _anchor()
    facet = _grow(
        facet, "run-capsule@0.3.0", D03, "2029-02-01T00:00:00Z", from_version="run-capsule@0.2.0"
    )
    facet = _grow(facet, "run-capsule@0.4.0", D04, "2033-01-01T00:00:00Z")
    hops = chain_from_facet(facet)
    assert [h.to_version for h in hops] == ["run-capsule@0.3.0", "run-capsule@0.4.0"]
    assert verify_format_migration_chain(hops, ROOT).ok
    events = [e for e in facet.provenance_events if e.event == MIGRATION_EVENT]
    assert [e.at for e in events] == ["2029-02-01T00:00:00Z", "2033-01-01T00:00:00Z"]
    assert all(e.agent_ref == TOOL for e in events)


def test_append_without_event() -> None:
    facet = append_format_migration(_anchor(), _hop(), record_event=False)
    assert facet.provenance_events == []
    assert len(chain_from_facet(facet)) == 1


def test_append_never_mutates_the_input_or_the_original() -> None:
    """Additive-only (spec §7): the anchor and its original are untouched."""
    anchor = _anchor()
    snapshot = copy.deepcopy(anchor.model_dump())
    grown = append_format_migration(anchor, _hop())
    assert anchor.model_dump() == snapshot
    assert grown.original_root == anchor.original_root
    assert grown.fixity == anchor.fixity
    assert grown.fixity_log == anchor.fixity_log
    verify_append_only(anchor, grown)  # NF-335 guard still holds
    verify_migration_append_only(anchor, grown)


def test_append_refuses_a_breaking_hop() -> None:
    with pytest.raises(BrokenMigrationChainError, match="version_not_increasing") as info:
        append_format_migration(_anchor(), _hop(to_version="run-capsule@0.1.0"))
    assert info.value.verification.monotonic is False


def test_append_refuses_to_extend_a_broken_chain() -> None:
    facet = PreservationFacet.model_validate(json.loads(INVALID_CHAIN.read_text()))
    tip = chain_from_facet(facet)[-1]
    nxt = _hop(
        from_version=tip.to_version,
        to_version="run-capsule@0.5.0",
        migrated_at="2040-01-01T00:00:00Z",
        pre_digest=tip.post_digest,
        post_digest=D05,
        parent=tip.post_digest,
    )
    with pytest.raises(BrokenMigrationChainError, match="already fails") as info:
        append_format_migration(facet, nxt)
    assert info.value.verification.reaches_original_root is False


def test_chain_survives_attach_and_read_back() -> None:
    facet = append_format_migration(_anchor(), _hop())
    capsule = attach_facet({"run_id": "r"}, facet)
    back = facet_from_capsule(capsule)
    assert back is not None
    assert chain_from_facet(back) == chain_from_facet(facet)
    assert capsule["facets"]["preservation"][CHAIN_FIELD][0]["parent"] is None


def test_fixity_check_after_migration_keeps_the_chain() -> None:
    facet = append_format_migration(_anchor(), _hop())
    checked = check_fixity(facet, "sealed-capsule-2026", checked_at="2030-01-01T00:00:00Z")
    assert chain_from_facet(checked) == chain_from_facet(facet)


# ── I-1: append-only across chain versions ────────────────────────────────


def test_append_only_accepts_a_grown_chain() -> None:
    one = append_format_migration(_anchor(), _hop())
    two = _grow(one, "run-capsule@0.4.0", D04, "2033-01-01T00:00:00Z")
    verify_migration_append_only(one, two)


def test_append_only_rejects_a_changed_original_root() -> None:
    one = append_format_migration(_anchor(), _hop())
    moved = one.model_copy(update={"original_root": D05})
    with pytest.raises(FormatMigrationRewriteError, match="original_root"):
        verify_migration_append_only(one, moved)


def test_append_only_rejects_a_dropped_hop() -> None:
    one = append_format_migration(_anchor(), _hop())
    two = _grow(one, "run-capsule@0.4.0", D04, "2033-01-01T00:00:00Z")
    with pytest.raises(FormatMigrationRewriteError, match="shrank"):
        verify_migration_append_only(two, one)


def test_append_only_rejects_an_edited_hop() -> None:
    one = append_format_migration(_anchor(), _hop())
    raw = one.model_dump()
    raw[CHAIN_FIELD][0]["migrated_at"] = "2029-03-01T00:00:00Z"
    with pytest.raises(FormatMigrationRewriteError, match="hop 0 was rewritten"):
        verify_migration_append_only(one, PreservationFacet.model_validate(raw))


def test_fixity_append_does_not_disturb_migration_append_only() -> None:
    one = append_format_migration(_anchor(), _hop())
    checked = append_fixity_check(
        one,
        check_fixity(one, "sealed-capsule-2026", checked_at="2030-01-01T00:00:00Z").fixity_log[0],
    )
    verify_migration_append_only(one, checked)


def test_module_uses_no_merkle_construction() -> None:
    """Like P1: no tree is needed, so neither in-tree Merkle is imported."""
    source = Path(fm.__file__).read_text()
    assert "merkle" not in source.lower()


# ── Linear-time link walk (review fix: no per-hop rebuild of ``earlier``) ──


def _link_findings_reference(
    hops: list[FormatMigrationHop], original_root: str
) -> list[tuple[int, str]]:
    """The pre-fix quadratic semantics, kept verbatim as an oracle."""
    out: list[tuple[int, str]] = []
    for i, hop in enumerate(hops):
        if i == 0:
            if hop.parent is not None:
                out.append((0, "first_hop_has_parent"))
            expected_pre = original_root
        else:
            previous = hops[i - 1].post_digest
            earlier = {h.post_digest for h in hops[:i]}
            if hop.parent is None:
                out.append((i, "missing_parent"))
            elif hop.parent != previous and hop.parent in earlier:
                out.append((i, "parent_not_previous_hop"))
            elif hop.parent != previous:
                out.append((i, "missing_parent"))
            expected_pre = previous
        if hop.pre_digest != expected_pre:
            out.append((i, "pre_digest_mismatch"))
    return out


def _link_variants() -> list[list[FormatMigrationHop]]:
    base = _three_hops()
    variants = [base, [], base[:1]]
    for index, update in [
        (0, {"parent": D04}),
        (1, {"parent": None}),
        (1, {"parent": digest_artifact("ghost")}),
        (2, {"parent": D03}),
        (2, {"parent": D05}),
        (1, {"pre_digest": digest_artifact("other")}),
        (2, {"post_digest": D03}),
    ]:
        hops = list(base)
        hops[index] = hops[index].model_copy(update=update)
        variants.append(hops)
    fixture = PreservationFacet.model_validate(json.loads(INVALID_CHAIN.read_text()))
    variants.append(chain_from_facet(fixture))
    return variants


@pytest.mark.parametrize("hops", _link_variants())
def test_link_findings_match_the_pre_fix_semantics(hops: list[FormatMigrationHop]) -> None:
    got = [(f.hop_index, f.code) for f in fm._link_findings(hops, ROOT)]
    assert got == _link_findings_reference(hops, ROOT)


def _long_chain(length: int) -> list[FormatMigrationHop]:
    hops: list[FormatMigrationHop] = []
    previous: str | None = None
    for i in range(length):
        post = digest_artifact(f"migrated-{i}")
        hops.append(
            _hop(
                from_version=f"run-capsule@1.{i}",
                to_version=f"run-capsule@1.{i + 1}",
                pre_digest=ROOT if previous is None else previous,
                post_digest=post,
                parent=previous,
            )
        )
        previous = post
    return hops


def test_chain_at_the_length_cap_verifies_in_linear_time() -> None:
    import time

    hops = _long_chain(fm.MAX_CHAIN_LENGTH)
    started = time.perf_counter()
    result = verify_format_migration_chain(hops, ROOT)
    elapsed = time.perf_counter() - started
    assert result.ok and result.hop_count == fm.MAX_CHAIN_LENGTH
    assert elapsed < 1.0, f"verifying {fm.MAX_CHAIN_LENGTH} hops took {elapsed:.2f}s"


def test_chain_one_past_the_real_cap_is_refused() -> None:
    hops = _long_chain(fm.MAX_CHAIN_LENGTH + 1)
    result = verify_format_migration_chain(hops, ROOT)
    assert not result.ok
    assert [(f.hop_index, f.code) for f in result.findings] == [
        (fm.MAX_CHAIN_LENGTH, "chain_too_long")
    ]
