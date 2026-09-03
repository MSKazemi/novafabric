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

"""Computer-use / browser-agent action and observation capture (ADR-0148 D3, NF-166/167).

Records what an agent *did* to a GUI and what it *saw* while doing it: an ordered list of
actions (`facets.ui_actions`) and a content-addressed list of observations
(`facets.ui_observations`). It records; it never performs a GUI action, and it is never in
the path of one.

**⚠⚠ A digest of typed text is not a redaction.** The spec sketches typed text as
``text_digest: sha256:…``, and taken literally that would seal a *verifiable oracle* into
evidence. A keystroke stream is the highest-PII-risk content this system touches, and what
people type into GUIs is exactly the low-entropy material a dictionary defeats — passwords,
PINs, coupon codes, postcodes, names, dates of birth. Anyone holding the capsule could
confirm a guess by hashing candidates. Labelling that "redacted" would be worse than not
capturing at all, because the label stops anyone from looking.

So typed text passes through three layers:

1. **Scanned before it is digested.** The ADR-0009 scanner (``capture/secrets.py``) runs
   first. On a match, **no digest is written at all** — :attr:`UiAction.redacted` is ``True``
   with a :attr:`UiAction.redaction_reason`. A digest *of a detected secret* is strictly
   worse than an absence, because it is a checkable record of the secret.
2. **Salted per capsule.** What survives is digested with a random salt generated once per
   facet and recorded once (:attr:`UiActionsFacet.text_digest_salt`). Within one capsule
   "was the same string typed twice?" stays answerable — the only thing the field is for —
   while precomputed tables and cross-capsule correlation both stop working.
3. **The residual is stated, not papered over.** A salted digest does **not** stop someone
   holding the capsule from brute-forcing a short input: the salt is right there beside it.
   That is a real limitation of recording anything about keystrokes at all, and it is
   written down here, in ``docs/cli-reference.md`` and in the CLI output rather than implied
   away. See :data:`KEYSTROKE_RESIDUAL_RISK`.

Raw text is written **only** when byte capture is opted in (``NOVAFABRIC_CAPTURE_MEDIA=1``,
ADR-0125) *and* the text survives the redaction pass.

**Fail-open is a safety property here, not a convenience (ADR-0148 I-3).** NovaFabric
observes an agent driving a real browser or desktop. If a capture hook raises, the agent's
click must still happen — an evidence tool that can wedge the workload it observes is worse
than one that records nothing. :meth:`UiRecorder.record_action` and
:meth:`UiRecorder.record_observation` therefore never propagate an exception; a hook that
fails increments :attr:`UiRecorder.dropped` so the loss is *counted* rather than silent.

**Observations are ADR-0125 MediaParts, not a new blob store.** A screenshot or DOM snapshot
is content-addressed exactly like captured media: ``content_hash`` and ``byte_size`` always,
``blob_ref`` only under the same opt-in. This module introduces no second storage path.
"""

from __future__ import annotations

import hashlib
import secrets as _secrets
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from novafabric.capture.media import media_capture_enabled
from novafabric.capture.secrets import redact_secrets_in_text, scan_text_rule_ids

SCHEMA_VERSION = "0.1.0"
ACTIONS_FACET_NAME = "ui_actions"
OBSERVATIONS_FACET_NAME = "ui_observations"

#: Stated wherever a text digest is surfaced. The honest limit of layer 2 above: a salt
#: stops precomputation and cross-capsule correlation, and stops nothing for someone who
#: holds this capsule and guesses a short string.
KEYSTROKE_RESIDUAL_RISK = (
    "A salted digest is not encryption: anyone holding this capsule can still confirm a "
    "guess at short typed text by hashing candidates with the recorded salt."
)

#: Action kinds NF-166 enumerates. Closed: an unfamiliar kind is more likely a caller bug
#: than a new interaction primitive, and `other` would become the bucket everything fell in.
ActionKind = Literal[
    "click", "type", "key", "scroll", "navigate", "drag", "screenshot"
]

#: Observation kinds NF-167 enumerates.
ObservationKind = Literal["screenshot", "dom_snapshot"]

_SALT_BYTES = 16


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_salt() -> str:
    """A fresh per-capsule salt for text digests."""
    return _secrets.token_hex(_SALT_BYTES)


def salted_text_digest(text: str, salt: str) -> str:
    """Digest *text* under *salt*.

    Salt-first so the construction is not a plain ``sha256(text)`` with a suffix — a reader
    must not be able to strip the salt and match against a precomputed table.
    """
    return "sha256:" + hashlib.sha256(f"{salt}:{text}".encode()).hexdigest()


class TypedText(BaseModel):
    """The outcome of putting one typed string through the three layers above."""

    model_config = ConfigDict(extra="forbid")

    text_digest: str | None = Field(
        default=None,
        description=(
            "Salted digest of the typed text. Absent when a secret rule matched — a digest "
            "of a detected secret is a checkable record of it."
        ),
    )
    text: str | None = Field(
        default=None,
        description=(
            "Raw typed text. Present only under the ADR-0125 byte-capture opt-in AND after "
            "surviving the ADR-0009 redaction pass."
        ),
    )
    redacted: bool = False
    redaction_reason: str | None = None
    char_count: int | None = Field(
        default=None,
        description=(
            "Length of the typed text. Recorded even when redacted, because 'something was "
            "typed here' is evidence and the length alone is not the content."
        ),
    )


def classify_typed_text(
    text: str, *, salt: str, capture_raw: bool | None = None
) -> TypedText:
    """Put one typed string through scan → digest-or-not → optional raw retention.

    *capture_raw* defaults to the ADR-0125 opt-in. Passing it explicitly is for tests and
    for callers that have already made the decision; it never *widens* what is stored,
    because a secret match still suppresses both the raw text and the digest.
    """
    findings = scan_text_rule_ids(text)
    if findings:
        return TypedText(
            text_digest=None,
            text=None,
            redacted=True,
            redaction_reason=f"secret_rule:{findings[0]}",
            char_count=len(text),
        )

    raw: str | None = None
    if capture_raw if capture_raw is not None else media_capture_enabled():
        # Even on the opt-in path the text goes through redaction — the scan above is a
        # detector, this is the transform, and a caller opting in is not opting out of it.
        redacted_text = redact_secrets_in_text(text)
        raw = redacted_text
    return TypedText(
        text_digest=salted_text_digest(text, salt),
        text=raw,
        redacted=False,
        redaction_reason=None,
        char_count=len(text),
    )


class UiAction(BaseModel):
    """One recorded GUI action (NF-166)."""

    model_config = ConfigDict(extra="forbid")

    action_seq: int
    kind: ActionKind
    at: str = Field(default_factory=_now)
    target_ref: str | None = Field(
        default=None, description="Selector or accessibility ref, e.g. ``css:button#checkout``."
    )
    coords: tuple[int, int] | None = None
    url: str | None = None
    typed: TypedText | None = Field(
        default=None, description="Present only for ``kind='type'``."
    )


class UiObservation(BaseModel):
    """One content-addressed thing the agent saw (NF-167)."""

    model_config = ConfigDict(extra="forbid")

    obs_seq: int
    kind: ObservationKind
    content_hash: str
    byte_size: int
    at: str = Field(default_factory=_now)
    blob_ref: str | None = Field(
        default=None,
        description="Set only under the ADR-0125 byte-capture opt-in; a reference, not bytes.",
    )
    dom_digest: str | None = Field(
        default=None, description="For ``dom_snapshot`` — digest of the serialised DOM."
    )


class UiActionsFacet(BaseModel):
    """``facets.ui_actions`` — additive, optional, absent when empty."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    actions: list[UiAction] = Field(default_factory=list)
    text_digest_salt: str | None = Field(
        default=None,
        description=(
            "The per-capsule salt every ``text_digest`` here was taken under. Recorded so "
            "digests are checkable within this capsule; see the residual-risk note."
        ),
    )
    residual_risk: str = KEYSTROKE_RESIDUAL_RISK
    dropped: int = Field(
        default=0,
        description=(
            "Actions lost to a failing capture hook. Counted, never silent: an evidence "
            "list that is quietly short reads as a complete one."
        ),
    )


class UiObservationsFacet(BaseModel):
    """``facets.ui_observations`` — additive, optional, absent when empty."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    observations: list[UiObservation] = Field(default_factory=list)
    dropped: int = 0


class UiRecorder:
    """Collects GUI actions and observations without ever being able to break the run.

    Every public method is total: it returns ``None`` on failure rather than raising, and
    increments :attr:`dropped`. NovaFabric sits beside an agent driving a real browser, and
    an evidence tool that can wedge the workload it observes is worse than one that records
    nothing (ADR-0148 I-3).
    """

    def __init__(self, *, salt: str | None = None, capture_raw: bool | None = None) -> None:
        self._salt = salt or new_salt()
        self._capture_raw = capture_raw
        self._actions: list[UiAction] = []
        self._observations: list[UiObservation] = []
        self.dropped = 0
        self.dropped_observations = 0

    @property
    def salt(self) -> str:
        return self._salt

    def record_action(
        self,
        kind: str,
        *,
        target_ref: str | None = None,
        coords: tuple[int, int] | None = None,
        url: str | None = None,
        text: str | None = None,
        at: str | None = None,
    ) -> UiAction | None:
        """Record one action. Never raises; returns ``None`` if it could not be recorded."""
        try:
            typed = (
                classify_typed_text(text, salt=self._salt, capture_raw=self._capture_raw)
                if text is not None
                else None
            )
            action = UiAction(
                action_seq=len(self._actions),
                kind=kind,  # type: ignore[arg-type]  # validated by pydantic
                target_ref=target_ref,
                coords=coords,
                url=url,
                typed=typed,
                **({"at": at} if at else {}),
            )
        except Exception:  # noqa: BLE001 — fail-open is the point (I-3)
            self.dropped += 1
            return None
        self._actions.append(action)
        return action

    def record_observation(
        self,
        kind: str,
        payload: bytes,
        *,
        blob_ref: str | None = None,
        at: str | None = None,
    ) -> UiObservation | None:
        """Content-address one screenshot or DOM snapshot. Never raises."""
        try:
            digest = "sha256:" + hashlib.sha256(payload).hexdigest()
            keep_ref = blob_ref if self._byte_capture() else None
            observation = UiObservation(
                obs_seq=len(self._observations),
                kind=kind,  # type: ignore[arg-type]
                content_hash=digest,
                byte_size=len(payload),
                blob_ref=keep_ref,
                dom_digest=digest if kind == "dom_snapshot" else None,
                **({"at": at} if at else {}),
            )
        except Exception:  # noqa: BLE001 — fail-open (I-3)
            self.dropped_observations += 1
            return None
        self._observations.append(observation)
        return observation

    def _byte_capture(self) -> bool:
        return (
            self._capture_raw if self._capture_raw is not None else media_capture_enabled()
        )

    def actions_facet(self) -> UiActionsFacet | None:
        """The NF-166 facet, or ``None`` when nothing was recorded.

        ``None`` rather than an empty facet: an empty ``ui_actions`` in a sealed capsule
        reads as "we watched the GUI and the agent did nothing", which is a claim about a
        collection process this recorder cannot make for a run that simply had no GUI.
        """
        if not self._actions and not self.dropped:
            return None
        salt_used = any(
            a.typed is not None and a.typed.text_digest is not None for a in self._actions
        )
        return UiActionsFacet(
            actions=list(self._actions),
            text_digest_salt=self._salt if salt_used else None,
            dropped=self.dropped,
        )

    def observations_facet(self) -> UiObservationsFacet | None:
        if not self._observations and not self.dropped_observations:
            return None
        return UiObservationsFacet(
            observations=list(self._observations), dropped=self.dropped_observations
        )


def attach_facet(
    capsule: dict[str, Any], facet: UiActionsFacet | UiObservationsFacet | None
) -> dict[str, Any]:
    """Attach *facet* under its own key, additively. ``None`` writes nothing."""
    if facet is None:
        return capsule
    facets = capsule.setdefault("facets", {})
    if not isinstance(facets, dict):  # pragma: no cover - defensive
        raise TypeError("capsule 'facets' must be a mapping")
    name = (
        ACTIONS_FACET_NAME
        if isinstance(facet, UiActionsFacet)
        else OBSERVATIONS_FACET_NAME
    )
    facets[name] = facet.model_dump(exclude_none=True)
    return capsule


def actions_from_capsule(capsule: Mapping[str, Any]) -> UiActionsFacet | None:
    """Read the NF-166 facet back, or ``None``."""
    return _read(capsule, ACTIONS_FACET_NAME, UiActionsFacet)


def observations_from_capsule(
    capsule: Mapping[str, Any],
) -> UiObservationsFacet | None:
    """Read the NF-167 facet back, or ``None``."""
    return _read(capsule, OBSERVATIONS_FACET_NAME, UiObservationsFacet)


_FacetT = TypeVar("_FacetT", bound=BaseModel)


def _read(
    capsule: Mapping[str, Any], name: str, model: type[_FacetT]
) -> _FacetT | None:
    facets = capsule.get("facets")
    if not isinstance(facets, Mapping):
        return None
    body = facets.get(name)
    if not isinstance(body, Mapping):
        return None
    try:
        return model.model_validate(dict(body))
    except ValueError:
        return None


def unresolved_observations(
    facet: UiObservationsFacet, capsule_dir: Any
) -> list[UiObservation]:
    """Observations whose ``blob_ref`` does not resolve to bytes matching ``content_hash``.

    A reference-metadata-only observation (``blob_ref`` absent) is **not** unresolved — there
    is nothing to resolve, which is the documented default, not a failure.
    """
    from pathlib import Path

    root = Path(capsule_dir)
    bad: list[UiObservation] = []
    for obs in facet.observations:
        if not obs.blob_ref:
            continue
        try:
            raw = (root / obs.blob_ref).read_bytes()
        except OSError:
            bad.append(obs)
            continue
        if "sha256:" + hashlib.sha256(raw).hexdigest() != obs.content_hash:
            bad.append(obs)
    return bad
