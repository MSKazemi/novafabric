"""ADR-0011 Amendment 1 — an Evidence Bundle may carry a capsule **set**.

The bundle schema already allowed this before any code did:

    "subject": {"oneOf": [Subject, {"type": "array", "items": Subject, "minItems": 2}]}

So this is an implementation of the shipped format, not a change to it. Two
things follow, and both are asserted here:

* a **one-capsule** export must stay exactly what it always was — the array form
  starts at two, and a single-capsule bundle *is* a normal bundle;
* a **set** must verify with the shipped `nova verify`, unmodified. That is the
  entire argument for one bundle over N bundles, so it is proved by running the
  real verifier rather than by inspecting the manifest.
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import jsonschema
import pytest

from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.evidence.bundle import EvidenceBundleBuilder
from novafabric.evidence.signing import LocalSigner, generate_keypair

_SCHEMA = json.loads(
    (
        Path(__file__).parents[2] / "src/novafabric/schemas/evidence-bundle.schema.json"
    ).read_text()
)


def _capsule(tmp_path: Path, name: str) -> Path:
    return (
        CaptureOrchestrator(base_dir=tmp_path / f"runs-{name}")
        .run(command=[sys.executable, "-c", "pass"])
        .capsule_dir
    )


def _signer(tmp_path: Path) -> LocalSigner:
    priv, _ = generate_keypair(tmp_path / f"keys-{id(tmp_path)}")
    return LocalSigner(priv)


def _manifest(zip_path: Path) -> dict:
    with zipfile.ZipFile(zip_path) as zf:
        return json.loads(zf.read("manifest.json"))


def _names(zip_path: Path) -> set[str]:
    with zipfile.ZipFile(zip_path) as zf:
        return set(zf.namelist())


# ---------------------------------------------------------------------------
# AC1 — the single-capsule bundle is unchanged
# ---------------------------------------------------------------------------


def test_single_capsule_bundle_keeps_its_exact_shape(tmp_path: Path) -> None:
    """The criterion that protects every bundle already in the wild.

    Written and passing *before* the builder was refactored, so a regression in
    the shared staging/signing path shows up here rather than in a user's
    verifier.
    """
    cap = _capsule(tmp_path, "a")
    out = tmp_path / "single.zip"
    EvidenceBundleBuilder(cap, _signer(tmp_path), out).build()

    manifest = _manifest(out)
    jsonschema.validate(manifest, _SCHEMA)

    subject = manifest["subject"]
    assert isinstance(subject, dict), "one capsule must use the object form, not a list"
    assert subject["kind"] == "run-capsule"
    assert subject["capsule_path_in_bundle"] == "run-capsule/"

    names = _names(out)
    assert "run-capsule/capsule.yaml" in names
    assert "attestations/run.intoto.json" in names
    assert "attestations/redaction.intoto.json" in names
    assert "attestations/lineage.intoto.json" in names
    assert "signatures/run.sig" in names


# ---------------------------------------------------------------------------
# AC2/AC3 — a set bundle, verified by the shipped verifier
# ---------------------------------------------------------------------------


def _set_builder(tmp_path: Path, caps: list[Path], out: Path, **kw):  # type: ignore[no-untyped-def]
    from novafabric.evidence.bundle import CapsuleSetBundleBuilder

    return CapsuleSetBundleBuilder(caps, _signer(tmp_path), out, **kw)


def test_a_set_bundle_carries_a_subject_per_capsule(tmp_path: Path) -> None:
    caps = [_capsule(tmp_path, "a"), _capsule(tmp_path, "b")]
    out = tmp_path / "set.zip"
    _set_builder(tmp_path, caps, out).build()

    manifest = _manifest(out)
    jsonschema.validate(manifest, _SCHEMA)

    subjects = manifest["subject"]
    assert isinstance(subjects, list) and len(subjects) == 2
    assert {s["kind"] for s in subjects} == {"run-capsule"}
    # Distinct capsules must never share a digest or a path.
    assert len({s["capsule_hash"] for s in subjects}) == 2
    assert len({s["capsule_path_in_bundle"] for s in subjects}) == 2


def test_the_shipped_verifier_reads_a_set_bundle_unmodified(tmp_path: Path) -> None:
    """This is the whole argument for one bundle over N, so it is *run*.

    `nova verify` works off `artifacts[]` and `manifest_hash` and never consults
    `subject`. If that ever stops being true, this fails and the design premise
    needs revisiting — which is exactly when someone should be told.
    """
    import typer

    from novafabric.cli.verify import _verify_evidence_bundle

    caps = [_capsule(tmp_path, "a"), _capsule(tmp_path, "b")]
    out = tmp_path / "set.zip"
    _set_builder(tmp_path, caps, out).build()

    # Exits non-zero on any digest mismatch, missing artifact, or unlisted file.
    try:
        _verify_evidence_bundle(out)
    except typer.Exit as exc:  # pragma: no cover - failure path
        raise AssertionError(f"shipped verifier rejected a capsule-set bundle: {exc}") from exc


def test_every_capsule_is_staged_under_its_own_run_id(tmp_path: Path) -> None:
    caps = [_capsule(tmp_path, "a"), _capsule(tmp_path, "b")]
    out = tmp_path / "set.zip"
    _set_builder(tmp_path, caps, out).build()

    names = _names(out)
    for subject in _manifest(out)["subject"]:
        run_id = subject["run_id"]
        assert f"run-capsule/{run_id}/capsule.yaml" in names
        assert f"attestations/{run_id}/run.intoto.json" in names


def test_attestation_subjects_are_never_shared_between_capsules(tmp_path: Path) -> None:
    """AC5. An envelope naming the wrong capsule is worse than none at all."""
    caps = [_capsule(tmp_path, "a"), _capsule(tmp_path, "b")]
    out = tmp_path / "set.zip"
    _set_builder(tmp_path, caps, out).build()

    manifest = _manifest(out)
    run_atts = [
        a for a in manifest["attestations"] if a["path"].endswith("run.intoto.json")
    ]
    assert len(run_atts) == 2
    digests = [h for a in run_atts for h in a["subject_hashes"]]
    assert len(set(digests)) == 2, "two capsules shared one attestation subject digest"


def test_covered_by_points_at_the_right_capsules_attestation(tmp_path: Path) -> None:
    """Telling a verifier a file is attested by an envelope over *another*
    capsule is a worse answer than omitting the hint."""
    caps = [_capsule(tmp_path, "a"), _capsule(tmp_path, "b")]
    out = tmp_path / "set.zip"
    _set_builder(tmp_path, caps, out).build()

    manifest = _manifest(out)
    for art in manifest["artifacts"]:
        rel = art["path"]
        if not rel.startswith("run-capsule/"):
            continue
        run_id = rel.split("/")[1]
        for envelope in art.get("covered_by", []):
            assert envelope.startswith(f"attestations/{run_id}/"), (
                f"{rel} claims coverage by {envelope}, which is a different capsule"
            )


# ---------------------------------------------------------------------------
# AC4 / AC7 / AC8 — refuse before writing
# ---------------------------------------------------------------------------


def test_an_empty_set_is_refused(tmp_path: Path) -> None:
    """A bundle of nothing still carries a valid signature and reads as success."""
    from novafabric.evidence.bundle import CapsuleValidationError

    with pytest.raises(CapsuleValidationError, match="at least one capsule"):
        _set_builder(tmp_path, [], tmp_path / "empty.zip")


def test_a_duplicate_capsule_is_refused(tmp_path: Path) -> None:
    """A repeated subject over-counts the evidence a recipient thinks they have."""
    from novafabric.evidence.bundle import CapsuleValidationError

    cap = _capsule(tmp_path, "a")
    with pytest.raises(CapsuleValidationError, match="appears twice"):
        _set_builder(tmp_path, [cap, cap], tmp_path / "dup.zip")


def test_one_bad_capsule_writes_no_output_at_all(tmp_path: Path) -> None:
    """AC4. A half-written evidence bundle is worse than none: it is a signed
    artifact whose contents do not match what was asked for."""
    from novafabric.evidence.bundle import CapsuleValidationError

    good = _capsule(tmp_path, "a")
    bad = tmp_path / "not-a-capsule"
    bad.mkdir()
    (bad / "capsule.yaml").write_text("run_id: nope\n")

    out = tmp_path / "partial.zip"
    with pytest.raises(CapsuleValidationError):
        _set_builder(tmp_path, [good, bad], out).build()
    assert not out.exists(), "a refused export must leave no file behind"


def test_a_single_capsule_set_is_an_ordinary_bundle(tmp_path: Path) -> None:
    """The schema's array form starts at two, and one capsule *is* a normal export."""
    cap = _capsule(tmp_path, "a")
    out = tmp_path / "one.zip"
    _set_builder(tmp_path, [cap], out).build()

    manifest = _manifest(out)
    jsonschema.validate(manifest, _SCHEMA)
    assert isinstance(manifest["subject"], dict)
    assert "run-capsule/capsule.yaml" in _names(out)


# ---------------------------------------------------------------------------
# AC6 — the ADR-0239 curation record is tamper-evident
# ---------------------------------------------------------------------------


def test_the_curation_record_is_a_hashed_artifact(tmp_path: Path) -> None:
    """D5's requirement is that the disclosure cannot be quietly removed.

    As a file listed in `artifacts[]` its digest is covered by `manifest_hash`,
    so deleting it is reported and altering it changes a recorded digest.
    """
    from novafabric.evidence.cart import CURATION_DISCLOSURE

    caps = [_capsule(tmp_path, "a"), _capsule(tmp_path, "b")]
    out = tmp_path / "curated.zip"
    curation = {
        "operator_assembled": True,
        "exhaustive": False,
        "disclosure": CURATION_DISCLOSURE,
    }
    _set_builder(tmp_path, caps, out, curation=curation).build()

    names = _names(out)
    assert "curation.json" in names

    manifest = _manifest(out)
    entry = next(a for a in manifest["artifacts"] if a["path"] == "curation.json")
    assert entry["sha256"].startswith("sha256:")

    with zipfile.ZipFile(out) as zf:
        recorded = json.loads(zf.read("curation.json"))
    assert recorded["exhaustive"] is False
    assert recorded["operator_assembled"] is True
    assert "curated subset" in recorded["disclosure"]


def test_a_set_without_curation_carries_no_curation_file(tmp_path: Path) -> None:
    """Absent means "not a curated selection" — a different claim from `false`."""
    caps = [_capsule(tmp_path, "a"), _capsule(tmp_path, "b")]
    out = tmp_path / "plain.zip"
    _set_builder(tmp_path, caps, out).build()
    assert "curation.json" not in _names(out)
