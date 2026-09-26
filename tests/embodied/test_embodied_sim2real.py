"""ADR-0162 P3 — NF-304 sim-to-real lineage.

Acceptance criteria (spec §6 "Sim-to-real"):

- a sim-trained ``sim_policy_ref`` + real ``deployment_run_id`` records and
  verifies offline; the walk passes;
- an absent or unresolvable ``sim_policy_ref`` yields ``unbound: true`` —
  recorded, never fatal, on every construction path (a claimed binding with no
  ref is overridden);
- verification is fail-closed on contradiction: a local artifact whose digest
  differs, or a ``deployment_run_id`` naming another capsule, fails;
- refs are ``sha256:`` digests (no trailing-newline bypass), run ids bounded
  identifiers, and the I-2 payload boundary applies.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from novafabric.embodied import (
    FACET_NAME,
    ArtifactReadError,
    EmbodiedFacet,
    InvalidDeploymentRunError,
    InvalidReferenceError,
    RawPayloadRejectedError,
    Sim2RealLineage,
    attach_facet,
    build_facet,
    build_sim2real,
    digest_artifact_file,
    digest_stream,
    verify_sim2real,
)

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "embodied"
POLICY = b"golden sim policy checkpoint v1"
POLICY_REF = digest_stream(POLICY)
ENV_REF = digest_stream(b"golden sim env config v1")
RAND_REF = digest_stream(b"golden domain randomization spec v1")
RUN = "run:01HXAY7M5JZ8R7K4P9DPBYK2WX"


def _fixture(name: str) -> dict[str, Any]:
    raw: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    raw.pop("_comment", None)
    return raw


def _lineage(**kw: Any) -> Sim2RealLineage:
    base: dict[str, Any] = {
        "sim_policy_ref": POLICY_REF,
        "sim_env_ref": ENV_REF,
        "deployment_run_id": RUN,
        "unbound": False,
    }
    base.update(kw)
    return Sim2RealLineage(**base)


# ── Recording (fail-open on resolution) ───────────────────────────────────


def test_resolver_that_produces_the_policy_binds_it() -> None:
    lineage = build_sim2real(
        sim_policy_ref=POLICY_REF,
        sim_env_ref=ENV_REF,
        randomization_ref=RAND_REF,
        deployment_run_id=RUN,
        resolver=lambda ref: POLICY,
    )
    assert lineage.unbound is False
    assert lineage.randomization_ref == RAND_REF


@pytest.mark.parametrize(
    "resolver",
    [
        lambda ref: None,
        lambda ref: b"some other checkpoint",
        lambda ref: (_ for _ in ()).throw(OSError("store offline")),
    ],
    ids=["unresolvable", "mismatch", "resolver-raises"],
)
def test_unresolvable_policy_is_recorded_unbound_not_fatal(resolver: Any) -> None:
    lineage = build_sim2real(
        sim_policy_ref=POLICY_REF, sim_env_ref=ENV_REF, deployment_run_id=RUN, resolver=resolver
    )
    assert lineage.unbound is True
    assert lineage.sim_policy_ref == POLICY_REF  # recorded, never dropped


def test_ref_without_resolver_is_left_as_claimed() -> None:
    assert (
        build_sim2real(
            sim_policy_ref=POLICY_REF, sim_env_ref=ENV_REF, deployment_run_id=RUN
        ).unbound
        is False
    )


def test_absent_policy_is_unbound_on_every_path() -> None:
    assert build_sim2real(sim_env_ref=ENV_REF, deployment_run_id=RUN).unbound is True
    forged = Sim2RealLineage.model_validate(
        {"sim_env_ref": ENV_REF, "deployment_run_id": RUN, "unbound": False}
    )
    assert forged.unbound is True
    loaded = EmbodiedFacet.model_validate(_fixture("sim2real-unbound-facet.json"))
    assert loaded.sim2real is not None and loaded.sim2real.unbound is True


def test_string_resolver_output_is_hashed_as_utf8() -> None:
    ref = digest_stream("text policy")
    lineage = build_sim2real(
        sim_policy_ref=ref,
        sim_env_ref=ENV_REF,
        deployment_run_id=RUN,
        resolver=lambda r: "text policy",
    )
    assert lineage.unbound is False


# ── Shape ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    ["not-a-digest", POLICY_REF + "\n", POLICY_REF.upper(), "sha256:" + "a" * 63],
)
def test_non_digest_refs_are_refused(bad: str) -> None:
    with pytest.raises(InvalidReferenceError):
        _lineage(sim_policy_ref=bad)
    with pytest.raises(InvalidReferenceError):
        _lineage(sim_env_ref=bad)


@pytest.mark.parametrize("bad", ["", "run 01", "run:01\n", "x" * 129, 42, "-leading", "run:01;rm"])
def test_malformed_deployment_run_id_is_refused(bad: Any) -> None:
    with pytest.raises(InvalidDeploymentRunError):
        _lineage(deployment_run_id=bad)


@pytest.mark.parametrize("bad", ["false", 0, 1, None])
def test_unbound_must_be_a_real_boolean(bad: Any) -> None:
    with pytest.raises(InvalidDeploymentRunError):
        _lineage(unbound=bad)


def test_non_string_ref_is_a_validation_error() -> None:
    with pytest.raises(ValidationError):
        _lineage(sim_env_ref=42)


def test_raw_payload_is_refused() -> None:
    with pytest.raises(RawPayloadRejectedError):
        _lineage(sim_policy_ref=b"\x00weights")
    with pytest.raises(RawPayloadRejectedError):
        _lineage(policy_weights=b"\x00" * 8)
    with pytest.raises(RawPayloadRejectedError):
        _lineage(rawCheckpoint="x")


# ── Facet integration ─────────────────────────────────────────────────────


def test_golden_valid_fixture_round_trips() -> None:
    raw = _fixture("valid-p3-sim2real-teleop-timing-facet.json")
    facet = EmbodiedFacet.model_validate(raw)
    assert facet.sim2real is not None and facet.sim2real.unbound is False
    out = attach_facet({"run_id": "r"}, facet)
    assert out["facets"][FACET_NAME]["sim2real"] == raw["sim2real"]


def test_build_facet_with_only_sim2real_is_not_empty() -> None:
    facet = build_facet(sim2real=_lineage())
    assert facet is not None and facet.sim2real is not None
    assert facet.schema_version == "0.3.0"
    dumped = facet.model_dump(exclude_none=True)
    assert "teleop" not in dumped and "timing" not in dumped  # absent is not false


# ── Offline verification ──────────────────────────────────────────────────


def test_verify_passes_for_a_bound_matching_record() -> None:
    report = verify_sim2real(
        _lineage(),
        capsule_run_id="01HXAY7M5JZ8R7K4P9DPBYK2WX",
        checkpoint_digests=[POLICY_REF],
        artifact_digests={"sim_policy_ref": POLICY_REF},
    )
    assert report.ok
    assert report.deployment_matches is True
    assert report.checkpoint_chain_bound is True
    assert report.resolved == {"sim_policy_ref": True}
    assert {f.code for f in report.findings} == {"checkpoint_chain_bound", "artifact_resolved"}


def test_verify_unbound_is_a_warning_not_a_failure() -> None:
    report = verify_sim2real(_lineage(sim_policy_ref=None), capsule_run_id=RUN)
    assert report.ok and report.unbound
    assert [f.code for f in report.findings] == ["unbound"]


def test_verify_artifact_mismatch_fails_closed() -> None:
    report = verify_sim2real(
        _lineage(), capsule_run_id=RUN, artifact_digests={"sim_policy_ref": ENV_REF}
    )
    assert not report.ok
    assert report.resolved == {"sim_policy_ref": False}
    assert any(f.code == "artifact_mismatch" and f.severity == "error" for f in report.findings)


def test_verify_deployment_mismatch_fails_closed() -> None:
    report = verify_sim2real(_lineage(), capsule_run_id="some-other-run")
    assert not report.ok and report.deployment_matches is False


def test_verify_without_run_id_warns_unchecked() -> None:
    report = verify_sim2real(_lineage())
    assert report.ok and report.deployment_matches is None
    assert [f.code for f in report.findings] == ["deployment_unchecked"]


def test_verify_artifact_for_unrecorded_field_warns() -> None:
    report = verify_sim2real(
        _lineage(), capsule_run_id=RUN, artifact_digests={"randomization_ref": RAND_REF}
    )
    assert report.ok and report.resolved == {}
    assert [f.code for f in report.findings] == ["artifact_unreferenced"]


# ── Local artifact hashing (bounded) ──────────────────────────────────────


def test_digest_artifact_file_matches_digest_stream(tmp_path: Path) -> None:
    path = tmp_path / "policy.ckpt"
    path.write_bytes(POLICY)
    assert digest_artifact_file(path) == POLICY_REF


def test_digest_artifact_file_refuses_missing_dirs_and_oversize(tmp_path: Path) -> None:
    with pytest.raises(ArtifactReadError, match="cannot stat"):
        digest_artifact_file(tmp_path / "nope")
    with pytest.raises(ArtifactReadError, match="not a regular file"):
        digest_artifact_file(tmp_path)
    big = tmp_path / "big"
    big.write_bytes(b"x" * 10)
    with pytest.raises(ArtifactReadError, match="cap"):
        digest_artifact_file(big, max_bytes=5)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_digest_artifact_file_refuses_a_fifo(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(ArtifactReadError, match="not a regular file"):
        digest_artifact_file(fifo)


def test_digest_artifact_file_refuses_growth_past_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import novafabric.embodied.sim2real as mod

    path = tmp_path / "grows"
    path.write_bytes(b"x" * 4)
    monkeypatch.setattr(mod, "_CHUNK", 2)
    real_stat = Path.stat

    def small_stat(self: Path, **kw: Any) -> os.stat_result:
        result = real_stat(self, **kw)
        if self == path:
            fields = list(result)
            fields[6] = 1  # st_size: pretend the file was 1 byte when checked
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(Path, "stat", small_stat)
    with pytest.raises(ArtifactReadError, match="grew past"):
        digest_artifact_file(path, max_bytes=3)


def test_digest_artifact_file_unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "locked"
    path.write_bytes(b"x")

    def boom(self: Path, *a: Any, **kw: Any) -> Any:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "open", boom)
    with pytest.raises(ArtifactReadError, match="cannot read"):
        digest_artifact_file(path)
