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

"""Settlement-provenance facet — ADR-0163 P1 (NF-311).

Records *that* an agent's payment settled and *what it was authorized by*,
as references only: the NF-087 payment mandate and the settlement
confirmation are bound by ``sha256:`` digest, never inlined.

Four invariants from ADR-0163 shape every choice in this module:

- **I-1 Record-only.** NovaFabric is never in the payment path. Nothing here
  processes, holds, moves, releases, or adjudicates money; it records what a
  payment network already did.
- **I-2 Secret-free.** No PAN, CVV, IBAN, private key, seed phrase, or raw
  payment token may enter the facet. A caller who hands one over gets a
  :class:`PaymentSecretRejectedError` — see the module note on why this
  refuses rather than redacts.
- **I-3 Additive-first / fail-open.** The facet lives in optional
  ``facets.settlement``. A run that moved no money produces no facet, and a
  capsule without one stays exactly as valid as before this feature existed.
- **I-4 Records, never determines.** A bound settlement reference supports a
  reconciliation, dispute, or payout determination; it never makes one.

P2 (NF-312/313/314) adds three blocks on top of that binding: what was
authorized against what was observed, how final the money actually is, and
whether the non-repudiation anchor still holds. All three obey I-4 in a
specific way worth stating once:

- A **discrepancy is recorded, never resolved.** Nothing here picks a winner
  between the mandate and the confirmation, "corrects" either side, or nets
  the two amounts off. The gap between them *is* the evidence; closing it
  would destroy the only thing an auditor came for.
- **Not-yet-final never renders as final,** and the *absence* of a finality
  record is ``unknown`` — never ``settled``. See :func:`finality_state`.
- A **non-repudiation binding cannot claim to be intact when it is not.**
  That is enforced in a model validator, not in the builder, so
  ``model_validate`` of untrusted JSON cannot mint one.

**Why refuse instead of redact.** The rest of the capsule pipeline redacts
secrets silently, because there the secret arrived incidentally (a token
quoted in a log line) and the run must not be disturbed. Here it did not: a
caller passing a card number into a settlement facet has a bug in its
integration with a payment protocol, and it is holding a PAN somewhere else
too. Silently storing a ``[REDACTED]`` marker would tell that caller its
integration is fine. So I-2 raises, loudly, naming the field but never
echoing the value (an exception message travels into logs).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# The prefixed credential patterns curated for the support bundle. Reused on
# purpose rather than `capture.secrets`' full pack: that pack carries
# entropy-only rules (bare 64-hex, bare 32-alnum, bare UUID) which match
# *every* `sha256:` reference this facet is built out of. The prefixed subset
# is the one that is safe to run over content-addressed material.
from novafabric.support_bundle._redact import _VALUE_SECRET_PATTERNS

FACET_NAME = "settlement"
SCHEMA_VERSION = "0.1.0"

#: Payment protocols ADR-0163 D1 binds by reference. `other` exists so an
#: unlisted protocol is recorded as itself rather than mislabelled as one of
#: the six — an unknown protocol is a fact, not an error.
Protocol = Literal[
    "ap2",
    "trusted_agent",
    "machine_payments",
    "x402",
    "shared_payment_token",
    "iso20022",
    "other",
]

#: A reference must be a `sha256:<64 hex>` digest.
#:
#: The spec's wider "digest **or** URI" shape is deferred to a later phase.
#: P1's whole job is the *binding*, and only a digest binds: a URI names a
#: place a confirmation was, which an offline verifier cannot check and which
#: says nothing about the bytes. Accepting one here would let a facet claim a
#: binding it does not have.
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


# ── Secret shapes (I-2) ───────────────────────────────────────────────────

#: Field *names* that may never appear in the facet, whatever their value.
#:
#: Key-driven because two of these are undetectable from the value alone: a
#: CVV is three digits, indistinguishable from any small number, and a wallet
#: seed phrase is ordinary words. For those, the field name is the only
#: signal there is.
#:
#: Deliberately narrower than the support bundle's `DENY_KEY_PATTERN`
#: (`token|secret|password|key|dsn|credential`): that pattern would reject
#: `signer_ref: "network:visa-ic:key-2026"`, and ADR-0163 D2 explicitly
#: *requires* a key **identifier** to be storable. Rejecting the field the
#: ADR mandates would be worse than the leak it prevents.
_SECRET_KEY_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:"
    r"pan|cvv|cvc|cvv2|csc|card_?number|primary_?account_?number|"
    r"private_?key|priv_?key|seed_?phrase|mnemonic|"
    r"raw_?token|payment_?token|card_?token|card_?credential"
    r")(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)

#: A 13–19 digit group, optionally separated by spaces or dashes.
#:
#: The lookarounds require the group to be delimited by non-alphanumerics.
#: Without them a `sha256:` digest whose hex happens to contain a long run of
#: decimal characters would be read as a card number — a real (~0.5% per
#: digest) false positive, and the failure mode would be a settlement facet
#: that intermittently refuses to build. A digit run *inside* a hex string is
#: not a PAN; a delimited one might be, and then Luhn decides.
_PAN_RE = re.compile(r"(?<![0-9A-Za-z])(?:\d[ -]?){12,18}\d(?![0-9A-Za-z])")

#: ISO 13616 IBAN shape. Uppercase-only by construction, so it cannot collide
#: with the lowercase-hex digests that make up the rest of the facet.
_IBAN_RE = re.compile(r"(?<![0-9A-Za-z])([A-Z]{2}[0-9]{2}[A-Z0-9]{11,30})(?![0-9A-Za-z])")

_API_KEY_RE = re.compile(
    "|".join(f"(?:{pattern})" for _rule_id, pattern in _VALUE_SECRET_PATTERNS)
)


class PaymentSecretRejectedError(Exception):
    """Raised when a payment secret is offered to the settlement facet.

    Carries the field path and the rule that fired, never the value: this
    exception is destined for a log, and echoing the PAN there would move the
    leak rather than stop it.

    Deliberately **not** a ``ValueError``. Pydantic catches ``ValueError``
    inside a validator and folds it into a ``ValidationError`` alongside
    ordinary shape complaints — so a caller that leaked a card number would
    see "1 validation error" and might well retry with a different value. A
    leak is not a validation nit; the named type has to survive.
    """

    def __init__(self, path: str, rule: str) -> None:
        self.path = path
        self.rule = rule
        super().__init__(
            f"settlement facet field {path!r} looks like a {rule}; payment "
            "secrets are never captured (ADR-0163 I-2). Store a sha256: "
            "digest of the artifact instead — the value was not stored, "
            "redacted, or logged."
        )


class InvalidReferenceError(Exception):
    """Raised when a reference is not a ``sha256:`` digest.

    Not a ``ValueError``, for the same reason as
    :class:`PaymentSecretRejectedError`: the most likely bad reference is an
    inlined artifact or a raw token, and the caller must see *which* rule
    rejected it, not a generic validation failure.
    """


class DiscrepancyLaunderedError(Exception):
    """Raised when a reconciliation record claims a match it has not earned.

    Not a ``ValueError``, for the reason given on
    :class:`PaymentSecretRejectedError`. This is the exact failure NF-312
    exists to make impossible — a record that shows a discrepancy while
    reporting ``reconciled: true``, or that reports a match having compared
    nothing — and it must not be foldable into a generic ``ValidationError``
    alongside a mistyped field.
    """


class UnconfirmedFinalityError(Exception):
    """Raised when a finality record claims a confirmation it does not carry.

    Not a ``ValueError``, for the same reason: a record asserting network or
    on-chain confirmation with no ``confirmation_ref`` is a specific and
    dangerous defect, not a shape complaint.
    """


def _luhn_ok(digits: str) -> bool:
    """Return True if ``digits`` passes the Luhn check (ISO/IEC 7812)."""
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = ord(char) - 48
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _iban_ok(candidate: str) -> bool:
    """Return True if ``candidate`` passes the ISO 7064 mod-97 check.

    Shape alone is not enough: `EU2026SETTLEMENT001` is an entirely plausible
    settlement identifier and matches the IBAN regex. mod-97 is what separates
    a bank account from a reference that happens to look like one.
    """
    rearranged = candidate[4:] + candidate[:4]
    return int("".join(str(int(char, 36)) for char in rearranged)) % 97 == 1


def _check_scalar(value: Any, path: str) -> None:
    """Raise if a scalar carries a payment-secret shape."""
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        # Integers are scanned too. A PAN moved into a numeric field is still
        # a PAN, and `amount_minor` is exactly the int field an integration
        # bug would put one in. Real minor-unit amounts of 13+ digits are
        # absurd (>= 10 billion major units), so the false-positive cost is
        # nil against a leak this module exists to prevent.
        text = str(abs(value))
    elif isinstance(value, str):
        text = value
    else:
        # Floats never reach here: `Money` forbids them (see its docstring).
        return

    for match in _PAN_RE.finditer(text):
        digits = re.sub(r"[ -]", "", match.group())
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            raise PaymentSecretRejectedError(path, "card number (PAN)")

    for match in _IBAN_RE.finditer(text):
        if _iban_ok(match.group(1)):
            raise PaymentSecretRejectedError(path, "bank account number (IBAN)")

    if _API_KEY_RE.search(text):
        raise PaymentSecretRejectedError(path, "credential / API key")


def reject_payment_secrets(value: Any, *, path: str = "") -> None:
    """Walk ``value`` and raise on the first payment secret found (I-2).

    Raises:
        PaymentSecretRejectedError: on a PAN, IBAN, credential, or a field
            *named* for a secret (CVV, seed phrase, raw token, private key).
    """
    if isinstance(value, Mapping):
        for key, child in value.items():
            name = str(key)
            child_path = f"{path}.{name}" if path else name
            if _SECRET_KEY_RE.search(name):
                raise PaymentSecretRejectedError(child_path, "payment secret field")
            reject_payment_secrets(child, path=child_path)
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            reject_payment_secrets(item, path=f"{path}[{index}]")
        return
    _check_scalar(value, path)


# ── References ────────────────────────────────────────────────────────────


def digest_artifact(content: str | bytes) -> str:
    """Return the ``sha256:`` digest of a mandate or settlement confirmation.

    Callers hash the artifact and keep the artifact out of the capsule. The
    form matches every other digest in the capsule, so a verifier does not
    have to know which subsystem wrote it.
    """
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def verify_ref_binding(ref: str | None, artifact: str | bytes) -> bool:
    """Re-verify a reference against the artifact it claims to bind.

    Returns False for a missing reference. An unbound facet is not
    "trivially valid" — it is the case a verifier exists to surface, and
    returning True would pass exactly the facets with nothing to check.
    """
    if not ref:
        return False
    return ref == digest_artifact(artifact)


# ── Models ────────────────────────────────────────────────────────────────


class Money(BaseModel):
    """A settled amount, as integer minor units plus an ISO-4217 code.

    Never a float. Binary floating point cannot represent 162.40, so a float
    amount would differ from the network's confirmation in the last place —
    and the entire point of NF-312 reconciliation (P2) is comparing an
    observed amount against an authorized one. An amount that is 0.01 wrong
    turns a clean settlement into a phantom `over_authorized` discrepancy, or
    hides a real one. Minor units keep the comparison exact and integral.

    The currency code is what makes minor units meaningful: 500 is EUR 5.00
    but JPY 500 (zero-decimal) and KWD 0.500 (three-decimal). Storing the
    amount without the code would be storing a number, not a sum of money.
    """

    model_config = ConfigDict(extra="allow")

    #: Minor units (cents, satang, …). Non-negative: a reversal is recorded
    #: as its own positively-signed hop in NF-320 reversal lineage (P4), not
    #: as a negative settlement, so a negative here means a caller bug.
    amount_minor: int = Field(ge=0)
    #: ISO-4217 alpha-3. Shape-validated only — pinning the full code list
    #: would mean shipping a table that goes stale every time a currency is
    #: redenominated, to reject a value NovaFabric only ever records (I-4).
    currency: str

    @field_validator("currency")
    @classmethod
    def _validate_currency(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Z]{3}", value):
            raise ValueError(
                f"currency {value!r} is not an ISO-4217 alpha-3 code "
                "(three uppercase letters, e.g. 'EUR')"
            )
        return value


# ── NF-312: authorized ↔ observed reconciliation ──────────────────────────

#: The discrepancy vocabulary fixed by ADR-0163 D2 / spec §3.6.
#:
#: Closed on purpose, and note what is *not* in it: there is no
#: ``under_authorized``. A payment mandate authorizes a **ceiling**
#: (``max_amount``), so a charge below it is inside the authorization and is
#: not a discrepancy against the mandate. Recording one would manufacture a
#: finding the mandate does not support — and an under-settlement that *is* a
#: problem (goods paid for in part) is a commercial dispute, which NF-317
#: assembles evidence for and NovaFabric never adjudicates (I-4).
Discrepancy = Literal[
    "over_authorized",
    "payee_mismatch",
    "currency_mismatch",
    "expired_mandate",
    "out_of_scope",
]


class AuthorizedTerms(BaseModel):
    """What the NF-087 payment mandate authorized.

    Every field is optional because a mandate is read from a protocol
    NovaFabric does not control, and a term that was not read is *unknown* —
    never "unrestricted". :func:`reconcile` therefore compares only the terms
    that are present, and records which ones those were.
    """

    model_config = ConfigDict(extra="allow")

    #: The ceiling the mandate authorized, with its currency (see
    #: :class:`Money` on why this is integer minor units and not a float).
    max_amount: Money | None = None
    payee: str | None = None
    #: RFC-3339 instant after which the mandate no longer authorizes.
    expiry: str | None = None
    #: Scope tokens the mandate permits. A list, against the observed charge's
    #: single scope.
    scope: list[str] | None = None


class ObservedSettlement(BaseModel):
    """What the settlement confirmation says actually happened.

    Deliberately a separate model from :class:`AuthorizedTerms` rather than a
    second instance of one shape: the two sides are not symmetric. One is a
    ceiling and a permission set, the other is a single charge that occurred.
    Collapsing them into one type invites code that "merges" them, which is
    exactly the resolution this module must never perform.
    """

    model_config = ConfigDict(extra="allow")

    amount: Money | None = None
    payee: str | None = None
    #: RFC-3339 instant the charge was observed, compared against
    #: ``AuthorizedTerms.expiry``.
    observed_at: str | None = None
    scope: str | None = None


class MandateReconciliation(BaseModel):
    """Authorized vs observed, as a record (NF-312).

    Both sides are kept verbatim. Neither is corrected, and no net figure is
    derived: an auditor needs to see the two numbers that disagree, not a
    third number this module invented from them (I-4, ADR-0163 D2 — "it
    records the discrepancy, it does **not** void the charge").

    ``reconciled`` is deliberately *not* "no discrepancies were found". It is
    "terms were compared and they matched". Those differ when nothing could be
    compared — a mandate whose terms were unreadable produces an empty
    ``discrepancies`` list, and calling that ``reconciled: true`` would launder
    "we checked nothing" into "it matched". ``compared`` carries the basis, and
    the validator below refuses the laundered combination outright.
    """

    model_config = ConfigDict(extra="allow")

    authorized: AuthorizedTerms = Field(default_factory=AuthorizedTerms)
    observed: ObservedSettlement = Field(default_factory=ObservedSettlement)
    #: True only when at least one term was compared and none disagreed.
    reconciled: bool = False
    discrepancies: list[Discrepancy] = Field(default_factory=list)
    #: Which terms were actually compared (``amount``, ``payee``, ``currency``,
    #: ``expiry``, ``scope``). Present so a reader can tell a clean
    #: reconciliation from an empty one.
    compared: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _reconciled_must_be_earned(self) -> MandateReconciliation:
        """A discrepancy, or an empty basis, can never render as reconciled.

        In the model rather than in :func:`reconcile` so that no construction
        path — direct instantiation, ``model_validate`` of a facet read off
        disk, ``model_copy(update=…)`` — can produce a record that reads
        "authorized and observed agree" when it holds evidence they do not, or
        holds no evidence at all. This is the reconciliation-side twin of
        ``non_repudiation_broken``.
        """
        if self.discrepancies and self.reconciled:
            raise DiscrepancyLaunderedError(
                "mandate_reconciliation claims reconciled=true while "
                f"recording discrepancies {sorted(set(self.discrepancies))}; a "
                "discrepancy is recorded, never resolved (ADR-0163 D2, I-4)"
            )
        if self.reconciled and not self.compared:
            raise DiscrepancyLaunderedError(
                "mandate_reconciliation claims reconciled=true while having "
                "compared no terms; 'nothing was checked' must not be recorded "
                "as 'authorized and observed agree' (ADR-0163 I-4)"
            )
        return self


def _parse_instant(value: str | None) -> datetime | None:
    """Parse an RFC-3339 instant, or return None if it is unreadable.

    Unreadable is *not* an error here. An expiry NovaFabric cannot parse is a
    term it did not read, which by the absent-is-not-false rule yields no
    finding at all — rather than an ``expired_mandate`` finding invented from
    a formatting problem, or a hard failure that would cost the run its whole
    reconciliation record (I-3, fail-open).
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def reconcile(
    authorized: AuthorizedTerms, observed: ObservedSettlement
) -> MandateReconciliation:
    """Compare authorized against observed and record the difference (NF-312).

    Records; never resolves. The returned record carries both sides unchanged,
    the discrepancies found, and ``compared`` — the terms that had a value on
    both sides and were therefore actually checked. A term missing on either
    side yields no finding: it was not read, which is not the same as it being
    satisfied.

    Two orderings matter and are not obvious:

    - A **currency mismatch suppresses the amount comparison.** EUR 150
      authorized against USD 162.40 observed cannot be called
      ``over_authorized`` without an FX rate and a rate date, and choosing
      those would be NovaFabric adjudicating the charge (I-4). The mismatch is
      the finding; the amounts stand side by side, uncompared.
    - An **amount at or below the ceiling is clean.** ``max_amount`` is a
      maximum, so only ``observed > max_amount`` is ``over_authorized`` — see
      the :data:`Discrepancy` note on why there is no under-settlement code.

    Nothing here blocks, voids, reverses, or corrects the charge. It cannot:
    the charge already happened somewhere NovaFabric has never been (I-1).
    """
    discrepancies: list[Discrepancy] = []
    compared: list[str] = []

    if authorized.max_amount is not None and observed.amount is not None:
        compared.append("currency")
        if authorized.max_amount.currency != observed.amount.currency:
            # The amount comparison lives in the `else` branch on purpose:
            # suppression-on-mismatch is structural, not a later filter.
            discrepancies.append("currency_mismatch")
        else:
            compared.append("amount")
            if observed.amount.amount_minor > authorized.max_amount.amount_minor:
                discrepancies.append("over_authorized")

    if authorized.payee is not None and observed.payee is not None:
        compared.append("payee")
        if authorized.payee != observed.payee:
            discrepancies.append("payee_mismatch")

    expiry = _parse_instant(authorized.expiry)
    observed_at = _parse_instant(observed.observed_at)
    if expiry is not None and observed_at is not None:
        compared.append("expiry")
        # Both instants must be comparable. A naive/aware mix raises in
        # Python, and a mandate expiry written without an offset is a real
        # producer habit — so it is treated as unreadable rather than fatal.
        try:
            expired = observed_at > expiry
        except TypeError:
            compared.remove("expiry")
        else:
            if expired:
                discrepancies.append("expired_mandate")

    if authorized.scope is not None and observed.scope is not None:
        compared.append("scope")
        if observed.scope not in authorized.scope:
            discrepancies.append("out_of_scope")

    return MandateReconciliation(
        authorized=authorized,
        observed=observed,
        reconciled=bool(compared) and not discrepancies,
        discrepancies=discrepancies,
        compared=compared,
    )


# ── NF-313: settlement finality ───────────────────────────────────────────

#: What :func:`finality_state` reports when no finality record exists.
#:
#: A string rather than ``None`` so that a caller formatting the value into a
#: report cannot silently print an empty cell where the auditor's single most
#: important question — did the money actually move? — belongs.
UNKNOWN_FINALITY = "unknown"

#: Declared/observed settlement states.
#:
#: ``captured`` is **added to ADR-0163's list** (`initiated | authorized |
#: settled | pending | failed | reversed`), which is the one place this slice
#: departs from the ADR's enumeration. On card rails authorize, capture and
#: settle are three distinct positions of the money, and the ADR's list offers
#: no home for the middle one: folding a captured payment into ``authorized``
#: understates it, and folding it into ``settled`` reports funds as settled
#: that the acquirer has not yet settled — the precise "not-yet-final rendered
#: as final" error NF-313 exists to prevent. Adding a value is additive and
#: breaks no consumer of the ADR's six.
FinalityState = Literal[
    "initiated",
    "authorized",
    "captured",
    "settled",
    "pending",
    "failed",
    "reversed",
]

#: Where the state came from. ``declared`` means the producer said so and
#: nothing corroborated it — kept distinguishable from a network or on-chain
#: confirmation precisely so a claim is never read as a confirmation.
FinalitySource = Literal["network_confirmation", "onchain_confirmation", "declared"]

#: The only state that means the money is final. A frozenset rather than an
#: inline comparison so that "which states count as final" has exactly one
#: definition; the trap here is a second, more generous copy appearing in a
#: report or exporter later.
_FINAL_STATES = frozenset({"settled"})


class FinalityRecord(BaseModel):
    """The declared/observed settlement state (NF-313).

    NovaFabric records what a network or producer said; it does not assert
    legal settlement finality, and no state here is a guarantee that funds are
    irrevocable (ADR-0163 D2, I-4).
    """

    model_config = ConfigDict(extra="allow")

    state: FinalityState
    finality_source: FinalitySource
    #: Digest of the ISO 20022 ``sese.025``-style confirmation, network
    #: auth-code artifact, or on-chain transaction artifact.
    confirmation_ref: str | None = None
    #: RFC-3339 instant the state was observed. Required: a settlement state
    #: with no time attached cannot be ordered against a later reversal, and
    #: NF-320 reversal lineage is built on exactly that ordering.
    observed_at: str

    @field_validator("confirmation_ref")
    @classmethod
    def _validate_confirmation_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _DIGEST_RE.match(value):
            raise InvalidReferenceError(
                f"confirmation_ref {value!r} is not a 'sha256:<64 hex>' digest "
                "(ADR-0163 D1)"
            )
        return value

    @model_validator(mode="after")
    def _confirmation_must_exist_to_be_claimed(self) -> FinalityRecord:
        """A corroborated source requires the corroborating reference.

        ``finality_source`` is the field that tells an auditor whether anyone
        other than the producer vouched for this state. A record saying
        ``network_confirmation`` with no ``confirmation_ref`` claims exactly
        that corroboration while holding none — so it is refused rather than
        downgraded to ``declared``, because silently rewriting a producer's
        claim would hide an integration that is emitting unfounded ones.
        """
        if self.finality_source != "declared" and self.confirmation_ref is None:
            raise UnconfirmedFinalityError(
                f"finality record declares source {self.finality_source!r} but "
                "carries no confirmation_ref; a state is only 'confirmed' if "
                "the confirmation is bound (ADR-0163 D2/NF-313). Use "
                "finality_source='declared' for an uncorroborated state."
            )
        return self


def finality_state(facet: SettlementFacet | None) -> str:
    """Return the recorded finality state, or ``"unknown"``.

    The absence of a finality record means *nobody recorded whether the money
    moved*. It never means ``settled``, and it never means ``failed`` either:
    both would be this function inventing an observation. ``unknown`` is the
    honest answer and is the value every caller must be able to render.
    """
    if facet is None or facet.finality is None:
        return UNKNOWN_FINALITY
    return facet.finality.state


def is_final(facet: SettlementFacet | None) -> bool:
    """Return True only for a recorded ``settled`` state.

    Every other input — no facet, no finality record, ``authorized``,
    ``captured``, ``pending``, ``failed``, ``reversed`` — returns False.
    ``captured`` in particular is money taken but not yet settled by the
    acquirer, and reporting it as final is the failure this function exists to
    prevent.

    A ``True`` here means "a settled state is on the record", not "the funds
    are irrevocable in law" — see :class:`FinalityRecord` (I-4).
    """
    if facet is None or facet.finality is None:
        return False
    return facet.finality.state in _FINAL_STATES


# ── NF-314: non-repudiation binding ───────────────────────────────────────

#: Signature schemes ADR-0163 D2 binds by reference.
SigScheme = Literal["iso20022_cms", "w3c_vc", "protocol_native", "detached_jws"]


class NonRepudiationBinding(BaseModel):
    """Anchors a signed settlement artifact to the capsule root (NF-314).

    Three references make the anchor: ``signed_digest`` (what was signed),
    ``signer_ref`` (who asserts it — a key *identifier*, never a private key),
    and ``bound_root`` (the capsule root it is tied to). Missing any one of
    them and there is no anchor, whatever the record says about itself.

    **Scope of "verified".** What can be re-checked offline here is that the
    digest still binds the artifact and that the anchor names this capsule's
    root. Verifying the signature itself against the signer's public key needs
    a key NovaFabric does not hold and must not fetch (local-first, no network),
    so it belongs to the sealing phase (P5) and is not claimed by this record.
    ``non_repudiation_broken`` therefore means "the anchor does not hold",
    which is the ADR's own framing of a broken binding — not "the signature is
    forged", which this module never asserts.
    """

    model_config = ConfigDict(extra="allow")

    sig_scheme: SigScheme
    #: Digest of the signed settlement/mandate artifact.
    signed_digest: str | None = None
    #: Reference to the asserting party's key id, e.g.
    #: ``network:visa-ic:key-2026``. Never the key material (I-2).
    signer_ref: str | None = None
    #: Digest of the capsule root the signature is anchored to.
    bound_root: str | None = None
    #: True when the anchor is incomplete or failed re-verification. Defaults
    #: to True: an unstated binding is a broken one, never an intact one.
    non_repudiation_broken: bool = True

    @field_validator("signed_digest", "bound_root")
    @classmethod
    def _validate_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _DIGEST_RE.match(value):
            raise InvalidReferenceError(
                f"reference {value!r} is not a 'sha256:<64 hex>' digest; the "
                "signed artifact is bound by digest and held elsewhere "
                "(ADR-0163 D1)"
            )
        return value

    @model_validator(mode="after")
    def _incomplete_anchor_is_broken(self) -> NonRepudiationBinding:
        """No complete anchor ⇒ ``non_repudiation_broken``, on every path.

        Enforced in the model rather than only in
        :func:`build_non_repudiation` so that ``model_validate`` of untrusted
        JSON, ``model_copy(update=…)`` and direct instantiation cannot mint a
        record claiming an intact binding it does not have. This is the field
        a dispute turns on: a forged ``non_repudiation_broken: false`` on a
        record with no ``bound_root`` would present an unanchored signature as
        a tamper-evident one, which is the single most valuable lie an
        attacker could tell about this facet.

        Note the direction. Missing references force ``broken = True``; the
        flag is never *cleared* here, because a record already marked broken
        by a verifier that had the artifact knows more than this validator,
        which only sees shape.
        """
        if (
            self.signed_digest is None
            or self.signer_ref is None
            or not self.signer_ref.strip()
            or self.bound_root is None
        ):
            self.non_repudiation_broken = True
        return self


def verify_non_repudiation(
    binding: NonRepudiationBinding,
    *,
    artifact: str | bytes | None = None,
    capsule_root: str | None = None,
) -> bool:
    """Re-verify an anchor offline; True only if it still holds.

    Checks what is supplied: the digest against ``artifact`` (reusing P1's
    :func:`verify_ref_binding`), and ``bound_root`` against ``capsule_root``.
    An already-broken binding stays broken without further checking — there is
    nothing to re-verify once a reference is missing.

    Supplying neither ``artifact`` nor ``capsule_root`` returns the binding's
    own recorded state rather than re-deriving it: a verifier given nothing to
    check against has learned nothing, and returning True on that basis would
    turn "not checked" into "checked and intact".
    """
    if binding.non_repudiation_broken:
        return False
    if artifact is not None and not verify_ref_binding(binding.signed_digest, artifact):
        return False
    if capsule_root is not None and binding.bound_root != capsule_root:
        return False
    return True


def build_non_repudiation(
    *,
    sig_scheme: SigScheme,
    signed_digest: str | None = None,
    signer_ref: str | None = None,
    bound_root: str | None = None,
    resolver: Callable[[str], str | bytes | None] | None = None,
    capsule_root: str | None = None,
) -> NonRepudiationBinding:
    """Record a non-repudiation anchor, re-verifying it where possible.

    ``resolver`` is asked to produce the bytes ``signed_digest`` names. The
    binding is marked broken when the resolver returns ``None`` (the artifact
    cannot be produced) **or** returns bytes whose digest differs (the ref
    names something else). Both are the same fact in a dispute: there is no
    verifiable artifact behind the signature. Calling the second case intact
    because a lookup succeeded would be the more dangerous of the two errors.

    With refs but no ``resolver`` and no ``capsule_root``, the binding is
    recorded intact: the caller has asserted an anchor this function was given
    no way to check, and inventing a ``broken`` mark for an unchecked anchor
    would report a finding nobody made. With any reference missing it is broken
    regardless — see :meth:`NonRepudiationBinding._incomplete_anchor_is_broken`.

    Raises:
        InvalidReferenceError: if a ref is not a ``sha256:`` digest.
        PaymentSecretRejectedError: if any argument carries a payment secret.
    """
    broken = False
    if resolver is not None and signed_digest is not None:
        try:
            artifact = resolver(signed_digest)
        except Exception:  # noqa: BLE001
            # A resolver that raised told us about itself, not about the
            # artifact. Fail-open (I-3): record the anchor as unverified
            # rather than failing the capsule over a lookup error.
            artifact = None
        broken = artifact is None or not verify_ref_binding(signed_digest, artifact)
    if not broken and capsule_root is not None and bound_root != capsule_root:
        broken = True
    return NonRepudiationBinding(
        sig_scheme=sig_scheme,
        signed_digest=signed_digest,
        signer_ref=signer_ref,
        bound_root=bound_root,
        non_repudiation_broken=broken,
    )


class SettlementFacet(BaseModel):
    """The optional ``facets.settlement`` block (NF-311, D1).

    Every field is a reference, a digest, an amount, or a state. The mandate
    VC and the settlement confirmation stay where they are; NF-087 remains
    the single source of truth for the mandate and is bound, not re-emitted
    (ADR-0163 alternative 2).
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    protocol: Protocol
    protocol_version: str | None = None
    #: Digest of the NF-087 AP2 Payment Mandate that authorized the charge.
    mandate_ref: str | None = None
    #: Digest of the settlement confirmation (ISO 20022 `sese.025`, network
    #: auth-code, or on-chain tx-hash artifact).
    settlement_ref: str | None = None
    #: Digest of the capsule root this facet is sealed into.
    bound_root: str | None = None
    #: The settled amount, when observed. Optional: a facet that binds the
    #: two artifacts is already useful evidence without one.
    amount: Money | None = None
    #: NF-312. Absent means no reconciliation was performed — never that the
    #: mandate and the charge agreed.
    mandate_reconciliation: MandateReconciliation | None = None
    #: NF-313. Absent means the finality of this payment is *unknown*; see
    #: :func:`finality_state`.
    finality: FinalityRecord | None = None
    #: NF-314. Absent means no anchor was recorded — never that one held.
    non_repudiation: NonRepudiationBinding | None = None

    @field_validator("mandate_ref", "settlement_ref", "bound_root")
    @classmethod
    def _validate_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _DIGEST_RE.match(value):
            raise InvalidReferenceError(
                f"reference {value!r} is not a 'sha256:<64 hex>' digest; "
                "artifact bytes, raw tokens and URIs are never stored here "
                "(ADR-0163 D1)"
            )
        return value

    @model_validator(mode="after")
    def _reject_secrets(self) -> SettlementFacet:
        """Enforce I-2 on the whole facet, including ``extra`` fields.

        Run as a model validator rather than inside :func:`build_facet` so
        that *no* construction path — direct instantiation, ``model_validate``
        of untrusted JSON, ``model_copy(update=…)`` — can produce a facet
        carrying a payment secret. I-2 has to be structural, not procedural.
        """
        reject_payment_secrets(self.model_dump())
        return self

    @model_validator(mode="after")
    def _anchor_must_match_this_capsule(self) -> SettlementFacet:
        """A non-repudiation anchor naming another root is broken (NF-314).

        The facet's own ``bound_root`` says which capsule this evidence
        belongs to. An anchor pointing at a *different* root is a signature
        over some other run's material, and it is exactly the shape a
        transplanted binding takes: lift an intact ``non_repudiation`` block
        out of a real capsule, paste it into a fabricated one, and every
        field-level check still passes. Only the cross-field comparison
        catches it, so it lives here rather than on the binding — which cannot
        see the facet that contains it.

        Marked broken rather than refused: a mismatched anchor is a finding an
        auditor needs to *see recorded*, and raising would delete the evidence
        that someone tried it.
        """
        if (
            self.non_repudiation is not None
            and self.bound_root is not None
            and self.non_repudiation.bound_root is not None
            and self.non_repudiation.bound_root != self.bound_root
        ):
            self.non_repudiation.non_repudiation_broken = True
        return self


# ── Facet assembly ────────────────────────────────────────────────────────


def build_facet(
    *,
    protocol: Protocol,
    protocol_version: str | None = None,
    mandate_ref: str | None = None,
    settlement_ref: str | None = None,
    bound_root: str | None = None,
    amount: Money | None = None,
    mandate_reconciliation: MandateReconciliation | None = None,
    finality: FinalityRecord | None = None,
    non_repudiation: NonRepudiationBinding | None = None,
    extra: Mapping[str, Any] | None = None,
) -> SettlementFacet | None:
    """Build the settlement facet, or ``None`` when there is nothing to bind.

    Fail-open (I-3): a run whose agent moved no money yields ``None``, not an
    exception. A facet naming a protocol but binding no mandate, no
    confirmation and no amount is not evidence — it is a claim that a payment
    happened with nothing to check it against, which is worse than silence.

    Note the asymmetry with :class:`PaymentSecretRejectedError`: *absent*
    settlement material is fail-open, *poisoned* settlement material is not.
    D6's "never an exception" governs the missing case; a caller that handed
    over a PAN needs to be told (see the module docstring).
    """
    material = (
        mandate_ref,
        settlement_ref,
        amount,
        mandate_reconciliation,
        finality,
        non_repudiation,
    )
    if all(item is None for item in material):
        return None
    return SettlementFacet(
        protocol=protocol,
        protocol_version=protocol_version,
        mandate_ref=mandate_ref,
        settlement_ref=settlement_ref,
        bound_root=bound_root,
        amount=amount,
        mandate_reconciliation=mandate_reconciliation,
        finality=finality,
        non_repudiation=non_repudiation,
        **dict(extra or {}),
    )


def attach_facet(
    capsule: dict[str, Any], facet: SettlementFacet | None
) -> dict[str, Any]:
    """Attach the settlement facet to a capsule dict, additively.

    Writes nothing when there is no facet: a run with no settlement material
    must be byte-identical to one captured before this feature existed (I-3).
    Returns a new dict; the input is not mutated.
    """
    if facet is None:
        return capsule
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    facets[FACET_NAME] = facet.model_dump(exclude_none=True)
    out["facets"] = facets
    return out
