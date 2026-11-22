"""Property tests for Hub receipts (HF-03): whatever the input, the invariants hold.

The example tests in ``test_hf_hub.py`` pin named cases; these generate adversarial text and random
sequences of operations and assert the properties the contract states -- nothing accepted can be
unsafe, nothing refused leaks its input, and no sequence of resolve / re-resolve / verify steps can
leave a receipt that claims more than it holds. No network, no Hub client.
"""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from novafabric.interop.hf_hub import (
    HubReference,
    InvalidHubReferenceError,
    attach_hub_references,
    check_revision_drift,
    declare_reference,
    references_from_manifest,
    resolve_reference,
    verify_local_file,
)

SHA_A, SHA_B = "a" * 40, "b" * 40
BYTES = b"weights"
DIGEST = hashlib.sha256(BYTES).hexdigest()
# Text that is hostile on purpose: separators, traversal, control characters, bidi, a token shape.
_hostile = st.lists(
    st.sampled_from([*"abcXYZ019/._-:@\\ \t\n\x00\x7f\u202e`'\"<>%$;&|", "..", "hf_", "acme/", "a" * 40]),
    max_size=12,
).map("".join)


@settings(max_examples=400, deadline=None)
@given(repo=_hostile, revision=st.none() | _hostile)
def test_whatever_is_accepted_is_safe_and_whatever_is_refused_does_not_echo_its_input(
    repo: str, revision: str | None
) -> None:
    try:
        ref = declare_reference("model", repo, revision=revision)
    except InvalidHubReferenceError as exc:  # the module's own type, never a bare ValueError
        for text in (repo, revision or ""):
            assert len(text) < 6 or text not in str(exc)  # an input is not copied into the error
        return
    for value in (ref.repo_id, ref.requested_revision or ""):
        assert not any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value)
        assert "\\" not in value and ".." not in ref.repo_id.split("/")
    assert ref.pinned == bool(revision is not None and len(revision) == 40 and set(revision) <= set("0123456789abcdef"))
    assert references_from_manifest(attach_hub_references({}, [ref])) == [ref]  # lossless


_ops = st.lists(
    st.sampled_from(["ok_a", "ok_b", "down", "offline", "verify_good", "verify_bad", "verify_missing"]),
    max_size=14,
)


class _Api:
    def __init__(self, sha: str | None) -> None:
        self.sha = sha

    def model_info(self, repo_id: str, **kw: Any) -> Any:
        if self.sha is None:
            raise ConnectionError("down")
        return SimpleNamespace(
            sha=self.sha, id=repo_id, last_modified=None,
            siblings=[SimpleNamespace(rfilename="w.bin", lfs={"sha256": DIGEST, "size": 7})],
        )


@settings(max_examples=250, deadline=None)
@given(ops=_ops)
def test_no_sequence_of_resolutions_and_checks_leaves_a_receipt_that_claims_more_than_it_holds(
    ops: list[str],
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        good, bad = Path(tmp, "good"), Path(tmp, "bad")
        good.write_bytes(BYTES)
        bad.write_bytes(b"tampered")
        ref = declare_reference("model", "acme/tiny", revision="main")
        for op in ops:
            before = ref
            if op in ("ok_a", "ok_b", "down", "offline"):
                api = _Api({"ok_a": SHA_A, "ok_b": SHA_B}.get(op))
                env = {"HF_HUB_OFFLINE": "1"} if op == "offline" else {}
                ref = resolve_reference(
                    ref, api=api, files=["w.bin"], clock=lambda: "2026-10-29T00:00:00Z", environ=env
                )
                if op in ("down", "offline"):
                    # A failed attempt carries no evidence and drift can never read it as "same".
                    assert ref.evidence_strength == "declared_only" and ref.resolved_sha is None
                    assert check_revision_drift(before, ref) == "unknown"
            elif ref.files:
                path = {"verify_good": good, "verify_bad": bad, "verify_missing": Path(tmp, "nope")}[op]
                ref = verify_local_file(ref, "w.bin", path)
            # The invariants are re-checked from scratch on the serialised form after every step.
            again = HubReference.model_validate_json(ref.model_dump_json())
            assert again == ref
            if ref.evidence_strength != "declared_only":
                assert ref.resolution_status == "resolved" and ref.resolved_sha and ref.resolved_at
            if ref.evidence_strength == "local_bytes_verified":
                assert all(f.local_verified is not False for f in ref.files)
                assert any(f.local_verified and f.source == "hub_lfs" for f in ref.files)


_leaf = st.none() | st.booleans() | st.integers() | st.text(max_size=12)
_junk = st.recursive(
    _leaf, lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=6), inner, max_size=4),
    max_leaves=20,
)


@settings(max_examples=500, deadline=None)
@given(manifest=st.dictionaries(st.sampled_from(["extensions", "other", "run_id"]), _junk, max_size=3))
def test_reading_a_manifest_back_is_a_list_or_the_modules_own_error_never_anything_else(
    manifest: dict[str, Any],
) -> None:
    try:
        refs = references_from_manifest(manifest)
    except InvalidHubReferenceError:
        return  # fail closed
    assert isinstance(refs, list) and all(isinstance(r, HubReference) for r in refs)
