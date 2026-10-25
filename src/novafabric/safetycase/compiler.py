"""Safety-case compiler (ADR-0095, slice C1).

``SafetyCaseCompiler.build(capsule_dir, template_id)`` runs a deterministic pipeline:

1. **Template.** Load the CAE skeleton.
2. **Resolve artifacts.** For each leaf claim, locate a matching capsule artifact
   (replay attestation, eval/judge aggregate, completeness assertion, criterion
   bindings, seal) and bind it by sha256. A leaf with no resolvable artifact becomes
   an ``UNSUPPORTED`` claim — an honest hole, never a silent omission.
3. **Assign backing_state.** Mechanically, from evidence statistics, reusing
   ``judge/_kappa.py``, ``eval/significance.py`` (Wilson/SPRT), and the replay
   ``match`` verdict. No operator input enters this stage.
4. **Assemble.** Build the ``Claim -> ArgumentStrategy -> Claim*|Evidence*`` tree,
   propagating the weakest child state upward.
5. **Residual risk.** Default ``not-quantified`` / ``null``; never reproduce the
   literature's disavowed toy numbers as measurements.
6. **case_hash (+ optional DSSE-sign).** Canonicalise, hash, optionally wrap in an
   in-toto Statement and DSSE-sign with the existing NovaSeal/LocalSigner path.

**Honesty is structural.** A claim with no evidence and no UNSUPPORTED flag is
impossible: an unresolved leaf is forced to UNSUPPORTED, and an eval leaf without a
``process_evidence_ref`` cannot be constructed (invariant I1, enforced in the model
and the JSON schema).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from novafabric._capsule_fs import read_capsule_file
from novafabric.capsule.ulid_util import is_valid_ulid, new_ulid
from novafabric.eval.significance import wilson_interval
from novafabric.evidence.intoto import dsse_sign, make_intoto_statement
from novafabric.safetycase.models import (
    SAFETY_CASE_SCHEMA_VERSION,
    ArgumentStrategy,
    ArtifactRef,
    BackingState,
    Claim,
    ClaimKind,
    Confidence,
    ConfidenceMethod,
    EvalContext,
    Evidence,
    EvidenceKind,
    InferenceType,
    Node,
    ProducerInfo,
    ResidualRisk,
    ResidualRiskBasis,
    SafetyCase,
    Subject,
    SubjectKind,
)
from novafabric.safetycase.templates import (
    ClaimSpec,
    StrategySpec,
    Template,
    load_template,
)

SAFETY_CASE_PREDICATE_TYPE = "https://novafabric.io/safety-case/v0"

#: default pass threshold a Wilson interval is checked against; a CI straddling it
#: (lo < threshold < hi) forces CONTESTED (ADR-0095 backing-state rules).
DEFAULT_PASS_THRESHOLD = 0.7

#: inter-judge agreement below this forces the parent claim to CONTESTED.
KAPPA_CONTESTED_THRESHOLD = 0.6


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


#: consensus verdicts that say the eval failed (first word, case-insensitive)
_FAILING_VERDICTS = frozenset({"fail", "failed", "failing", "reject", "rejected", "unsafe"})


def _is_failing_verdict(verdict: object) -> bool:
    words = str(verdict or "").strip().lower().replace(":", " ").split()
    return bool(words) and words[0] in _FAILING_VERDICTS


def _subject_digests(statement: dict[str, Any]) -> set[str]:
    subjects = statement.get("subject")
    if not isinstance(subjects, list):
        return set()
    return {
        str(s["digest"]["sha256"]).removeprefix("sha256:")
        for s in subjects
        if isinstance(s, dict) and isinstance(s.get("digest"), dict) and "sha256" in s["digest"]
    }


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


class NotACapsuleError(ValueError):
    """The directory handed to the compiler is not a capsule with a run id.

    Raised instead of attributing a signed case to a run that does not exist.
    """


#: Weakest first — the order :meth:`SafetyCaseCompiler._propagate` uses.
_STATE_STRENGTH = {
    BackingState.UNSUPPORTED: 0,
    BackingState.CONTESTED: 1,
    BackingState.UNKNOWN: 2,
    BackingState.SUPPORTED: 3,
}


class _Resolved:
    """A leaf resolution: the evidence node, the parent claim state, an optional
    contest reason, and a possible strategy inference-type override."""

    def __init__(
        self,
        evidence: Evidence | None,
        state: BackingState,
        contest_reason: str | None = None,
        confidence: Confidence | None = None,
        inference_override: InferenceType | None = None,
    ) -> None:
        self.evidence = evidence
        self.state = state
        self.contest_reason = contest_reason
        self.confidence = confidence or Confidence()
        self.inference_override = inference_override


class SafetyCaseCompiler:
    """Assemble a safety case from a capsule directory and a template."""

    def __init__(
        self,
        producer_version: str | None = None,
        *,
        replay_public_keys: Sequence[bytes] = (),
    ) -> None:
        """*replay_public_keys* are the PEM ed25519 keys a re-performance attestation
        must be signed by to count (review misc-1-02); none = no replay is verified."""
        if producer_version is None:
            from novafabric import __version__  # noqa: PLC0415

            producer_version = __version__
        self._producer_version = producer_version
        self._replay_public_keys = tuple(replay_public_keys)
        self._evidence_counter = 0

    # -- public API ----------------------------------------------------------

    def build(
        self,
        capsule_dir: Path,
        template_id: str,
        signer: Any | None = None,
    ) -> SafetyCase:
        """Compile a safety case from *capsule_dir* against *template_id*.

        Always returns a hashed :class:`SafetyCase`. DSSE-signing is a separate
        step (:meth:`sign`); the *signer* argument is accepted for API symmetry
        and, when given, the caller may pass the returned case to :meth:`sign`.
        """
        _ = signer  # signing is an explicit follow-up step via .sign()
        template = load_template(template_id)
        run_id = self._run_id(capsule_dir)
        capsule_hash = self._capsule_hash(capsule_dir)

        nodes: list[Node] = []
        self._evidence_counter = 0
        root_state = self._compile_claim(
            template.root, capsule_dir, nodes
        )

        residual = self._residual_risk(template, root_state)

        case = SafetyCase(
            schema_version=SAFETY_CASE_SCHEMA_VERSION,
            case_id=new_ulid(),
            created_at=datetime.now(timezone.utc).isoformat(),
            created_by=ProducerInfo(name="novafabric", version=self._producer_version),
            template_id=template_id,
            subject=Subject(
                kind=SubjectKind.RUN_CAPSULE,
                run_ids=[run_id],
                capsule_hash=capsule_hash,
            ),
            top_claim_id=template.root.id,
            nodes=nodes,
            residual_risk=residual,
        )
        return case.with_case_hash()

    def sign(self, case: SafetyCase, signer: Any) -> dict[str, Any]:
        """Wrap a compiled case as an in-toto Statement and DSSE-sign it.

        The ``case_hash`` is the in-toto subject digest (ADR-0095 §6).
        """
        statement = make_intoto_statement(
            predicate_type=SAFETY_CASE_PREDICATE_TYPE,
            subject_name=f"safety-case/{case.case_id}",
            subject_sha256=case.case_hash,
            predicate=case.to_record(),
        )
        envelope: dict[str, Any] = dsse_sign(statement, signer)
        return envelope

    # -- subject helpers -----------------------------------------------------

    def _run_id(self, capsule_dir: Path) -> str:
        """The capsule's own ``run_id`` — ``capsule.yaml``'s, else a ULID dir name.

        Review misc-1-05: a freshly minted ULID for any other directory made the
        signed case claim a run that never existed, and a different one per rebuild.
        """
        if not capsule_dir.is_dir():
            raise NotACapsuleError(f"{capsule_dir} is not a directory")
        manifest = capsule_dir / "capsule.yaml"
        if manifest.exists():
            try:
                data = yaml.safe_load(
                    read_capsule_file(capsule_dir, "capsule.yaml", operation="safety case")
                )
            except yaml.YAMLError as exc:
                raise NotACapsuleError(f"{manifest} is not valid YAML") from exc
            run_id = data.get("run_id") if isinstance(data, dict) else None
            if isinstance(run_id, str) and is_valid_ulid(run_id):
                return run_id
            raise NotACapsuleError(f"{manifest} carries no valid ULID run_id")
        if is_valid_ulid(capsule_dir.name):
            return capsule_dir.name
        raise NotACapsuleError(
            f"{capsule_dir} has no capsule.yaml and its name is not a run id; "
            "not a capsule"
        )

    def _capsule_hash(self, capsule_dir: Path) -> str:
        manifest = capsule_dir / "capsule.yaml"
        raw = (
            read_capsule_file(capsule_dir, "capsule.yaml", operation="safety case")
            if manifest.exists()
            else b""
        )
        return "sha256:" + hashlib.sha256(raw).hexdigest()

    # -- tree assembly -------------------------------------------------------

    def _compile_claim(
        self,
        spec: ClaimSpec,
        capsule_dir: Path,
        nodes: list[Node],
    ) -> BackingState:
        """Compile one claim spec into nodes; return the claim's backing state."""
        if spec.is_leaf:
            return self._compile_leaf(spec, capsule_dir, nodes)
        return self._compile_internal(spec, capsule_dir, nodes)

    def _compile_leaf(
        self,
        spec: ClaimSpec,
        capsule_dir: Path,
        nodes: list[Node],
    ) -> BackingState:
        resolved = self._resolve_leaf(spec, capsule_dir)
        evidence_ids: list[str] = []
        children: list[str] = []

        if resolved.evidence is not None:
            nodes.append(resolved.evidence)
            evidence_ids.append(resolved.evidence.id)

        if spec.strategy is not None:
            arg = self._strategy_node(spec.strategy, resolved.inference_override)
            nodes.append(arg)
            children.append(arg.id)

        claim = Claim(
            id=spec.id,
            statement=spec.statement,
            claim_kind=ClaimKind(spec.claim_kind.value),
            backing_state=resolved.state,
            children=children,
            evidence_ids=evidence_ids,
            confidence=resolved.confidence,
            contest_reason=resolved.contest_reason
            if resolved.state is BackingState.CONTESTED
            else None,
        )
        nodes.append(claim)
        return resolved.state

    def _compile_internal(
        self,
        spec: ClaimSpec,
        capsule_dir: Path,
        nodes: list[Node],
    ) -> BackingState:
        child_states: list[BackingState] = []
        children: list[str] = []

        if spec.strategy is not None:
            arg = self._strategy_node(spec.strategy, None)
            nodes.append(arg)
            children.append(arg.id)

        for child_spec in spec.children:
            child_states.append(self._compile_claim(child_spec, capsule_dir, nodes))

        state = self._propagate(child_states)
        contest = (
            self._propagation_reason(spec, child_states)
            if state is BackingState.CONTESTED
            else None
        )
        claim = Claim(
            id=spec.id,
            statement=spec.statement,
            claim_kind=ClaimKind(spec.claim_kind.value),
            backing_state=state,
            children=children,
            evidence_ids=[],
            confidence=Confidence(),
            contest_reason=contest,
        )
        nodes.append(claim)
        return state

    def _strategy_node(
        self, spec: StrategySpec, override: InferenceType | None
    ) -> ArgumentStrategy:
        return ArgumentStrategy(
            id=spec.id,
            strategy=spec.strategy,
            inference_type=override or InferenceType(spec.inference_type.value),
            premises=list(spec.premises),
            defeaters=list(spec.defeaters),
        )

    @staticmethod
    def _propagate(child_states: list[BackingState]) -> BackingState:
        """Weakest child state wins (UNSUPPORTED < CONTESTED < UNKNOWN < SUPPORTED)."""
        if not child_states:
            return BackingState.UNSUPPORTED
        return min(child_states, key=lambda s: _STATE_STRENGTH[s])

    @staticmethod
    def _propagation_reason(
        spec: ClaimSpec, child_states: list[BackingState]
    ) -> str:
        n_contested = sum(1 for s in child_states if s is BackingState.CONTESTED)
        n_unsupported = sum(1 for s in child_states if s is BackingState.UNSUPPORTED)
        parts = []
        if n_unsupported:
            parts.append(f"{n_unsupported} sub-claim(s) UNSUPPORTED")
        if n_contested:
            parts.append(f"{n_contested} sub-claim(s) CONTESTED")
        detail = "; ".join(parts) or "a sub-claim is not fully supported"
        return f"Propagated from children: {detail}."

    # -- leaf resolution + backing-state rules -------------------------------

    def _next_evidence_id(self, kind: str) -> str:
        self._evidence_counter += 1
        return f"E-{kind}-{self._evidence_counter}"

    def _resolve_leaf(self, spec: ClaimSpec, capsule_dir: Path) -> _Resolved:
        accepts = spec.binding_spec.accepts if spec.binding_spec else []
        for kind in accepts:
            resolver = self._RESOLVERS.get(kind)
            if resolver is None:
                continue
            resolved = resolver(self, capsule_dir)
            if resolved is not None:
                return resolved
        # honest hole: no resolvable artifact
        return _Resolved(evidence=None, state=BackingState.UNSUPPORTED)

    # each resolver returns a _Resolved or None (artifact absent)

    def _resolve_replay(self, capsule_dir: Path) -> _Resolved | None:
        """A re-performance attestation, counted only when signed by a trusted key.

        Review misc-1-02: the DSSE envelope was decoded and never verified, so a
        hand-written ``{"match": "exact"}`` made the record-integrity claim
        SUPPORTED and deductive. Now the signature must verify under one of the
        compiler's ``replay_public_keys`` (UNKNOWN otherwise — unsigned or
        unverifiable is not evidence either way), and the statement's subject must
        be this capsule's current ``capsule.yaml`` (CONTESTED otherwise).
        """
        path = capsule_dir / "attestations" / "reperformance.json"
        data = _load_json(path)
        if data is None:
            return None
        ev = Evidence(
            id=self._next_evidence_id("replay"),
            evidence_kind=EvidenceKind.REPLAY_ATTESTATION,
            artifact_ref=ArtifactRef(
                kind="reperformance",
                path_in_bundle=str(path.relative_to(capsule_dir)),
                sha256=_sha256_file(path),
            ),
            attestation_ref=str(path.relative_to(capsule_dir)),
        )
        statement = self._verified_statement(data)
        if statement is None:
            return _Resolved(ev, BackingState.UNKNOWN)
        current = self._capsule_hash(capsule_dir).removeprefix("sha256:")
        if current not in _subject_digests(statement):
            return _Resolved(
                ev,
                BackingState.CONTESTED,
                contest_reason=(
                    "The replay attestation is for another capsule state: its subject "
                    "digest is not this capsule's capsule.yaml."
                ),
            )
        predicate = statement.get("predicate")
        match = str(predicate.get("match", "")) if isinstance(predicate, dict) else ""
        if match == "exact":
            return _Resolved(ev, BackingState.SUPPORTED, inference_override=InferenceType.DEDUCTIVE)
        if match == "semantic-match":
            return _Resolved(ev, BackingState.SUPPORTED, inference_override=InferenceType.INDUCTIVE)
        # mismatch (or unknown) -> CONTESTED
        return _Resolved(
            ev,
            BackingState.CONTESTED,
            contest_reason=f"Replay match == {match!r}; re-performance diverged.",
        )

    def _verified_statement(self, envelope: dict[str, Any]) -> dict[str, Any] | None:
        """The in-toto statement of *envelope* if a trusted key signed it, else None."""
        if not ("payload" in envelope and "payloadType" in envelope):
            return None  # not a signed envelope at all
        from novafabric.evidence.intoto import dsse_verify  # noqa: PLC0415
        from novafabric.evidence.signing import verify_with_pem  # noqa: PLC0415

        for key in self._replay_public_keys:
            try:
                statement = dsse_verify(
                    envelope, lambda pae, sig, key=key: verify_with_pem(key, pae, sig)
                )
            except (ValueError, TypeError, KeyError, IndexError):
                continue
            if isinstance(statement, dict):
                return statement
        return None

    def _resolve_eval(self, capsule_dir: Path) -> _Resolved | None:
        eval_dir = capsule_dir / "eval-results"
        if not eval_dir.is_dir():
            return None
        # Review misc-1-05: every eval file counts and the weakest state wins — the
        # same rule the tree uses — so file names cannot choose the verdict. Each
        # candidate is resolved from the same evidence counter; the chosen one keeps
        # its id and the counter advances once.
        start = self._evidence_counter
        resolutions: list[_Resolved] = []
        for path in sorted(eval_dir.glob("*.json")):
            data = _load_json(path)
            if data is None:
                continue
            self._evidence_counter = start
            resolutions.append(self._eval_resolution(capsule_dir, path, data))
        self._evidence_counter = start + 1 if resolutions else start
        if not resolutions:
            return None
        return min(resolutions, key=lambda r: _STATE_STRENGTH[r.state])

    def _eval_resolution(
        self, capsule_dir: Path, path: Path, data: dict[str, Any]
    ) -> _Resolved:
        judges = [str(j) for j in data.get("judges", [])]
        kappa = data.get("inter_judge_agreement")
        kappa_f = float(kappa) if isinstance(kappa, (int, float)) else None
        sample_size = data.get("sample_size")
        successes = data.get("successes")
        process_ref = str(data.get("process_evidence_ref") or "")
        if not process_ref:
            # honest fallback: point at the eval file itself as the process ref so
            # the I1 invariant holds; without process evidence there is no claim.
            process_ref = str(path.relative_to(capsule_dir))

        # confidence interval: prefer a recomputed Wilson interval, else stored.
        interval = self._wilson(successes, sample_size, data.get("confidence_interval"))

        ev = Evidence(
            id=self._next_evidence_id("eval"),
            evidence_kind=EvidenceKind.JUDGE_AGGREGATE,
            artifact_ref=ArtifactRef(
                kind="eval_result",
                path_in_bundle=str(path.relative_to(capsule_dir)),
                sha256=_sha256_file(path),
            ),
            eval_context=EvalContext(
                judges=judges,
                inter_judge_agreement=kappa_f,
                consensus_verdict=str(data.get("consensus_verdict", "unknown")),
                sample_size=int(sample_size) if isinstance(sample_size, int) else None,
                confidence_interval=interval,
                process_evidence_ref=process_ref,
            ),
            attestation_ref=None,
        )

        confidence = Confidence(
            interval=interval, method=ConfidenceMethod.WILSON if interval else ConfidenceMethod.NONE
        )

        # Rule 1: low inter-judge agreement forces CONTESTED.
        if kappa_f is not None and kappa_f < KAPPA_CONTESTED_THRESHOLD:
            return _Resolved(
                ev,
                BackingState.CONTESTED,
                contest_reason=(
                    f"Inter-judge agreement κ={kappa_f:.2f} "
                    f"(< {KAPPA_CONTESTED_THRESHOLD}); consensus verdict not trustworthy."
                ),
                confidence=confidence,
            )
        # Review misc-1-01: the verdict and the whole interval decide, not only a
        # straddle — a CI wholly below the threshold (10/100) was SUPPORTED.
        # Rule 2: a failing consensus verdict is evidence against the claim.
        if _is_failing_verdict(data.get("consensus_verdict")):
            return _Resolved(ev, BackingState.UNSUPPORTED, confidence=confidence)
        # Rule 3: no interval, no statistical basis either way.
        if interval is None:
            return _Resolved(ev, BackingState.UNKNOWN, confidence=confidence)
        # Rule 4: SUPPORTED only when the whole interval clears the threshold.
        if interval[0] >= DEFAULT_PASS_THRESHOLD:
            return _Resolved(ev, BackingState.SUPPORTED, confidence=confidence)
        if interval[1] < DEFAULT_PASS_THRESHOLD:
            return _Resolved(ev, BackingState.UNSUPPORTED, confidence=confidence)
        # Rule 5: a Wilson CI straddling (or touching) the threshold forces CONTESTED.
        return _Resolved(
            ev,
            BackingState.CONTESTED,
            contest_reason=(
                f"Wilson CI [{interval[0]:.2f}, {interval[1]:.2f}] straddles the "
                f"pass threshold {DEFAULT_PASS_THRESHOLD}; statistically "
                "indistinguishable from failing."
            ),
            confidence=confidence,
        )

    @staticmethod
    def _wilson(
        successes: Any, sample_size: Any, stored: Any
    ) -> tuple[float, float] | None:
        if isinstance(successes, int) and isinstance(sample_size, int) and sample_size > 0:
            if 0 <= successes <= sample_size:
                lo, hi = wilson_interval(successes, sample_size)
                return (lo, hi)
        if (
            isinstance(stored, list)
            and len(stored) == 2
            and all(isinstance(x, (int, float)) for x in stored)
        ):
            return (float(stored[0]), float(stored[1]))
        return None

    def _resolve_completeness(self, capsule_dir: Path) -> _Resolved | None:
        path = capsule_dir / "completeness.json"
        data = _load_json(path)
        if data is None:
            return None
        ev = Evidence(
            id=self._next_evidence_id("completeness"),
            evidence_kind=EvidenceKind.COMPLETENESS_ASSERTION,
            artifact_ref=ArtifactRef(
                kind="completeness_assertion",
                path_in_bundle=str(path.relative_to(capsule_dir)),
                sha256=_sha256_file(path),
            ),
            attestation_ref=None,
        )
        # Review misc-1-02: read the assertion, not just its existence.
        from novafabric.evidence.completeness import CompletenessAssertion  # noqa: PLC0415

        try:
            assertion = CompletenessAssertion.model_validate(data)
        except ValueError:
            return _Resolved(ev, BackingState.UNKNOWN)
        dropped = {k: v for k, v in assertion.dropped_counts.items() if v > 0}
        if dropped:
            detail = ", ".join(f"{k}: {v}" for k, v in sorted(dropped.items()))
            return _Resolved(
                ev,
                BackingState.CONTESTED,
                contest_reason=(
                    f"The capture plane dropped events ({detail}); the log is incomplete."
                ),
            )
        return _Resolved(ev, BackingState.SUPPORTED)

    def _resolve_criterion_binding(self, capsule_dir: Path) -> _Resolved | None:
        path = capsule_dir / "criterion-bindings.json"
        data = _load_json(path)
        if data is None:
            return None
        bindings = data.get("bindings", [])
        statuses = [str(b.get("binding_status", "")) for b in bindings if isinstance(b, dict)]
        ev = Evidence(
            id=self._next_evidence_id("binding"),
            evidence_kind=EvidenceKind.CRITERION_BINDING,
            artifact_ref=ArtifactRef(
                kind="criterion_binding",
                path_in_bundle=str(path.relative_to(capsule_dir)),
                sha256=_sha256_file(path),
            ),
            attestation_ref=None,
        )
        if statuses and all(s == "satisfied" for s in statuses):
            return _Resolved(ev, BackingState.SUPPORTED)
        if any(s == "satisfied" or s == "partial" for s in statuses):
            return _Resolved(
                ev,
                BackingState.CONTESTED,
                contest_reason="Criterion bindings only partially satisfied.",
            )
        return _Resolved(ev, BackingState.UNSUPPORTED)

    def _resolve_seal(self, capsule_dir: Path) -> _Resolved | None:
        """The capsule's NovaSeal seal, verified (review misc-1-02).

        Any ``seal-bundle.json``/``seal.json`` that parsed used to be a SUPPORTED seal,
        while the real seal — ``.seal/``, which nothing here looked at — never was.
        """
        from novafabric.trust.novaseal.capsule_verify import (  # noqa: PLC0415
            capsule_seal_verifies,
        )

        path = capsule_dir / ".seal" / "manifest.dsse"
        if not path.is_file():
            return None
        ev = Evidence(
            id=self._next_evidence_id("seal"),
            evidence_kind=EvidenceKind.SEAL,
            artifact_ref=ArtifactRef(
                kind="seal",
                path_in_bundle=".seal/manifest.dsse",
                sha256=_sha256_file(path),
            ),
            attestation_ref=None,
        )
        if capsule_seal_verifies(capsule_dir):
            return _Resolved(ev, BackingState.SUPPORTED)
        return _Resolved(
            ev,
            BackingState.CONTESTED,
            contest_reason="The capsule's seal does not verify: it was altered after sealing.",
        )

    #: evidence_kind -> resolver (unbound; called as resolver(self, capsule_dir))
    _RESOLVERS: dict[str, Callable[["SafetyCaseCompiler", Path], "_Resolved | None"]] = {
        "replay_attestation": _resolve_replay,
        "judge_aggregate": _resolve_eval,
        "eval_result": _resolve_eval,
        "completeness_assertion": _resolve_completeness,
        "criterion_binding": _resolve_criterion_binding,
        "seal": _resolve_seal,
    }

    # -- residual risk -------------------------------------------------------

    def _residual_risk(
        self, template: Template, root_state: BackingState
    ) -> ResidualRisk:
        spec = template.residual_risk
        caveat = spec.caveat if spec is not None else ""
        basis = spec.basis if spec is not None else ResidualRiskBasis.NOT_QUANTIFIED
        if root_state is not BackingState.SUPPORTED:
            extra = f" Top claim backing_state is {root_state.value}."
            caveat = (caveat + extra).strip()
        # value is ALWAYS null here: this compiler never invents a measured number.
        return ResidualRisk(value=None, basis=basis, caveat=caveat)
