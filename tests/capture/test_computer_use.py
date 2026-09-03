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

"""NF-166/167 — computer-use action + observation capture (ADR-0148 D3).

The load-bearing tests here are the keystroke ones. Everything else is shape; those are
the reason the module is written the way it is.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from novafabric.capture.ui import (
    ACTIONS_FACET_NAME,
    KEYSTROKE_RESIDUAL_RISK,
    OBSERVATIONS_FACET_NAME,
    UiRecorder,
    actions_from_capsule,
    attach_facet,
    classify_typed_text,
    new_salt,
    observations_from_capsule,
    salted_text_digest,
    unresolved_observations,
)

#: A string matching the ADR-0009 pack (NovaFabric's own API-key format).
A_SECRET = "nvfk_abcd1234_" + "x" * 40
ORDINARY = "SAVE20"


# --- the keystroke boundary --------------------------------------------------


def test_a_detected_secret_yields_no_digest_and_no_raw_text() -> None:
    """AC2 — a digest *of a detected secret* is a checkable record of it, so there is none."""
    out = classify_typed_text(A_SECRET, salt=new_salt(), capture_raw=False)
    assert out.redacted is True
    assert out.text_digest is None, "a digest here would be an oracle for the secret"
    assert out.text is None
    assert out.redaction_reason is not None
    assert out.redaction_reason.startswith("secret_rule:")


def test_the_secret_appears_nowhere_in_the_serialised_facet() -> None:
    """The property that actually matters, asserted against the bytes that get sealed.

    Field-by-field assertions can all pass while the value leaks through some other key,
    so this searches the whole serialised facet for the secret itself.
    """
    rec = UiRecorder(capture_raw=False)
    rec.record_action("type", target_ref="css:input#token", text=A_SECRET)
    facet = rec.actions_facet()
    assert facet is not None
    blob = json.dumps(facet.model_dump(mode="json"))
    assert A_SECRET not in blob
    assert "nvfk_" not in blob


def test_even_with_byte_capture_on_a_secret_is_not_stored() -> None:
    """Opting into byte capture is not opting out of the secret rules."""
    out = classify_typed_text(A_SECRET, salt=new_salt(), capture_raw=True)
    assert out.text is None
    assert out.text_digest is None
    assert out.redacted is True


def test_ordinary_text_digests_and_records_its_length() -> None:
    out = classify_typed_text(ORDINARY, salt=new_salt(), capture_raw=False)
    assert out.redacted is False
    assert out.text_digest is not None
    assert out.text is None, "raw text needs the opt-in"
    assert out.char_count == len(ORDINARY)


def test_the_same_text_twice_in_one_capsule_digests_identically() -> None:
    """AC3a — the only property the field exists to provide."""
    salt = new_salt()
    a = classify_typed_text(ORDINARY, salt=salt, capture_raw=False)
    b = classify_typed_text(ORDINARY, salt=salt, capture_raw=False)
    assert a.text_digest == b.text_digest


def test_the_same_text_in_two_capsules_digests_differently() -> None:
    """AC3b — without this, one digest is a cross-capsule correlation key for a password."""
    a = classify_typed_text(ORDINARY, salt=new_salt(), capture_raw=False)
    b = classify_typed_text(ORDINARY, salt=new_salt(), capture_raw=False)
    assert a.text_digest != b.text_digest


def test_the_digest_is_not_a_bare_sha256_of_the_text() -> None:
    """AC4 — the naive implementation passes AC3a, so it is asserted separately.

    A plain ``sha256(text)`` is exactly the rainbow-table target the salt exists to defeat.
    """
    salt = new_salt()
    naive = "sha256:" + hashlib.sha256(ORDINARY.encode()).hexdigest()
    out = classify_typed_text(ORDINARY, salt=salt, capture_raw=False)
    assert out.text_digest != naive


def test_the_salt_is_prefixed_not_appended() -> None:
    """A suffix salt can be stripped by a reader; a prefix cannot be, as cheaply."""
    salt = "0" * 32
    assert salted_text_digest(ORDINARY, salt) == "sha256:" + hashlib.sha256(
        f"{salt}:{ORDINARY}".encode()
    ).hexdigest()


def test_salts_are_not_reused_between_recorders() -> None:
    assert UiRecorder().salt != UiRecorder().salt


def test_the_facet_carries_the_salt_and_states_the_residual_risk() -> None:
    """A digest nobody can check is useless; a digest nobody was warned about is worse."""
    rec = UiRecorder(capture_raw=False)
    rec.record_action("type", target_ref="css:input#coupon", text=ORDINARY)
    facet = rec.actions_facet()
    assert facet is not None
    assert facet.text_digest_salt == rec.salt
    assert facet.residual_risk == KEYSTROKE_RESIDUAL_RISK
    assert "not encryption" in facet.residual_risk


def test_no_salt_is_published_when_nothing_was_digested() -> None:
    """A capsule whose only typed text was redacted publishes no salt — there is nothing
    for it to unlock, and an unused salt invites the reader to assume there is."""
    rec = UiRecorder(capture_raw=False)
    rec.record_action("type", target_ref="css:input#token", text=A_SECRET)
    facet = rec.actions_facet()
    assert facet is not None
    assert facet.text_digest_salt is None


def test_raw_text_is_stored_only_under_the_opt_in() -> None:
    off = classify_typed_text(ORDINARY, salt=new_salt(), capture_raw=False)
    on = classify_typed_text(ORDINARY, salt=new_salt(), capture_raw=True)
    assert off.text is None
    assert on.text == ORDINARY


# --- actions -----------------------------------------------------------------


@pytest.mark.parametrize(
    "kind", ["click", "type", "key", "scroll", "navigate", "drag", "screenshot"]
)
def test_every_enumerated_action_kind_records(kind: str) -> None:
    """AC1."""
    rec = UiRecorder(capture_raw=False)
    action = rec.record_action(kind, target_ref="css:body")
    assert action is not None
    assert action.kind == kind


def test_actions_are_sequenced_in_order() -> None:
    rec = UiRecorder(capture_raw=False)
    rec.record_action("navigate", url="https://example.test/cart")
    rec.record_action("click", target_ref="css:button#checkout", coords=(812, 344))
    rec.record_action("type", target_ref="css:input#coupon", text=ORDINARY)
    facet = rec.actions_facet()
    assert facet is not None
    assert [a.action_seq for a in facet.actions] == [0, 1, 2]
    assert [a.kind for a in facet.actions] == ["navigate", "click", "type"]
    assert facet.actions[1].coords == (812, 344)


def test_an_unknown_action_kind_is_dropped_not_raised() -> None:
    """AC6 — fail-open. A bad kind is a caller bug; it must not reach the agent as an
    exception, and it must not be silently invented into a valid one either."""
    rec = UiRecorder(capture_raw=False)
    assert rec.record_action("teleport", target_ref="css:body") is None
    assert rec.dropped == 1
    facet = rec.actions_facet()
    assert facet is not None
    assert facet.actions == []
    assert facet.dropped == 1, "the loss is counted, not silent"


def test_a_recorder_that_dropped_everything_still_reports_the_drops() -> None:
    """An empty action list with dropped=0 and one with dropped=3 are different claims."""
    rec = UiRecorder(capture_raw=False)
    for _ in range(3):
        rec.record_action("nope")
    facet = rec.actions_facet()
    assert facet is not None and facet.dropped == 3


def test_a_run_with_no_gui_produces_no_facet() -> None:
    """AC7 / I-1 — an empty ui_actions reads as 'we watched and it did nothing'."""
    assert UiRecorder(capture_raw=False).actions_facet() is None
    assert UiRecorder(capture_raw=False).observations_facet() is None


# --- observations ------------------------------------------------------------


def test_a_screenshot_is_content_addressed_reference_only_by_default() -> None:
    """AC5."""
    payload = b"\x89PNG\r\n\x1a\nscreenshot-bytes"
    rec = UiRecorder(capture_raw=False)
    obs = rec.record_observation("screenshot", payload, blob_ref="outputs/shot.png")
    assert obs is not None
    assert obs.content_hash == "sha256:" + hashlib.sha256(payload).hexdigest()
    assert obs.byte_size == len(payload)
    assert obs.blob_ref is None, "blob_ref needs the byte-capture opt-in"
    assert obs.dom_digest is None


def test_a_dom_snapshot_carries_a_dom_digest() -> None:
    rec = UiRecorder(capture_raw=False)
    obs = rec.record_observation("dom_snapshot", b"<html><body>cart</body></html>")
    assert obs is not None
    assert obs.dom_digest == obs.content_hash


def test_blob_ref_is_kept_under_the_opt_in() -> None:
    rec = UiRecorder(capture_raw=True)
    obs = rec.record_observation("screenshot", b"bytes", blob_ref="outputs/shot.png")
    assert obs is not None
    assert obs.blob_ref == "outputs/shot.png"


def test_an_invalid_observation_kind_is_dropped_not_raised() -> None:
    rec = UiRecorder(capture_raw=False)
    assert rec.record_observation("hologram", b"x") is None
    assert rec.dropped_observations == 1


def test_reference_only_observations_are_not_unresolved(tmp_path: Path) -> None:
    """A privacy-preserving capsule must not read as a corrupt one."""
    rec = UiRecorder(capture_raw=False)
    rec.record_observation("screenshot", b"bytes", blob_ref="outputs/shot.png")
    facet = rec.observations_facet()
    assert facet is not None
    assert unresolved_observations(facet, tmp_path) == []


def test_a_stored_observation_whose_bytes_changed_is_unresolved(tmp_path: Path) -> None:
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "shot.png").write_bytes(b"original")
    rec = UiRecorder(capture_raw=True)
    rec.record_observation("screenshot", b"original", blob_ref="outputs/shot.png")
    facet = rec.observations_facet()
    assert facet is not None
    assert unresolved_observations(facet, tmp_path) == []

    (tmp_path / "outputs" / "shot.png").write_bytes(b"tampered")
    assert len(unresolved_observations(facet, tmp_path)) == 1


def test_a_stored_observation_whose_blob_is_missing_is_unresolved(
    tmp_path: Path,
) -> None:
    rec = UiRecorder(capture_raw=True)
    rec.record_observation("screenshot", b"bytes", blob_ref="outputs/gone.png")
    facet = rec.observations_facet()
    assert facet is not None
    assert len(unresolved_observations(facet, tmp_path)) == 1


# --- facet round-trip --------------------------------------------------------


def test_both_facets_attach_and_read_back() -> None:
    rec = UiRecorder(capture_raw=False)
    rec.record_action("navigate", url="https://example.test/")
    rec.record_observation("screenshot", b"bytes")
    capsule: dict[str, Any] = {"run_id": "run_1"}
    attach_facet(capsule, rec.actions_facet())
    attach_facet(capsule, rec.observations_facet())

    assert set(capsule["facets"]) == {ACTIONS_FACET_NAME, OBSERVATIONS_FACET_NAME}
    actions = actions_from_capsule(capsule)
    observations = observations_from_capsule(capsule)
    assert actions is not None and len(actions.actions) == 1
    assert observations is not None and len(observations.observations) == 1


def test_attaching_none_writes_nothing() -> None:
    capsule: dict[str, Any] = {"run_id": "run_1"}
    attach_facet(capsule, None)
    assert "facets" not in capsule


def test_reading_a_capsule_without_the_facets_is_none() -> None:
    assert actions_from_capsule({}) is None
    assert observations_from_capsule({"facets": "nope"}) is None
    assert actions_from_capsule({"facets": {ACTIONS_FACET_NAME: {"actions": 3}}}) is None


# --- the span: does a capsule carrying these facets actually validate? --------


def test_a_capsule_carrying_both_facets_validates_against_the_real_schema() -> None:
    """AC7 — attach_facet producing the right shape and `nova validate` accepting it are
    different questions, because facets is a closed registry (ADR-0196 D2). On 2026-09-03
    that gap was holding 13 unregistered names; these two must not become the 14th and 15th.
    """
    import jsonschema

    repo = Path(__file__).resolve().parents[2]
    schema = json.loads(
        (repo / "src" / "novafabric" / "schemas" / "run-capsule.schema.json").read_text(
            encoding="utf-8"
        )
    )
    capsule = json.loads(
        (repo / "tests" / "trust" / "_capsule_base.json").read_text(encoding="utf-8")
    )

    rec = UiRecorder(capture_raw=False)
    rec.record_action("navigate", url="https://example.test/")
    rec.record_action("type", target_ref="css:input#coupon", text=ORDINARY)
    rec.record_observation("screenshot", b"bytes")
    attach_facet(capsule, rec.actions_facet())
    attach_facet(capsule, rec.observations_facet())

    jsonschema.Draft202012Validator(schema).validate(capsule)
    assert set(capsule["facets"]) == {ACTIONS_FACET_NAME, OBSERVATIONS_FACET_NAME}
