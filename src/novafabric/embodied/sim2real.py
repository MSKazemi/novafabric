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

"""Sim-to-real lineage — ADR-0162 P3 (NF-304).

``facets.embodied.sim2real`` binds a real-world deployment run to the
simulation it came from: the sim-trained policy checkpoint
(``sim_policy_ref``), the simulation / world-model configuration
(``sim_env_ref``), the domain-randomization spec when one was declared
(``randomization_ref``), and the real ``deployment_run_id`` — so the
sim-to-real reality gap has an auditable origin trail. Digests only; the
checkpoint weights, the scene assets and the randomization tables stay
wherever the operator holds them (I-2).

**Unresolvable is recorded, not fatal** (ADR-0162 D3). A policy ref that is
absent, or that the resolver cannot produce, or that names bytes whose digest
differs, is recorded as ``unbound: true`` — never dropped, never an exception
that costs a physical deployment its capsule (I-3). The model enforces
"no ``sim_policy_ref`` ⇒ ``unbound``" on every construction path, so a record
cannot claim a binding it does not even name.

**Recording and verifying are separate** (as for the NF-310 trajectory).
:func:`verify_sim2real` is a pure, offline walk that reports findings; its
results are never stored in the facet. It is fail-closed where the evidence
is *contradicted* — a local artifact whose digest differs from the recorded
ref, or a ``deployment_run_id`` naming a different capsule — and merely
warns where the evidence is *absent* (``unbound``), because absence is what
the spec says to record.
"""

from __future__ import annotations

import hashlib
import re
import stat
from collections.abc import Callable, Collection, Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from novafabric.embodied._boundary import _own_fields, _validate_ref, reject_raw_payloads

#: A deployment run id is an identifier (``run:01HX…``, a ULID, a UUID) —
#: never prose. Applied with ``fullmatch``: no whitespace, no newline.
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}")
MAX_RUN_ID_LEN = 128

#: Upper bound on a local artifact :func:`digest_artifact_file` will hash.
#: Generous — a sim-trained policy checkpoint can be tens of GiB — but finite,
#: so a verifier pointed at the wrong file cannot turn into an unbounded job.
MAX_ARTIFACT_BYTES = 64 * 1024**3
_CHUNK = 1024 * 1024

#: The three digest fields :func:`verify_sim2real` can re-check locally.
ArtifactField = Literal["sim_policy_ref", "sim_env_ref", "randomization_ref"]

Sim2RealCode = Literal[
    "unbound",
    "deployment_mismatch",
    "deployment_unchecked",
    "artifact_mismatch",
    "artifact_resolved",
    "artifact_unreferenced",
    "checkpoint_chain_bound",
]


class InvalidDeploymentRunError(Exception):
    """Raised when ``deployment_run_id`` is not a bounded identifier.

    Not a ``ValueError``, so pydantic cannot fold it into a generic
    validation error (see :class:`novafabric.embodied.RawPayloadRejectedError`).
    """


class ArtifactReadError(Exception):
    """Raised when a local artifact cannot be hashed for re-verification.

    Covers a missing path, a non-regular file (a FIFO or ``/dev/zero`` would
    otherwise be read forever), an unreadable file, and one over
    :data:`MAX_ARTIFACT_BYTES`.
    """


class Sim2RealLineage(BaseModel):
    """The ``facets.embodied.sim2real`` block (NF-304)."""

    model_config = ConfigDict(extra="allow")

    #: ``sha256:`` of the sim-trained policy/checkpoint. Optional: an absent
    #: ref is recorded as ``unbound: true`` rather than refused (D3).
    sim_policy_ref: str | None = None
    #: ``sha256:`` of the simulation / world-model configuration.
    sim_env_ref: str
    #: ``sha256:`` of the domain-randomization spec, when one was declared.
    randomization_ref: str | None = None
    #: The real-world run this policy was deployed in.
    deployment_run_id: str = Field(max_length=MAX_RUN_ID_LEN)
    #: True when no policy ref is recorded, or the recorded one did not
    #: resolve. Recorded, never fatal, never dropped.
    unbound: bool = True

    @field_validator("sim_policy_ref", "sim_env_ref", "randomization_ref", mode="before")
    @classmethod
    def _check_refs(cls, value: Any) -> Any:
        if value is None or isinstance(value, str):
            return _validate_ref(value)
        reject_raw_payloads(value, path="sim2real")
        return value

    @field_validator("deployment_run_id", mode="before")
    @classmethod
    def _check_run_id(cls, value: Any) -> Any:
        if not isinstance(value, str) or not _RUN_ID_RE.fullmatch(value):
            raise InvalidDeploymentRunError(
                "deployment_run_id must be a run identifier of at most "
                f"{MAX_RUN_ID_LEN} characters ([A-Za-z0-9._:+-], no whitespace); "
                "it names the real-world capsule (ADR-0162 NF-304)"
            )
        return value

    @field_validator("unbound", mode="before")
    @classmethod
    def _strict_bool(cls, value: Any) -> Any:
        # `"false"` / `0` must not be coerced into a claimed binding.
        if not isinstance(value, bool):
            raise InvalidDeploymentRunError(f"unbound must be a JSON boolean, got {value!r}")
        return value

    @model_validator(mode="after")
    def _force_unbound_without_policy(self) -> Sim2RealLineage:
        """No ``sim_policy_ref`` ⇒ ``unbound``, on every construction path."""
        if self.sim_policy_ref is None:
            self.unbound = True
        reject_raw_payloads(_own_fields(self))
        return self


def build_sim2real(
    *,
    sim_env_ref: str,
    deployment_run_id: str,
    sim_policy_ref: str | None = None,
    randomization_ref: str | None = None,
    resolver: Callable[[str], str | bytes | None] | None = None,
) -> Sim2RealLineage:
    """Record a sim→real binding, resolving the policy ref where possible.

    Mirrors :func:`novafabric.embodied.build_actuation`. ``resolver`` is asked
    for the bytes ``sim_policy_ref`` names; the record is ``unbound`` when it
    returns ``None``, raises, or returns bytes with a different digest — all
    three mean the same thing to a reader: the reality gap's origin is not
    verifiably pinned. With a ref but no resolver the caller's claim is left
    ``unbound=False`` (unchecked, not refuted); with no ref it is ``unbound``.

    Fail-open on *resolution* (I-3); shape errors in the arguments are caller
    bugs and raise.

    Raises:
        InvalidReferenceError: if a ref is not a ``sha256:`` digest.
        InvalidDeploymentRunError: if ``deployment_run_id`` is malformed.
        RawPayloadRejectedError: if any argument carries raw bytes.
    """
    unbound = True
    if sim_policy_ref is not None:
        unbound = False
        if resolver is not None:
            try:
                artifact = resolver(sim_policy_ref)
            except Exception:  # noqa: BLE001 - a failing lookup is "unresolved", never fatal
                artifact = None
            unbound = artifact is None or not _matches(sim_policy_ref, artifact)
    return Sim2RealLineage(
        sim_policy_ref=sim_policy_ref,
        sim_env_ref=sim_env_ref,
        randomization_ref=randomization_ref,
        deployment_run_id=deployment_run_id,
        unbound=unbound,
    )


def _matches(ref: str, artifact: str | bytes) -> bool:
    raw = artifact.encode("utf-8") if isinstance(artifact, str) else artifact
    return ref == f"sha256:{hashlib.sha256(raw).hexdigest()}"


def digest_artifact_file(path: Path, *, max_bytes: int = MAX_ARTIFACT_BYTES) -> str:
    """Stream-hash a local artifact file into a ``sha256:`` digest.

    Regular files only, bounded by ``max_bytes`` (checked before reading and
    enforced while reading, so a file that grows mid-hash is still refused).

    Raises:
        ArtifactReadError: naming the path and the reason.
    """
    try:
        info = path.stat()
    except OSError as exc:
        raise ArtifactReadError(f"cannot stat {path}: {exc.strerror or exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ArtifactReadError(f"{path} is not a regular file")
    if info.st_size > max_bytes:
        raise ArtifactReadError(f"{path} is {info.st_size} bytes, over the {max_bytes}-byte cap")
    hasher = hashlib.sha256()
    read = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK):
                read += len(chunk)
                if read > max_bytes:
                    raise ArtifactReadError(f"{path} grew past the {max_bytes}-byte cap")
                hasher.update(chunk)
    except OSError as exc:
        raise ArtifactReadError(f"cannot read {path}: {exc.strerror or exc}") from exc
    return f"sha256:{hasher.hexdigest()}"


# ── Offline verification ──────────────────────────────────────────────────


class Sim2RealFinding(BaseModel):
    """One observation from :func:`verify_sim2real`."""

    model_config = ConfigDict(frozen=True)

    code: Sim2RealCode
    #: ``error`` fails verification; ``warning``/``info`` never do.
    severity: Literal["error", "warning", "info"]
    message: str


class Sim2RealReport(BaseModel):
    """The result of walking a sim→real binding offline."""

    model_config = ConfigDict(frozen=True)

    #: As recorded — :func:`verify_sim2real` never rewrites the record.
    unbound: bool
    #: ``True``/``False`` when the capsule's own run id was available to
    #: compare against; ``None`` when it was not.
    deployment_matches: bool | None
    #: ``sim_policy_ref`` appears in this capsule's NF-202 checkpoint chain.
    checkpoint_chain_bound: bool
    #: Field → whether a supplied local artifact matched its recorded digest.
    resolved: dict[str, bool] = Field(default_factory=dict)
    findings: list[Sim2RealFinding] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when no finding has ``error`` severity."""
        return not any(f.severity == "error" for f in self.findings)


def _bare_run_id(value: str) -> str:
    return value.removeprefix("run:")


def verify_sim2real(
    lineage: Sim2RealLineage,
    *,
    capsule_run_id: str | None = None,
    checkpoint_digests: Collection[str] = (),
    artifact_digests: Mapping[ArtifactField, str] | None = None,
) -> Sim2RealReport:
    """Walk a sim→real binding offline and report what holds.

    Pure: every input is passed in. ``capsule_run_id`` is the run id of the
    capsule the facet sits in — the real deployment, so ``deployment_run_id``
    must name it (a ``run:`` prefix on either side is ignored).
    ``checkpoint_digests`` are the NF-202 ``checkpoint_digest`` values of this
    capsule's ``facets.model_provenance.checkpoint_chain``, where present.
    ``artifact_digests`` maps a ref field to the digest of a local file the
    caller hashed (:func:`digest_artifact_file`).

    Fail-closed on contradiction (``artifact_mismatch``,
    ``deployment_mismatch``); an ``unbound`` policy is a warning, recorded
    not fatal (ADR-0162 D3).
    """
    findings: list[Sim2RealFinding] = []
    if lineage.unbound:
        findings.append(
            Sim2RealFinding(
                code="unbound",
                severity="warning",
                message=(
                    "sim_policy_ref is absent or did not resolve at capture — the "
                    "deployed policy's simulation origin is recorded, not pinned"
                ),
            )
        )

    deployment_matches: bool | None = None
    if capsule_run_id is None:
        findings.append(
            Sim2RealFinding(
                code="deployment_unchecked",
                severity="warning",
                message="capsule has no run_id; deployment_run_id could not be compared",
            )
        )
    else:
        deployment_matches = _bare_run_id(lineage.deployment_run_id) == _bare_run_id(capsule_run_id)
        if not deployment_matches:
            findings.append(
                Sim2RealFinding(
                    code="deployment_mismatch",
                    severity="error",
                    message=(
                        f"deployment_run_id {lineage.deployment_run_id!r} does not name "
                        f"this capsule (run_id {capsule_run_id!r})"
                    ),
                )
            )

    chain_bound = lineage.sim_policy_ref is not None and lineage.sim_policy_ref in set(
        checkpoint_digests
    )
    if chain_bound:
        findings.append(
            Sim2RealFinding(
                code="checkpoint_chain_bound",
                severity="info",
                message="sim_policy_ref is a checkpoint in this capsule's NF-202 chain",
            )
        )

    resolved: dict[str, bool] = {}
    for field in sorted(artifact_digests or {}):
        findings.append(_artifact_finding(lineage, field, (artifact_digests or {})[field]))
        recorded = getattr(lineage, field)
        if recorded is not None:
            resolved[field] = recorded == (artifact_digests or {})[field]

    return Sim2RealReport(
        unbound=lineage.unbound,
        deployment_matches=deployment_matches,
        checkpoint_chain_bound=chain_bound,
        resolved=resolved,
        findings=findings,
    )


def _artifact_finding(lineage: Sim2RealLineage, field: str, digest: str) -> Sim2RealFinding:
    recorded = getattr(lineage, field)
    if recorded is None:
        return Sim2RealFinding(
            code="artifact_unreferenced",
            severity="warning",
            message=f"a local artifact was supplied for {field}, but no {field} is recorded",
        )
    if recorded != digest:
        return Sim2RealFinding(
            code="artifact_mismatch",
            severity="error",
            message=(f"{field} records {recorded} but the supplied artifact hashes to {digest}"),
        )
    return Sim2RealFinding(
        code="artifact_resolved",
        severity="info",
        message=f"{field} matches the supplied local artifact",
    )


__all__ = [
    "MAX_ARTIFACT_BYTES",
    "ArtifactReadError",
    "InvalidDeploymentRunError",
    "Sim2RealFinding",
    "Sim2RealLineage",
    "Sim2RealReport",
    "build_sim2real",
    "digest_artifact_file",
    "verify_sim2real",
]
