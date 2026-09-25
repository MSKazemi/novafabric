"""NF-324 FAIR Workflow-Run-RO-Crate science profile (ADR-0164 P2).

Acceptance criteria (NF-321-330 spec §3 item 8, §6 "FAIR (NF-324)"):

- the crate conforms to RO-Crate 1.1 structure and pins the Workflow Run Crate
  profile URIs + version (spec §3 item 16);
- ``prov_alignment: "w3c-prov"`` and the sealed root bound in;
- the NF-040 carrier is reused, not re-implemented (its File entities survive);
- output is byte-deterministic;
- refusal, not silent degradation, on: no science facet, tampered receipt,
  workflow profile without a declared workflow, malformed DAG.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from novafabric.science import digest_node
from novafabric.science.fair_rocrate import (
    PROCESS_RUN_CRATE,
    RO_CRATE_CONTEXT,
    RO_CRATE_SPEC,
    WORKFLOW_RO_CRATE,
    WORKFLOW_RUN_CONTEXT,
    WORKFLOW_RUN_CRATE,
    WRROC_VERSION,
    CrateCarrierError,
    FairBinding,
    InvalidPersistentIdentifierError,
    PersistentIdentifiers,
    ScienceMaterialMissingError,
    TamperedReceiptError,
    UnsupportedProfileError,
    build_science_metadata,
    export_science_rocrate,
    select_profile,
)
from novafabric.science.provenance import (
    FACET_NAME,
    ScienceProvenanceError,
    ScienceProvenanceFacet,
)
from novafabric.science.reproducibility import RECEIPT_KEY, build_receipt

from .conftest import FULL_RECEIPT, RUN_ID, SEALED_ROOT

ORCID = "0000-0002-1825-0097"  # ORCID's documented example iD (valid checksum)
ROR = "https://ror.org/03yrm5c26"
DOI = "10.5281/zenodo.1234567"


def _metadata(crate: Path) -> dict[str, Any]:
    with zipfile.ZipFile(crate) as zf:
        data: dict[str, Any] = json.loads(zf.read("ro-crate-metadata.json"))
    return data


def _by_id(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["@id"]: e for e in doc["@graph"]}


def _ids(value: Any) -> list[str]:
    items = value if isinstance(value, list) else [value]
    return [i["@id"] for i in items if isinstance(i, dict) and "@id" in i]


def assert_ro_crate_11(doc: dict[str, Any], crate: Path) -> None:
    """Structural RO-Crate 1.1 + Workflow Run Crate checks (no validator dependency)."""
    assert RO_CRATE_CONTEXT in doc["@context"]
    ids = [e["@id"] for e in doc["@graph"]]
    assert len(ids) == len(set(ids)), "duplicate @id in graph"
    graph = _by_id(doc)
    desc = graph["ro-crate-metadata.json"]
    assert desc["@type"] == "CreativeWork"
    assert desc["about"] == {"@id": "./"}
    assert RO_CRATE_SPEC in _ids(desc["conformsTo"])
    root = graph["./"]
    assert root["@type"] == "Dataset"
    for key in ("name", "description", "datePublished"):
        assert root.get(key), f"root Dataset missing {key}"
    with zipfile.ZipFile(crate) as zf:
        members = set(zf.namelist())
    for part in _ids(root["hasPart"]):
        assert part in members, f"hasPart {part} not in the archive"
    # Every local reference resolves to an entity in the graph.
    for entity in doc["@graph"]:
        for key, value in entity.items():
            if key in ("@id", "@type", "@context"):
                continue
            for ref in _ids(value):
                if not ref.startswith(("http://", "https://")):
                    assert ref in graph, f"{entity['@id']}.{key} -> {ref} dangling"
    # Process Run Crate: one CreateAction with an instrument, mentioned by root.
    action = graph["#science-run"]
    assert action["@type"] == "CreateAction"
    assert _ids(action["instrument"])
    assert "#science-run" in _ids(root["mentions"])
    for uri in _ids(root["conformsTo"]):
        assert graph[uri]["@type"] == "CreativeWork" and graph[uri]["version"]


# ── Success ────────────────────────────────────────────────────────────────


def test_process_run_crate_by_default(capsule_factory: Callable[..., Path], tmp_path: Path) -> None:
    capsule = capsule_factory(receipt_kwargs=FULL_RECEIPT)
    result = export_science_rocrate(capsule, tmp_path / "out")
    doc = _metadata(result.crate_path)
    assert_ro_crate_11(doc, result.crate_path)
    graph = _by_id(doc)
    assert _ids(graph["./"]["conformsTo"]) == [PROCESS_RUN_CRATE]
    assert graph[PROCESS_RUN_CRATE]["version"] == WRROC_VERSION
    assert WORKFLOW_RUN_CONTEXT in doc["@context"]
    assert "mainEntity" not in graph["./"]
    assert graph["#code"]["identifier"] == FULL_RECEIPT["code_digest"]
    binding = result.binding
    assert binding.rocrate_profile == "process-run-crate"
    assert binding.prov_alignment == "w3c-prov"
    assert binding.sealed_root == SEALED_ROOT
    assert binding.unbound == []
    assert binding.verdict is None


def test_workflow_run_crate_when_workflow_declared(
    capsule_factory: Callable[..., Path], tmp_path: Path
) -> None:
    capsule = capsule_factory(
        receipt_kwargs={**FULL_RECEIPT, "workflow_digest": digest_node("wf.cwl")}
    )
    result = export_science_rocrate(capsule, tmp_path / "out")
    doc = _metadata(result.crate_path)
    assert_ro_crate_11(doc, result.crate_path)
    graph = _by_id(doc)
    assert _ids(graph["./"]["conformsTo"]) == [
        PROCESS_RUN_CRATE,
        WORKFLOW_RUN_CRATE,
        WORKFLOW_RO_CRATE,
    ]
    assert WORKFLOW_RO_CRATE in _ids(graph["ro-crate-metadata.json"]["conformsTo"])
    assert graph["./"]["mainEntity"] == {"@id": "#workflow"}
    assert "ComputationalWorkflow" in graph["#workflow"]["@type"]
    assert _ids(graph["#science-run"]["instrument"]) == ["#workflow"]
    assert result.binding.rocrate_profile == "workflow-run-crate"


def test_sealed_root_and_receipt_root_are_bound_in(
    capsule_factory: Callable[..., Path], tmp_path: Path
) -> None:
    capsule = capsule_factory(receipt_kwargs=FULL_RECEIPT)
    result = export_science_rocrate(capsule, tmp_path / "out")
    graph = _by_id(_metadata(result.crate_path))
    assert SEALED_ROOT in graph["./"]["identifier"]
    assert graph["#science-provenance"]["identifier"] == SEALED_ROOT
    assert graph["#reproducibility-receipt"]["identifier"] == result.binding.receipt_root


def test_dag_maps_to_prov_derivation_and_results(
    capsule_factory: Callable[..., Path], tmp_path: Path
) -> None:
    capsule = capsule_factory(receipt_kwargs=FULL_RECEIPT)
    graph = _by_id(_metadata(export_science_rocrate(capsule, tmp_path / "o").crate_path))
    nodes = {e["name"]: e for k, e in graph.items() if k.startswith("#science-node-")}
    assert set(nodes) == {
        "hypothesis H1",
        "experiment_design D1",
        "experiment_run R1",
        "observation O1",
        "observation O2",
        "result S1",
        "claim C1",
    }
    assert "isBasedOn" not in nodes["hypothesis H1"]
    assert len(nodes["result S1"]["isBasedOn"]) == 2  # converging DAG preserved
    results = _ids(graph["#science-run"]["result"])
    assert {graph[r]["keywords"] for r in results} == {"observation", "result", "claim"}


def test_receipt_digests_and_seeds_become_property_values(
    capsule_factory: Callable[..., Path], tmp_path: Path
) -> None:
    capsule = capsule_factory(receipt_kwargs=FULL_RECEIPT)
    graph = _by_id(_metadata(export_science_rocrate(capsule, tmp_path / "o").crate_path))
    objects = [graph[i] for i in _ids(graph["#science-run"]["object"])]
    assert all(o["@type"] == "PropertyValue" for o in objects)
    seeds = [o["value"] for o in objects if o["name"] == "seed"]
    assert sorted(seeds) == [42, 1337]
    names = {o["name"] for o in objects}
    assert {"environment_digest", "data_digest", "code_digest"} <= names


def test_create_action_times_and_status(
    capsule_factory: Callable[..., Path], tmp_path: Path
) -> None:
    capsule = capsule_factory(receipt_kwargs=FULL_RECEIPT)
    graph = _by_id(_metadata(export_science_rocrate(capsule, tmp_path / "o").crate_path))
    action = graph["#science-run"]
    assert action["startTime"] == "2026-07-15T10:00:00Z"
    assert action["endTime"] == "2026-07-15T10:05:00Z"
    assert action["actionStatus"] == {"@id": "http://schema.org/CompletedActionStatus"}
    assert graph["./"]["datePublished"] == "2026-07-15T10:05:00Z"


def test_carrier_is_reused_not_reimplemented(
    capsule_factory: Callable[..., Path], tmp_path: Path
) -> None:
    capsule = capsule_factory(receipt_kwargs=FULL_RECEIPT)
    result = export_science_rocrate(capsule, tmp_path / "o")
    graph = _by_id(_metadata(result.crate_path))
    # The NF-040 carrier's File entities (incl. its .seal/ walk) are all present.
    for name in ("capsule.yaml", "lineage.jsonl", ".seal/root.json"):
        assert graph[name]["@type"] == "File"
    assert graph["./"]["creator"]["name"] == "NovaFabric"


def test_output_is_byte_deterministic(capsule_factory: Callable[..., Path], tmp_path: Path) -> None:
    capsule = capsule_factory(receipt_kwargs=FULL_RECEIPT)
    a = export_science_rocrate(capsule, tmp_path / "a")
    b = export_science_rocrate(capsule, tmp_path / "b")
    assert a.crate_path.read_bytes() == b.crate_path.read_bytes()
    assert a.binding_path.read_bytes() == b.binding_path.read_bytes()


def test_binding_record_digest_matches_metadata(
    capsule_factory: Callable[..., Path], tmp_path: Path
) -> None:
    capsule = capsule_factory(receipt_kwargs=FULL_RECEIPT)
    result = export_science_rocrate(capsule, tmp_path / "o")
    with zipfile.ZipFile(result.crate_path) as zf:
        raw = zf.read("ro-crate-metadata.json")
    assert result.binding.rocrate_digest == digest_node(raw)
    on_disk = json.loads(result.binding_path.read_text())
    assert FairBinding.model_validate(on_disk) == result.binding
    assert result.binding_path.name == f"{RUN_ID}.fair-binding.json"


def test_pids_are_references_and_contributors_not_authors(
    capsule_factory: Callable[..., Path], tmp_path: Path
) -> None:
    capsule = capsule_factory(receipt_kwargs=FULL_RECEIPT)
    pids = PersistentIdentifiers(doi=DOI, orcid_refs=[ORCID], ror_refs=[ROR])
    result = export_science_rocrate(capsule, tmp_path / "o", pids=pids)
    doc = _metadata(result.crate_path)
    assert_ro_crate_11(doc, result.crate_path)
    root = _by_id(doc)["./"]
    assert f"https://doi.org/{DOI}" in root["identifier"]
    assert _ids(root["contributor"]) == [f"https://orcid.org/{ORCID}"]
    assert _ids(root["sourceOrganization"]) == [ROR]
    assert "author" not in root
    assert result.binding.orcid_refs == [f"https://orcid.org/{ORCID}"]


def test_facet_without_receipt_or_root_is_recorded_unbound(
    capsule_factory: Callable[..., Path], tmp_path: Path
) -> None:
    capsule = capsule_factory(bound_root=None, extra={"status": "weird"})
    result = export_science_rocrate(capsule, tmp_path / "o")
    doc = _metadata(result.crate_path)
    assert_ro_crate_11(doc, result.crate_path)
    graph = _by_id(doc)
    assert result.binding.unbound == ["sealed_root", "reproducibility_receipt"]
    assert result.binding.sealed_root is None and result.binding.receipt_root is None
    assert graph["./"]["identifier"] == RUN_ID
    assert "#instrument-undeclared" in graph
    assert "actionStatus" not in graph["#science-run"]
    assert "#reproducibility-receipt" not in graph


# ── Refusals ───────────────────────────────────────────────────────────────


def test_non_science_capsule_is_refused(
    capsule_factory: Callable[..., Path], tmp_path: Path
) -> None:
    capsule = capsule_factory(with_facet=False)
    with pytest.raises(ScienceMaterialMissingError):
        export_science_rocrate(capsule, tmp_path / "o")
    assert not (tmp_path / "o").exists()


def test_missing_manifest_is_refused(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(ScienceMaterialMissingError):
        export_science_rocrate(tmp_path / "empty", tmp_path / "o")


@pytest.mark.parametrize("content", ["- a list\n", ": : not yaml : ["])
def test_unreadable_manifest_is_refused(tmp_path: Path, content: str) -> None:
    capsule = tmp_path / "c"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text(content)
    with pytest.raises(CrateCarrierError):
        export_science_rocrate(capsule, tmp_path / "o")


def test_tampered_receipt_is_refused(capsule_factory: Callable[..., Path], tmp_path: Path) -> None:
    capsule = capsule_factory(receipt_kwargs=FULL_RECEIPT)
    path = capsule / "capsule.yaml"
    manifest = yaml.safe_load(path.read_text())
    manifest["facets"][FACET_NAME][RECEIPT_KEY]["seeds"] = [1, 2]
    path.write_text(yaml.safe_dump(manifest))
    with pytest.raises(TamperedReceiptError):
        export_science_rocrate(capsule, tmp_path / "o")


def test_workflow_profile_without_workflow_is_refused(
    capsule_factory: Callable[..., Path], tmp_path: Path
) -> None:
    capsule = capsule_factory(receipt_kwargs=FULL_RECEIPT)
    with pytest.raises(UnsupportedProfileError):
        export_science_rocrate(capsule, tmp_path / "o", profile="workflow-run-crate")


def test_explicit_process_profile_with_workflow_is_allowed() -> None:
    receipt = build_receipt(workflow_digest=digest_node("wf"))
    assert select_profile(receipt, "process-run-crate") == "process-run-crate"
    assert select_profile(receipt, None) == "workflow-run-crate"
    assert select_profile(None, None) == "process-run-crate"


def test_broken_dag_is_refused(capsule_factory: Callable[..., Path], tmp_path: Path) -> None:
    capsule = capsule_factory(receipt_kwargs=FULL_RECEIPT)
    path = capsule / "capsule.yaml"
    manifest = yaml.safe_load(path.read_text())
    manifest["facets"][FACET_NAME]["hypothesis_experiment_result"][1]["parent"] = digest_node(
        "nowhere"
    )
    path.write_text(yaml.safe_dump(manifest))
    with pytest.raises(ScienceProvenanceError):
        export_science_rocrate(capsule, tmp_path / "o")


def test_carrier_document_without_graph_is_rejected() -> None:
    facet = ScienceProvenanceFacet()
    with pytest.raises(CrateCarrierError):
        build_science_metadata(
            {}, facet=facet, receipt=None, profile="process-run-crate", manifest={}
        )
    with pytest.raises(CrateCarrierError):
        build_science_metadata(
            {"@graph": [{"@id": "./"}]},
            facet=facet,
            receipt=None,
            profile="process-run-crate",
            manifest={},
        )


def test_carrier_archive_problems_are_rejected(
    capsule_factory: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.science import fair_rocrate

    capsule = capsule_factory(receipt_kwargs=FULL_RECEIPT)
    monkeypatch.setattr(fair_rocrate, "_carrier_members", lambda _d: {"x": b""})
    with pytest.raises(CrateCarrierError):
        export_science_rocrate(capsule, tmp_path / "o")
    monkeypatch.setattr(
        fair_rocrate, "_carrier_members", lambda _d: {"ro-crate-metadata.json": b"{"}
    )
    with pytest.raises(CrateCarrierError):
        export_science_rocrate(capsule, tmp_path / "o")


def test_graph_without_timestamp_keeps_carrier_date() -> None:
    carrier = {
        "@context": RO_CRATE_CONTEXT,
        "@graph": [
            {"@id": "ro-crate-metadata.json", "@type": "CreativeWork", "dateCreated": "T0"},
            {"@id": "./", "@type": "Dataset", "datePublished": "T0", "hasPart": []},
        ],
    }
    doc = build_science_metadata(
        carrier,
        facet=ScienceProvenanceFacet(),
        receipt=None,
        profile="process-run-crate",
        manifest={"run_id": "r9"},
    )
    root = _by_id(doc)["./"]
    assert root["datePublished"] == "T0"
    assert root["identifier"] == "r9"


# ── Persistent identifiers ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("doi", "expected"),
    [
        (DOI, f"https://doi.org/{DOI}"),
        (f"https://doi.org/{DOI}", f"https://doi.org/{DOI}"),
        (f"doi:{DOI}", f"https://doi.org/{DOI}"),
    ],
)
def test_doi_normalises(doi: str, expected: str) -> None:
    assert PersistentIdentifiers(doi=doi).doi == expected


@pytest.mark.parametrize(
    "kwargs",
    [
        {"doi": "not-a-doi"},
        {"orcid_refs": ["0000-0002-1825-0098"]},  # bad checksum
        {"orcid_refs": ["1234"]},
        {"ror_refs": ["https://ror.org/xyz"]},
        {"orcid_refs": "0000-0002-1825-0097"},  # not a list
        {"ror_refs": ["03yrm5c26"] * 300},
    ],
)
def test_bad_pids_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(InvalidPersistentIdentifierError):
        PersistentIdentifiers(**kwargs)


def test_pid_lists_dedupe_and_sort() -> None:
    pids = PersistentIdentifiers(
        orcid_refs=[f"https://orcid.org/{ORCID}", ORCID, "0000-0001-5109-3700"],
        ror_refs=["03yrm5c26"],
    )
    assert pids.orcid_refs == [
        "https://orcid.org/0000-0001-5109-3700",
        f"https://orcid.org/{ORCID}",
    ]
    assert pids.ror_refs == [ROR]


def test_orcid_x_check_digit_accepted() -> None:
    # 0000-0002-1694-233X is ORCID's documented example with an X check digit.
    assert PersistentIdentifiers(orcid_refs=["0000-0002-1694-233X"]).orcid_refs


def test_none_pid_lists_normalise_to_empty() -> None:
    pids = PersistentIdentifiers(orcid_refs=None, ror_refs=None)  # type: ignore[arg-type]
    assert pids.orcid_refs == [] and pids.ror_refs == []
