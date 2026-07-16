"""Deterministic Replay Attestation (ADR-0094 B, ``evidence/replay_attestation.py``).

A signed reproducibility certificate for a NovaFabric replay. It is an
**additive superset** of :class:`ReperformanceAttestation`
(``evidence/reperformance.py``, ADR-0087) — an extension, not a fork: every
existing re-performance field is carried unchanged, and the new fields add a
``determinism_class``, the ``pinned`` model/env block that justifies the class,
a ``bounded_equivalence`` record, an ``input_authorization``, and a
``ledger_ref`` back-link (B → A) to the ledger checkpoint that sealed the run.

The certificate matches ``schemas/replay-attestation.schema.json`` and is wrapped
as an in-toto predicate ``https://novafabric.io/replay-attestation/v0`` —
never a third top-level format (ADR-0034).

Determinism classification (normative downgrade rule, ADR-0094 §B):

- **BIT_EXACT** — ``original == replay`` digests AND every required pin concrete
  (model digest + seed present, deterministic lock mode);
- **BOUNDED_EQUIVALENT** — a named metric distance ``<= threshold``
  (``>= threshold`` when ``higher_is_closer``);
- **NON_DETERMINISTIC** — any required pin null/missing, a mismatch with no
  bounded metric, or a distance outside the threshold.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from novafabric.evidence.intoto import dsse_sign, make_intoto_statement
from novafabric.evidence.reperformance import ReperformanceAttestation

REPLAY_ATTESTATION_PREDICATE_TYPE = "https://novafabric.io/replay-attestation/v0"

DeterminismClass = Literal["BIT_EXACT", "BOUNDED_EQUIVALENT", "NON_DETERMINISTIC"]
LockMode = Literal["deterministic", "best-effort"]


class PinnedModel(BaseModel):
    """Model identity + sampling pins (from ``model-call.schema.json``)."""

    request_model: str
    response_model: str
    model_digest: str | None = None
    seed: int | None = None
    temperature: float | None = None
    top_p: float | None = None


class PinnedEnv(BaseModel):
    """Environment pins (from ``environment.schema.json``)."""

    lock_mode: LockMode
    container_image_digest: str | None = None
    python_lock_file_hash: str | None = None
    inference_deterministic: bool | None = None


class PinnedBlock(BaseModel):
    """The pinned dimensions that justify the determinism class."""

    model: PinnedModel
    env: PinnedEnv


class BoundedEquivalence(BaseModel):
    """Quantitative bound that justifies ``BOUNDED_EQUIVALENT``."""

    metric: str
    distance: float
    threshold: float
    higher_is_closer: bool | None = None


class InputAuthorization(BaseModel):
    """Binds the certificate to a specific authorized input set."""

    inputs_digest: str
    authorized_by: str | None = None
    authorized_at: str | None = None


class LedgerRef(BaseModel):
    """B → A back-link to the ledger checkpoint that sealed the source run."""

    checkpoint_id: str
    capsule_id: str | None = None
    merkle_root: str | None = None


class ReplayAttestation(BaseModel):
    """Signed reproducibility certificate (``replay-attestation`` v0.1)."""

    schema_version: str = "0.1.0"
    run_id: str
    capsule_hash: str
    replayed_at: str
    replay_mode: Literal["forensic", "mocked", "semantic", "exact", "intervention"]
    outcome_digest: str
    match: Literal["exact", "semantic-match", "mismatch"]
    determinism_class: DeterminismClass
    pinned: PinnedBlock
    bounded_equivalence: BoundedEquivalence | None = None
    input_authorization: InputAuthorization | None = None
    ledger_ref: LedgerRef | None = None
    reasons: list[str] | None = None
    replayer: dict[str, str] | None = Field(
        default_factory=lambda: {"name": "novafabric", "version": "unknown"}
    )
    notes: str | None = None
    extensions: dict[str, Any] | None = None

    def to_record(self) -> dict[str, Any]:
        """Schema-valid record (drops unset optional blocks)."""
        return self.model_dump(exclude_none=True)


def _is_fully_pinned(pinned: PinnedBlock) -> list[str]:
    """Return the list of missing required-for-BIT_EXACT pins (empty = fully pinned)."""
    missing: list[str] = []
    if pinned.model.model_digest is None:
        missing.append("model_digest")
    if pinned.model.seed is None:
        missing.append("seed")
    if pinned.env.lock_mode != "deterministic":
        missing.append("lock_mode!=deterministic")
    return missing


def classify_determinism(
    original_outcome_digest: str,
    replay_outcome_digest: str,
    pinned: PinnedBlock,
    *,
    bounded_metric: str | None = None,
    distance: float | None = None,
    threshold: float | None = None,
    higher_is_closer: bool = False,
) -> DeterminismClass:
    """Classify the reproducibility property of a replay.

    - ``BIT_EXACT`` iff digests are equal AND the environment is fully pinned
      deterministic;
    - ``BOUNDED_EQUIVALENT`` iff a named metric distance is within threshold;
    - ``NON_DETERMINISTIC`` otherwise.
    """
    fully_pinned = not _is_fully_pinned(pinned)
    if original_outcome_digest == replay_outcome_digest and fully_pinned:
        return "BIT_EXACT"

    if (
        bounded_metric is not None
        and distance is not None
        and threshold is not None
    ):
        within = distance >= threshold if higher_is_closer else distance <= threshold
        if within:
            return "BOUNDED_EQUIVALENT"

    return "NON_DETERMINISTIC"


def pinned_block_from_capsule(capsule_dir: Path) -> PinnedBlock:
    """Extract the pinned model/env dimensions recorded in a capsule.

    Reads the first record of ``model-calls.jsonl`` (OTel ``gen_ai.*`` pins,
    plus the optional ``model_digest`` extension) and ``env.lock`` (lock mode,
    container image digest, python lock-file hash, inference determinism).
    Missing values are recorded as ``None`` — never fabricated — which
    honestly precludes ``BIT_EXACT`` via the normative downgrade rule.
    """
    model = PinnedModel(request_model="unknown", response_model="unknown")
    calls_path = capsule_dir / "model-calls.jsonl"
    if calls_path.exists():
        for line in calls_path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                break
            request_model = str(rec.get("gen_ai.request.model", "unknown"))
            model = PinnedModel(
                request_model=request_model,
                response_model=str(rec.get("gen_ai.response.model", request_model)),
                model_digest=rec.get("model_digest"),
                seed=rec.get("gen_ai.request.seed"),
                temperature=rec.get("gen_ai.request.temperature"),
                top_p=rec.get("gen_ai.request.top_p"),
            )
            break

    env = PinnedEnv(lock_mode="best-effort")
    lock_path = capsule_dir / "env.lock"
    if lock_path.exists():
        try:
            data = yaml.safe_load(lock_path.read_text()) or {}
        except yaml.YAMLError:
            data = {}
        if isinstance(data, dict):
            mode = data.get("mode")
            container = data.get("container") or {}
            python = data.get("python") or {}
            inference = (data.get("hardware") or {}).get("inference") or {}
            env = PinnedEnv(
                lock_mode=mode if mode == "deterministic" else "best-effort",
                container_image_digest=container.get("image_digest"),
                python_lock_file_hash=python.get("lock_file_hash"),
                inference_deterministic=inference.get("deterministic"),
            )
    return PinnedBlock(model=model, env=env)


def classify_match_verdict(
    match: str, pinned: PinnedBlock
) -> tuple[DeterminismClass, list[str]]:
    """Classify a replay's match verdict without a measured bounded metric.

    Reuses :func:`classify_determinism`: an ``exact`` verdict means the replay
    reproduced the sealed outcome digest, so equal/unequal digest sentinels
    stand in for the digest pair. With no bounded-equivalence measurement, any
    non-exact verdict downgrades to ``NON_DETERMINISTIC`` (normative downgrade
    rule, ADR-0094 §B). Returns the class plus the honest reasons for any
    downgrade.
    """
    replay_digest = "outcome" if match == "exact" else "outcome:diverged"
    determinism_class = classify_determinism("outcome", replay_digest, pinned)
    reasons: list[str] = []
    if match != "exact":
        reasons.append(
            f"replay verdict is '{match}' (not exact) and no bounded metric was measured"
        )
    missing = _is_fully_pinned(pinned)
    if missing:
        reasons.append("missing required pins for BIT_EXACT: " + ", ".join(missing))
    return determinism_class, reasons


def from_reperformance(
    base: ReperformanceAttestation,
    *,
    determinism_class: DeterminismClass,
    pinned: PinnedBlock,
    bounded_equivalence: BoundedEquivalence | None = None,
    input_authorization: InputAuthorization | None = None,
    ledger_ref: LedgerRef | None = None,
    reasons: list[str] | None = None,
) -> ReplayAttestation:
    """Lift a :class:`ReperformanceAttestation` into a :class:`ReplayAttestation`.

    Every base field is carried unchanged (the superset invariant); the new
    determinism dimensions are layered on top.
    """
    return ReplayAttestation(
        run_id=base.run_id,
        capsule_hash=base.capsule_hash,
        replayed_at=base.replayed_at,
        replay_mode=base.replay_mode,  # type: ignore[arg-type]
        outcome_digest=base.outcome_digest,
        match=base.match,
        determinism_class=determinism_class,
        pinned=pinned,
        bounded_equivalence=bounded_equivalence,
        input_authorization=input_authorization,
        ledger_ref=ledger_ref,
        reasons=reasons,
        replayer=base.replayer,
        notes=base.notes,
    )


def attest_replay(attestation: ReplayAttestation, signer: Any) -> dict[str, Any]:
    """Wrap the attestation in an in-toto statement and DSSE-sign it."""
    statement = make_intoto_statement(
        predicate_type=REPLAY_ATTESTATION_PREDICATE_TYPE,
        subject_name=f"run-capsule/{attestation.run_id}",
        subject_sha256=attestation.capsule_hash,
        predicate=attestation.to_record(),
    )
    envelope: dict[str, Any] = dsse_sign(statement, signer)
    return envelope
