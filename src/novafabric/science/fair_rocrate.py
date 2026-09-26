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

"""FAIR Workflow-Run-RO-Crate science profile — ADR-0164 D2/P2 (NF-324).

Populates a *science profile* of the NF-040 RO-Crate carrier
(:func:`novafabric.compliance.export.ro_crate.export_ro_crate`) from a capsule's
``facets.science_provenance`` block. It does **not** re-implement RO-Crate
(ADR-0164 Alternative 2): the carrier builds the crate — the file set, the
``File`` entities, the ZIP — and this module *composes over* its output, adding
the Workflow Run Crate entities and re-emitting the archive deterministically.

What the profile adds to the carrier's graph:

- ``conformsTo`` on the root Dataset pins the Workflow Run Crate profile(s)
  read — Process Run Crate 0.5 always, plus Workflow Run Crate 0.5 and Workflow
  RO-Crate 1.0 when a workflow is declared — each also present as a contextual
  ``CreativeWork`` entity carrying its ``version`` (spec §3 item 16).
- One ``CreateAction`` (``#science-run``) — the W3C-PROV ``Activity`` of the
  run, per the Workflow Run Crate PROV mapping — whose ``instrument`` is the
  declared workflow or code, whose ``object`` lists the receipt's digests and
  seeds as ``PropertyValue`` entities, and whose ``result`` lists the
  observation/result/claim nodes of the NF-321 DAG.
- One contextual entity per DAG node, with ``isBasedOn`` edges to its parents
  (``schema:isBasedOn`` ↔ ``prov:wasDerivedFrom``).
- The sealed capsule root (the facet's ``bound_root``) and the receipt's own
  ``bound_root`` as ``identifier`` values, so the crate names the root it binds.

Honest limitations (**experimental**):

- Declared workflow and code are referenced by digest, never included
  (reference-not-bytes, ADR-0125). A strict Workflow RO-Crate validator expects
  ``mainEntity`` to be a data entity inside the crate and will flag
  ``#workflow`` as contextual; the crate says so in the entity's description.
- The Provenance Run Crate profile (per-step ``HowToStep`` actions) is
  **planned**, not emitted: NF-321 DAG nodes are scientific lineage, not workflow
  steps, and mapping one onto the other would fabricate structure.
- Determinism: output is byte-identical across runs *when the capsule carries a
  timestamp* (``finished_at``/``created_at``) — the only clock the crate uses.
"""

from __future__ import annotations

import io
import json
import re
import tempfile
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from novafabric._hashutil import sha256_prefixed
from novafabric.science.provenance import (
    ScienceNode,
    ScienceProvenanceError,
    ScienceProvenanceFacet,
    build_dag,
    facet_from_capsule,
)
from novafabric.science.reproducibility import (
    IN_MISSION_BOUNDARY,
    ReproducibilityReceipt,
    receipt_from_capsule,
    verify_receipt,
)

__all__ = [
    "BINDING_SUFFIX",
    "FAIR_SCHEMA_VERSION",
    "PROCESS_RUN_CRATE",
    "RO_CRATE_CONTEXT",
    "RO_CRATE_SPEC",
    "WORKFLOW_RO_CRATE",
    "WORKFLOW_RUN_CONTEXT",
    "WORKFLOW_RUN_CRATE",
    "WRROC_VERSION",
    "CrateCarrierError",
    "FairBinding",
    "FairRocrateError",
    "InvalidPersistentIdentifierError",
    "PersistentIdentifiers",
    "RocrateProfile",
    "ScienceCrateExport",
    "ScienceMaterialMissingError",
    "TamperedReceiptError",
    "UnsupportedProfileError",
    "build_science_metadata",
    "export_science_rocrate",
    "profile_uris",
    "select_profile",
]

FAIR_SCHEMA_VERSION = "0.1.0"

#: Pinned standards (spec §3 item 16: record which version was read).
RO_CRATE_SPEC = "https://w3id.org/ro/crate/1.1"
RO_CRATE_CONTEXT = "https://w3id.org/ro/crate/1.1/context"
WORKFLOW_RUN_CONTEXT = "https://w3id.org/ro/terms/workflow-run/context"
WRROC_VERSION = "0.5"
PROCESS_RUN_CRATE = f"https://w3id.org/ro/wfrun/process/{WRROC_VERSION}"
WORKFLOW_RUN_CRATE = f"https://w3id.org/ro/wfrun/workflow/{WRROC_VERSION}"
WORKFLOW_RO_CRATE = "https://w3id.org/workflowhub/workflow-ro-crate/1.0"

_PROFILE_NAMES: dict[str, tuple[str, str]] = {
    PROCESS_RUN_CRATE: ("Process Run Crate", WRROC_VERSION),
    WORKFLOW_RUN_CRATE: ("Workflow Run Crate", WRROC_VERSION),
    WORKFLOW_RO_CRATE: ("Workflow RO-Crate", "1.0"),
}

#: The spec (§3 item 8) names ``workflow-run-crate | provenance-run-crate``.
#: ``process-run-crate`` is the honest profile for a run with no declared
#: workflow; ``provenance-run-crate`` is **planned** (see module docstring).
RocrateProfile = Literal["process-run-crate", "workflow-run-crate"]

#: Suffix of the binding record written next to the crate: ``<run_id>.fair-binding.json``.
BINDING_SUFFIX = ".fair-binding.json"
_METADATA_NAME = "ro-crate-metadata.json"

#: Fixed ZIP member timestamp (the ZIP epoch) — part of byte-determinism.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

#: DAG node kinds that are outputs of the run (``CreateAction.result``).
_RESULT_KINDS = frozenset({"observation", "result", "claim"})

_ACTION_STATUS = {
    "success": "http://schema.org/CompletedActionStatus",
    "completed": "http://schema.org/CompletedActionStatus",
    "failed": "http://schema.org/FailedActionStatus",
    "error": "http://schema.org/FailedActionStatus",
}

_DOI_RE = re.compile(r"^10\.[0-9]{4,9}/[^\s]{1,256}\Z")
_ORCID_RE = re.compile(r"^[0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9]{3}[0-9X]\Z")
_ROR_RE = re.compile(r"^0[a-z0-9]{6}[0-9]{2}\Z")
_MAX_PIDS = 256


# ── Errors ────────────────────────────────────────────────────────────────


class FairRocrateError(ScienceProvenanceError):
    """Base class for every science RO-Crate export error."""


class ScienceMaterialMissingError(FairRocrateError):
    """The capsule carries no science facet — use ``nova export-rocrate`` instead."""


class UnsupportedProfileError(FairRocrateError):
    """The requested profile cannot be emitted honestly from this capsule."""


class TamperedReceiptError(FairRocrateError):
    """The capsule's reproducibility receipt does not re-derive its ``bound_root``."""


class CrateCarrierError(FairRocrateError):
    """The NF-040 carrier produced an archive this module cannot compose over."""


class InvalidPersistentIdentifierError(FairRocrateError):
    """A DOI / ORCID / ROR reference is malformed."""


# ── Persistent identifiers ────────────────────────────────────────────────


def _orcid_checksum_ok(orcid: str) -> bool:
    """ISO 7064 MOD 11-2 check digit, as ORCID specifies."""
    digits = orcid.replace("-", "")
    total = 0
    for ch in digits[:-1]:
        total = (total + int(ch)) * 2
    check = (12 - total % 11) % 11
    expected = "X" if check == 10 else str(check)
    return digits[-1] == expected


def _normalise_doi(value: str) -> str:
    raw = value.strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if raw.lower().startswith(prefix):
            raw = raw[len(prefix) :]
    if not _DOI_RE.match(raw):
        raise InvalidPersistentIdentifierError(f"not a DOI: {value!r}")
    return f"https://doi.org/{raw}"


def _normalise_orcid(value: str) -> str:
    raw = value.strip()
    for prefix in ("https://orcid.org/", "http://orcid.org/"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
    if not _ORCID_RE.match(raw) or not _orcid_checksum_ok(raw):
        raise InvalidPersistentIdentifierError(f"not a valid ORCID iD: {value!r}")
    return f"https://orcid.org/{raw}"


def _normalise_ror(value: str) -> str:
    raw = value.strip()
    for prefix in ("https://ror.org/", "http://ror.org/"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
    if not _ROR_RE.match(raw):
        raise InvalidPersistentIdentifierError(f"not a ROR id: {value!r}")
    return f"https://ror.org/{raw}"


def _pid_list(value: object, normalise: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise InvalidPersistentIdentifierError(f"{field} must be a list")
    if len(value) > _MAX_PIDS:
        raise InvalidPersistentIdentifierError(f"{field}: more than {_MAX_PIDS} entries")
    return sorted({normalise(str(v)) for v in value})


class PersistentIdentifiers(BaseModel):
    """DOI / ORCID / ROR references, normalised to resolvable URIs, sorted.

    References only (I-2): an ORCID iD is recorded, never a person's name.
    Contributors appear in the crate as ``contributor`` — never ``author``:
    authorship is a human, declared relationship this exporter does not assert
    (ADR-0164 D4 / NF-327).
    """

    model_config = ConfigDict(frozen=True)

    doi: str | None = None
    orcid_refs: list[str] = Field(default_factory=list)
    ror_refs: list[str] = Field(default_factory=list)

    @field_validator("doi", mode="before")
    @classmethod
    def _check_doi(cls, v: object) -> str | None:
        return None if v is None else _normalise_doi(str(v))

    @field_validator("orcid_refs", mode="before")
    @classmethod
    def _check_orcid(cls, v: object) -> list[str]:
        return _pid_list(v, _normalise_orcid, "orcid_refs")

    @field_validator("ror_refs", mode="before")
    @classmethod
    def _check_ror(cls, v: object) -> list[str]:
        return _pid_list(v, _normalise_ror, "ror_refs")


# ── FAIR binding record ───────────────────────────────────────────────────


class FairBinding(BaseModel):
    """The NF-324 ``fair_binding`` record for one emitted science crate.

    Written *next to* the crate (``<run_id>.fair-binding.json``), not inside it:
    ``rocrate_digest`` is the digest of the crate's metadata document, and a
    record cannot carry the digest of a document that contains it.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = FAIR_SCHEMA_VERSION
    rocrate_profile: RocrateProfile
    profile_uris: list[str]
    profile_version: str = WRROC_VERSION
    rocrate_spec: str = RO_CRATE_SPEC
    rocrate_digest: str
    prov_alignment: Literal["w3c-prov"] = "w3c-prov"
    sealed_root: str | None = None
    receipt_root: str | None = None
    doi: str | None = None
    orcid_refs: list[str] = Field(default_factory=list)
    ror_refs: list[str] = Field(default_factory=list)
    #: Bindings that could not be made, named rather than fabricated.
    unbound: list[str] = Field(default_factory=list)
    verdict: None = None


class ScienceCrateExport(BaseModel):
    """Where an export landed, and the binding record it produced."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    crate_path: Path
    binding_path: Path
    binding: FairBinding


# ── Profile selection ─────────────────────────────────────────────────────


def select_profile(
    receipt: ReproducibilityReceipt | None, requested: RocrateProfile | None
) -> RocrateProfile:
    """Pick the profile to emit, refusing one the capsule cannot honestly support.

    Default: Workflow Run Crate when the receipt declares a ``workflow_digest``,
    Process Run Crate otherwise. Requesting ``workflow-run-crate`` for a run with
    no declared workflow raises rather than inventing one.
    """
    has_workflow = receipt is not None and receipt.workflow_digest is not None
    if requested is None:
        return "workflow-run-crate" if has_workflow else "process-run-crate"
    if requested == "workflow-run-crate" and not has_workflow:
        raise UnsupportedProfileError(
            "workflow-run-crate needs a declared workflow_digest in the "
            "reproducibility receipt; none is recorded, and one is never fabricated. "
            "Use process-run-crate."
        )
    return requested


def profile_uris(profile: RocrateProfile) -> list[str]:
    """The ``conformsTo`` URIs a profile pins, in canonical order."""
    if profile == "workflow-run-crate":
        return [PROCESS_RUN_CRATE, WORKFLOW_RUN_CRATE, WORKFLOW_RO_CRATE]
    return [PROCESS_RUN_CRATE]


# ── Graph construction (pure) ─────────────────────────────────────────────


def _node_id(node: ScienceNode) -> str:
    return "#science-node-" + node.node_digest.removeprefix("sha256:")


def _ref(entity_id: str) -> dict[str, str]:
    return {"@id": entity_id}


def _property_values(receipt: ReproducibilityReceipt | None) -> list[dict[str, Any]]:
    if receipt is None:
        return []
    out: list[dict[str, Any]] = []
    for name in ("environment_digest", "data_digest", "code_digest", "workflow_digest"):
        value = getattr(receipt, name)
        if value is not None:
            out.append(
                {"@id": f"#pv-{name}", "@type": "PropertyValue", "name": name, "value": value}
            )
    for i, seed in enumerate(receipt.seeds):
        out.append(
            {"@id": f"#pv-seed-{i}", "@type": "PropertyValue", "name": "seed", "value": seed}
        )
    return out


def _instrument(profile: RocrateProfile, receipt: ReproducibilityReceipt | None) -> dict[str, Any]:
    if profile == "workflow-run-crate" and receipt is not None:
        return {
            "@id": "#workflow",
            "@type": ["SoftwareSourceCode", "ComputationalWorkflow"],
            "name": "Declared workflow (referenced by digest, not included)",
            "identifier": receipt.workflow_digest,
            "description": (
                "Reference-not-bytes (ADR-0125): the workflow definition is bound by "
                "digest only, so this entity is contextual rather than a data entity."
            ),
        }
    if receipt is not None and receipt.code_digest is not None:
        return {
            "@id": "#code",
            "@type": "SoftwareSourceCode",
            "name": "Declared code (referenced by digest, not included)",
            "identifier": receipt.code_digest,
        }
    return {
        "@id": "#instrument-undeclared",
        "@type": "SoftwareApplication",
        "name": "Undeclared instrument (no code_digest recorded)",
    }


def _pid_entities(pids: PersistentIdentifiers) -> list[dict[str, Any]]:
    people = [{"@id": o, "@type": "Person"} for o in pids.orcid_refs]
    orgs = [{"@id": r, "@type": "Organization"} for r in pids.ror_refs]
    return people + orgs


def _sorted_graph(
    descriptor: dict[str, Any], root: dict[str, Any], others: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Descriptor, root, then every other entity by ``@id`` — stable ordering."""
    seen: dict[str, dict[str, Any]] = {}
    for entity in others:
        seen[str(entity["@id"])] = entity
    return [descriptor, root, *(seen[k] for k in sorted(seen))]


def build_science_metadata(
    carrier_metadata: dict[str, Any],
    *,
    facet: ScienceProvenanceFacet | None,
    receipt: ReproducibilityReceipt | None,
    profile: RocrateProfile,
    manifest: dict[str, Any],
    pids: PersistentIdentifiers | None = None,
) -> dict[str, Any]:
    """Extend the NF-040 carrier's metadata document with the science profile.

    Pure: takes the carrier's JSON-LD document, returns a new one. Keeps the
    carrier's ``File`` entities as they are; rewrites only the descriptor and
    root (to pin profiles and bind roots) and adds contextual entities.

    Raises:
        CrateCarrierError: the carrier document lacks a descriptor or root.
        ScienceProvenanceError: the facet's DAG is malformed (via ``build_dag``).
    """
    pids = pids or PersistentIdentifiers()
    graph = carrier_metadata.get("@graph")
    if not isinstance(graph, list):
        raise CrateCarrierError("carrier metadata has no @graph")
    by_id = {str(e.get("@id")): e for e in graph if isinstance(e, dict)}
    if _METADATA_NAME not in by_id or "./" not in by_id:
        raise CrateCarrierError("carrier metadata lacks the descriptor or root dataset")

    run_id = str(by_id["./"].get("identifier") or manifest.get("run_id") or "")
    stamp = manifest.get("finished_at") or manifest.get("created_at")
    uris = profile_uris(profile)

    descriptor = dict(by_id[_METADATA_NAME])
    descriptor["conformsTo"] = [
        _ref(RO_CRATE_SPEC),
        *([_ref(WORKFLOW_RO_CRATE)] if profile == "workflow-run-crate" else []),
    ]

    root = dict(by_id["./"])
    if stamp:
        root["datePublished"] = str(stamp)
        descriptor["dateCreated"] = str(stamp)
    root["conformsTo"] = [_ref(u) for u in uris]
    root["hasPart"] = sorted(root.get("hasPart") or [], key=lambda p: str(p.get("@id")))
    identifiers = [run_id] if run_id else []
    if facet is not None and facet.bound_root:
        identifiers.append(facet.bound_root)
    if pids.doi:
        identifiers.append(pids.doi)
    root["identifier"] = identifiers[0] if len(identifiers) == 1 else identifiers
    root["mentions"] = [_ref("#science-run")]
    if pids.orcid_refs:
        root["contributor"] = [_ref(o) for o in pids.orcid_refs]
    if pids.ror_refs:
        root["sourceOrganization"] = [_ref(r) for r in pids.ror_refs]

    instrument = _instrument(profile, receipt)
    if profile == "workflow-run-crate":
        root["mainEntity"] = _ref(instrument["@id"])

    nodes = build_dag(facet.hypothesis_experiment_result) if facet is not None else []
    node_entities: list[dict[str, Any]] = []
    for node in nodes:
        entity: dict[str, Any] = {
            "@id": _node_id(node),
            "@type": "CreativeWork",
            "name": f"{node.kind} {node.node_id}",
            "identifier": node.node_digest,
            "keywords": node.kind,
        }
        parents = sorted(node.parent_digests)
        if parents:
            entity["isBasedOn"] = [
                _ref("#science-node-" + p.removeprefix("sha256:")) for p in parents
            ]
        node_entities.append(entity)

    values = _property_values(receipt)
    action: dict[str, Any] = {
        "@id": "#science-run",
        "@type": "CreateAction",
        "name": f"Agentic-science run {run_id}".strip(),
        "instrument": _ref(instrument["@id"]),
        "object": [_ref(v["@id"]) for v in values],
        "result": [_ref(_node_id(n)) for n in nodes if n.kind in _RESULT_KINDS],
    }
    if manifest.get("created_at"):
        action["startTime"] = str(manifest["created_at"])
    if manifest.get("finished_at"):
        action["endTime"] = str(manifest["finished_at"])
    status = _ACTION_STATUS.get(str(manifest.get("status", "")).lower())
    if status:
        action["actionStatus"] = _ref(status)

    extra: list[dict[str, Any]] = [
        action,
        instrument,
        *values,
        *node_entities,
        *_pid_entities(pids),
        *(
            {
                "@id": u,
                "@type": "CreativeWork",
                "name": _PROFILE_NAMES[u][0],
                "version": _PROFILE_NAMES[u][1],
            }
            for u in uris
        ),
    ]
    science: dict[str, Any] = {
        "@id": "#science-provenance",
        "@type": "CreativeWork",
        "name": "Science provenance (ADR-0164, NF-321/323/324)",
        "about": _ref("#science-run"),
        "description": IN_MISSION_BOUNDARY,
    }
    if facet is not None and facet.bound_root:
        science["identifier"] = facet.bound_root
    extra.append(science)
    if receipt is not None:
        incomplete = ", ".join(receipt.receipt_incomplete) or "none"
        extra.append(
            {
                "@id": "#reproducibility-receipt",
                "@type": "CreativeWork",
                "name": "Reproducibility receipt (ADR-0164 NF-323)",
                "identifier": receipt.bound_root,
                "about": _ref("#science-run"),
                "description": (
                    f"determinism_class={receipt.determinism_class}; "
                    f"receipt_incomplete={incomplete}. Record-only: binds what would "
                    "have to match to re-run; never re-executed."
                ),
            }
        )

    # Carrier entities (Files) keep their place; ours are added alongside.
    carrier_others = [e for k, e in by_id.items() if k not in (_METADATA_NAME, "./")]
    return {
        "@context": [RO_CRATE_CONTEXT, WORKFLOW_RUN_CONTEXT],
        "@graph": _sorted_graph(descriptor, root, [*carrier_others, *extra]),
    }


# ── Export (IO) ───────────────────────────────────────────────────────────


def _read_manifest(capsule_dir: Path) -> dict[str, Any]:
    path = capsule_dir / "capsule.yaml"
    if not path.is_file():
        raise ScienceMaterialMissingError(f"capsule.yaml not found in {capsule_dir}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CrateCarrierError(f"could not read capsule.yaml: {exc}") from exc
    if not isinstance(data, dict):
        raise CrateCarrierError("capsule.yaml is not a mapping")
    return data


def _canonical_bytes(doc: dict[str, Any]) -> bytes:
    return (json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _write_deterministic_zip(path: Path, members: dict[str, bytes]) -> None:
    """Write members in sorted order with fixed timestamps and permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        ordered = [_METADATA_NAME, *sorted(k for k in members if k != _METADATA_NAME)]
        for name in ordered:
            info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, members[name])
    path.write_bytes(buf.getvalue())


def _carrier_members(capsule_dir: Path) -> dict[str, bytes]:
    """Run the NF-040 carrier and return its archive members by name."""
    from novafabric.compliance.export.ro_crate import export_ro_crate  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix="nova-rocrate-science-") as tmp:
        carrier_zip = export_ro_crate(capsule_dir, Path(tmp) / "carrier.zip")
        with zipfile.ZipFile(carrier_zip) as zf:
            return {name: zf.read(name) for name in zf.namelist()}


def export_science_rocrate(
    capsule_dir: Path,
    out_dir: Path,
    *,
    profile: RocrateProfile | None = None,
    pids: PersistentIdentifiers | None = None,
) -> ScienceCrateExport:
    """Export a capsule as a Workflow-Run-RO-Crate science profile.

    Writes ``<run_id>.science.rocrate.zip`` and ``<run_id>.fair-binding.json`` into
    ``out_dir``. Refuses — rather than degrading silently — when there is no
    science material, when the receipt's root does not re-derive (tampering),
    or when the requested profile would need a workflow that was never declared.

    Raises:
        ScienceMaterialMissingError: no ``science_provenance`` facet.
        TamperedReceiptError: the receipt fails :func:`verify_receipt`.
        UnsupportedProfileError: see :func:`select_profile`.
        CrateCarrierError: the manifest or carrier output is unusable.
        ScienceProvenanceError: the declared DAG is malformed.
    """
    manifest = _read_manifest(capsule_dir)
    facet = facet_from_capsule(manifest)
    receipt = receipt_from_capsule(manifest)
    if facet is None:
        raise ScienceMaterialMissingError(
            "capsule carries no facets.science_provenance block; nothing to map into "
            "a science profile (use `nova export-rocrate` for the plain crate)"
        )
    if receipt is not None and not verify_receipt(receipt).ok:
        raise TamperedReceiptError(
            "reproducibility_receipt does not re-derive its bound_root (or misreports "
            "receipt_incomplete); refusing to bind it into a FAIR crate"
        )
    chosen = select_profile(receipt, profile)
    pids = pids or PersistentIdentifiers()

    members = _carrier_members(capsule_dir)
    if _METADATA_NAME not in members:
        raise CrateCarrierError("carrier archive has no ro-crate-metadata.json")
    try:
        carrier_doc = json.loads(members[_METADATA_NAME])
    except ValueError as exc:
        raise CrateCarrierError(f"carrier metadata is not JSON: {exc}") from exc

    doc = build_science_metadata(
        carrier_doc,
        facet=facet,
        receipt=receipt,
        profile=chosen,
        manifest=manifest,
        pids=pids,
    )
    metadata_bytes = _canonical_bytes(doc)
    members[_METADATA_NAME] = metadata_bytes

    run_id = str(manifest.get("run_id") or capsule_dir.name)
    crate_path = out_dir / f"{run_id}.science.rocrate.zip"
    _write_deterministic_zip(crate_path, members)

    unbound: list[str] = []
    if not facet.bound_root:
        unbound.append("sealed_root")
    if receipt is None:
        unbound.append("reproducibility_receipt")
    binding = FairBinding(
        rocrate_profile=chosen,
        profile_uris=profile_uris(chosen),
        rocrate_digest=sha256_prefixed(metadata_bytes),
        sealed_root=facet.bound_root,
        receipt_root=receipt.bound_root if receipt is not None else None,
        doi=pids.doi,
        orcid_refs=list(pids.orcid_refs),
        ror_refs=list(pids.ror_refs),
        unbound=unbound,
    )
    binding_path = out_dir / f"{run_id}{BINDING_SUFFIX}"
    binding_path.write_bytes(_canonical_bytes(binding.model_dump(mode="json")))
    return ScienceCrateExport(crate_path=crate_path, binding_path=binding_path, binding=binding)
