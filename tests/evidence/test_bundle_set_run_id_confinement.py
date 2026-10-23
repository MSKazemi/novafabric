"""A capsule-set bundle never stages a member outside its temp directory (review trust-evidence-02).

The set builder staged each member under ``run-capsule/<run_id>/``,
``lineage-subgraph/<run_id>/`` and ``attestations/<run_id>/``, with ``run_id``
read from the capsule's own ``capsule.yaml``, which the workload wrote. A
``../`` value made the exporter (the ``nova serve`` cart export, or the CLI)
create a directory anywhere it could write, full of capsule files, and
overwrite ``edges.jsonl``, ``run.intoto.json`` and signatures in existing ones.
Two members with one ``run_id`` collided (raw ``FileExistsError``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.evidence.bundle import CapsuleSetBundleBuilder, CapsuleValidationError
from novafabric.evidence.signing import LocalSigner, generate_keypair


def _capsule(tmp_path: Path, name: str) -> Path:
    return (
        CaptureOrchestrator(base_dir=tmp_path / f"runs-{name}")
        .run(command=[sys.executable, "-c", "pass"])
        .capsule_dir
    )


def _set_run_id(capsule: Path, run_id: str) -> None:
    path = capsule / "capsule.yaml"
    manifest = yaml.safe_load(path.read_text())
    manifest["run_id"] = run_id
    path.write_text(yaml.safe_dump(manifest))


@pytest.fixture(scope="module")
def signer(tmp_path_factory: pytest.TempPathFactory) -> LocalSigner:
    priv, _ = generate_keypair(tmp_path_factory.mktemp("keys"))
    return LocalSigner(priv)


def _tree(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*")}


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../../escaped",
        "../escaped",
        "/tmp/escaped-absolute",
        "a/../../escaped",
        "..\\escaped",
        "escaped\x00",
        "..",
    ],
)
def test_a_hostile_member_run_id_is_refused_before_any_write(
    tmp_path: Path, signer: LocalSigner, hostile: str
) -> None:
    good = _capsule(tmp_path, "a")
    evil = _capsule(tmp_path, "b")
    _set_run_id(evil, hostile.replace("/tmp/", f"{tmp_path}/"))
    before = _tree(tmp_path)
    out = tmp_path / "set.zip"
    with pytest.raises(CapsuleValidationError, match="run_id"):
        CapsuleSetBundleBuilder([good, evil], signer, out).build()
    assert not out.exists()
    assert _tree(tmp_path) == before
    assert not (tmp_path / "escaped").exists()
    assert not (tmp_path / "escaped-absolute").exists()


def test_a_repeated_run_id_is_refused(tmp_path: Path, signer: LocalSigner) -> None:
    a = _capsule(tmp_path, "a")
    b = _capsule(tmp_path, "b")
    _set_run_id(b, yaml.safe_load((a / "capsule.yaml").read_text())["run_id"])
    with pytest.raises(CapsuleValidationError, match="appears twice"):
        CapsuleSetBundleBuilder([a, b], signer, tmp_path / "set.zip").build()


def test_clean_run_ids_still_build_a_set(tmp_path: Path, signer: LocalSigner) -> None:
    out = tmp_path / "set.zip"
    CapsuleSetBundleBuilder(
        [_capsule(tmp_path, "a"), _capsule(tmp_path, "b")], signer, out
    ).build()
    assert out.read_bytes()[:4] == b"PK\x03\x04"


def test_a_link_at_the_output_name_is_replaced_not_written_through(
    tmp_path: Path, signer: LocalSigner
) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"precious")
    out = tmp_path / "set.zip"
    out.symlink_to(victim)
    CapsuleSetBundleBuilder(
        [_capsule(tmp_path, "a"), _capsule(tmp_path, "b")], signer, out
    ).build()
    assert victim.read_bytes() == b"precious"
    assert not out.is_symlink()
    assert out.read_bytes()[:4] == b"PK\x03\x04"
