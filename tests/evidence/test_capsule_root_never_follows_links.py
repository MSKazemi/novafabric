"""The capsule Merkle root never follows a link and never blocks on a FIFO.

``evidence.merkle.capsule_root`` walked the capsule with ``rglob`` + ``is_file`` +
``read_bytes``. Both follow symbolic links, so ``inputs/x -> ~/.ssh/id_rsa`` made the
root commit to a file outside the capsule, and that root is the signed subject of
Evidence Bundles, consent attestations, AI-BOMs, baseline pins, comment and
annotation subjects and score submissions. A file swapped for a FIFO between the
walk and the read blocked ``read_bytes`` forever.

The root now walks through ``novafabric._capsule_fs`` (the layer every exporter
shares): a link raises ``CapsuleLinkError`` and no root is computed over the other
files; a FIFO in the capsule is left out without being opened, as it always was;
a listed file swapped for a FIFO raises ``CapsuleSpecialFileError``. For a capsule
without links the v1 and v2 roots are byte-identical to the old walk's, pinned
below by values computed with the old code.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar

import pytest
import yaml
from typer.testing import CliRunner

from novafabric._capsule_fs import CapsuleLinkError, CapsuleSpecialFileError
from novafabric.capsule.comments import COMMENTS_FILENAME, capsule_subject_digest
from novafabric.eval.annotation_store import annotation_subject_digest
from novafabric.evidence import merkle
from novafabric.evidence.merkle import (
    capsule_merkle_root,
    capsule_root,
    capsule_root_matches,
    root_over_files,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks and FIFOs")

T = TypeVar("T")

CANARY = b"CANARY-OUTSIDE-THE-CAPSULE-91c2"
RUN_ID = "01J9ZZTESTMERKLENOFOLLOW01"

#: Most seconds any one root computation may take before the test calls it a hang.
HANG_BUDGET_S = 20.0

# -- the clean capsule and its roots, computed with the pre-fix rglob walk ---------

TRICKY: dict[str, bytes] = {
    "capsule.yaml": b"run_id: golden\n",
    "a-b": b"dash",
    "a/b": b"slash",
    "a.txt": b"dot",
    "a/c/d.json": b"{}",
    ".hidden": b"h",
    "empty.bin": b"",
    "été/café.txt": "unicode".encode(),
    "Z.txt": b"upper",
    "big.bin": bytes(range(256)) * 9000,  # > 2 MiB: several read chunks
    "comments.jsonl": b'{"c":1}\n',
    "scores.jsonl": b'{"s":1}\n',
}

#: Recorded on 2026-10-23 from ``capsule_root`` as it stood before this change
#: (``sorted(rglob("*"))`` + ``is_file`` + ``read_bytes``), commit 19ad8a41.
GOLDEN: dict[str, str] = {
    "v1": "sha256:29d2789499adb635ec8c07cb395cfe492a1d5818ab498cae45e92dd4c2493da0",
    "v2": "sha256:a614422b3388cf03447c1e3c70c1885238f8bbdff25e4e97d1648df429ae33c9",
    "v2_without_big": "sha256:e69754b85815dde18c430fa3ae2d5470cbaef772e509c6a7b33e4f4a54cb500c",
    "comment_subject": "sha256:3409e159a7a31979f5e55fcbbda3edad0f2b9260cb9372cab8275698f4365156",
    "annotation_subject": (
        "sha256:5da3ab6fd7953a5b0556ee719872a27c4d6e669e46013d2ba109bb72702e88eb"
    ),
}


def _tricky(root: Path) -> Path:
    for rel, data in TRICKY.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (root / "emptydir").mkdir()
    return root


def _bounded(fn: Callable[[], T], *, fifo: Path | None = None) -> T:
    """Run *fn* in a daemon thread; fail (and unblock a FIFO reader) if it hangs."""
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the test thread
            box["error"] = exc

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(HANG_BUDGET_S)
    if worker.is_alive():
        if fifo is not None:
            try:  # a blocked reader's open() completes once a writer appears
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass
        worker.join(5)
        pytest.fail(f"root computation blocked for more than {HANG_BUDGET_S}s")
    if "error" in box:
        raise box["error"]
    return box["value"]  # type: ignore[no-any-return]


@pytest.fixture()
def secret(tmp_path: Path) -> Path:
    path = tmp_path / "outside" / ".ssh" / "id_rsa"
    path.parent.mkdir(parents=True)
    path.write_bytes(CANARY + b"\n")
    return path


def _write_capsule(cap: Path) -> Path:
    (cap / "inputs").mkdir(parents=True)
    (cap / "outputs").mkdir()
    (cap / "capsule.yaml").write_text(
        yaml.safe_dump({"run_id": RUN_ID, "schema_version": "0.1.0", "status": "success"})
    )
    (cap / "trace.jsonl").write_text('{"span": 1}\n')
    (cap / "inputs" / "data.txt").write_text("input\n")
    (cap / "outputs" / "result.txt").write_text("output\n")
    return cap


@pytest.fixture()
def capsule(tmp_path: Path) -> Path:
    return _write_capsule(tmp_path / "caps" / RUN_ID)


def _file_link(cap: Path, secret: Path) -> None:
    os.symlink(secret, cap / "inputs" / "stolen")


def _top_level_link(cap: Path, secret: Path) -> None:
    os.symlink(secret, cap / "notes.txt")


def _dangling_link(cap: Path, secret: Path) -> None:
    os.symlink(secret.parent / "does-not-exist", cap / "inputs" / "gone")


def _dir_link(cap: Path, secret: Path) -> None:
    os.symlink(secret.parent, cap / "inputs" / "ssh")


def _inside_link(cap: Path, secret: Path) -> None:
    os.symlink(cap / "trace.jsonl", cap / "outputs" / "trace-copy.jsonl")


def _parent_dir_link(cap: Path, secret: Path) -> None:
    for child in (cap / "outputs").iterdir():
        child.unlink()
    (cap / "outputs").rmdir()
    os.symlink(secret.parent, cap / "outputs")


VARIANTS: dict[str, Callable[[Path, Path], None]] = {
    "file-link": _file_link,
    "top-level-link": _top_level_link,
    "dangling": _dangling_link,
    "dir-link": _dir_link,
    "inside-link": _inside_link,
    "parent-dir-link": _parent_dir_link,
}


# -- clean capsules: the root value is unchanged ---------------------------------


def test_clean_capsule_roots_are_byte_identical_to_the_old_walk(tmp_path: Path) -> None:
    cap = _tricky(tmp_path / "tricky")
    assert capsule_root(cap, version=1) == GOLDEN["v1"]
    assert capsule_root(cap, version=2) == GOLDEN["v2"]
    assert capsule_merkle_root(cap) == GOLDEN["v2"]
    assert capsule_root(cap, exclude=lambda p: p.name == "big.bin") == GOLDEN["v2_without_big"]
    assert capsule_subject_digest(cap) == GOLDEN["comment_subject"]
    assert annotation_subject_digest(cap) == GOLDEN["annotation_subject"]


def test_the_on_disk_root_still_equals_the_bundle_root(tmp_path: Path) -> None:
    """``nova verify`` recomputes the root from the bundle copy; the two must agree."""
    cap = _tricky(tmp_path / "tricky")
    for version in (1, 2):
        assert root_over_files(TRICKY.items(), version=version) == capsule_root(
            cap, version=version
        )


def test_a_root_reached_through_a_linked_capsule_path_is_unchanged(tmp_path: Path) -> None:
    """Only links *inside* the capsule are refused; ``runs/latest`` still works."""
    cap = _tricky(tmp_path / "tricky")
    alias = tmp_path / "latest"
    os.symlink(cap, alias)
    assert capsule_merkle_root(alias) == GOLDEN["v2"]


def test_a_fifo_in_the_capsule_is_left_out_without_being_opened(tmp_path: Path) -> None:
    """The old walk skipped a FIFO (``is_file`` is false); the root is still unchanged."""
    cap = _tricky(tmp_path / "tricky")
    fifo = cap / "a" / "pipe"
    os.mkfifo(fifo)
    assert _bounded(lambda: capsule_root(cap, version=2), fifo=fifo) == GOLDEN["v2"]
    assert _bounded(lambda: capsule_root(cap, version=1), fifo=fifo) == GOLDEN["v1"]


# -- links: refused, never hashed --------------------------------------------------


@pytest.mark.parametrize("variant", sorted(VARIANTS))
@pytest.mark.parametrize("version", [1, 2])
def test_any_link_refuses_the_root(
    capsule: Path, secret: Path, variant: str, version: int
) -> None:
    VARIANTS[variant](capsule, secret)
    with pytest.raises(CapsuleLinkError) as info:
        capsule_root(capsule, version=version)
    assert info.value.links, "the refusal names the link"
    assert CANARY.decode() not in str(info.value)


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_no_root_derived_subject_is_computed_over_a_link(
    capsule: Path, secret: Path, variant: str
) -> None:
    VARIANTS[variant](capsule, secret)
    for compute in (
        capsule_merkle_root,
        capsule_subject_digest,
        annotation_subject_digest,
        lambda c: capsule_root_matches(c, GOLDEN["v2"]),
    ):
        with pytest.raises(CapsuleLinkError):
            compute(capsule)


def test_a_link_hidden_behind_the_exclude_filter_still_refuses(
    capsule: Path, secret: Path
) -> None:
    """``exclude`` drops files from the hash, never a link from the check."""
    os.symlink(secret, capsule / COMMENTS_FILENAME)
    with pytest.raises(CapsuleLinkError):
        capsule_subject_digest(capsule)


def test_a_link_planted_after_the_listing_is_refused_not_followed(
    capsule: Path, secret: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_list = merkle.list_capsule

    def racing_list(root: Path, **kwargs: Any) -> Any:
        listing = real_list(root, **kwargs)
        victim = listing.root / "inputs" / "data.txt"
        victim.unlink()
        os.symlink(secret, victim)
        return listing

    monkeypatch.setattr(merkle, "list_capsule", racing_list)
    with pytest.raises(CapsuleLinkError):
        capsule_merkle_root(capsule)


def test_a_file_swapped_for_a_fifo_after_the_listing_never_blocks(
    capsule: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_list = merkle.list_capsule
    fifo = capsule / "inputs" / "data.txt"

    def racing_list(root: Path, **kwargs: Any) -> Any:
        listing = real_list(root, **kwargs)
        fifo.unlink()
        os.mkfifo(fifo)
        return listing

    monkeypatch.setattr(merkle, "list_capsule", racing_list)
    with pytest.raises(CapsuleSpecialFileError):
        _bounded(lambda: capsule_merkle_root(capsule), fifo=fifo)


# -- library callers turn the refusal into their own named error -------------------


def test_session_bundle_member_root_refuses_a_link(capsule: Path, secret: Path) -> None:
    from novafabric.session.bundle import SessionBundleError, _merkle_root

    _file_link(capsule, secret)
    with pytest.raises(SessionBundleError, match="symlink"):
        _merkle_root(capsule)


def test_evidence_bundle_root_refusal_is_a_capsule_validation_error(
    capsule: Path, secret: Path
) -> None:
    from novafabric.evidence.bundle import (
        CapsuleSymlinkError,
        CapsuleValidationError,
        _capsule_hash,
    )

    _file_link(capsule, secret)
    with pytest.raises(CapsuleSymlinkError) as info:
        _capsule_hash(capsule)
    assert isinstance(info.value, CapsuleValidationError)
    assert info.value.links


def test_baseline_pin_refuses_a_linked_capsule(capsule: Path, secret: Path) -> None:
    from novafabric.assure.baseline import BaselineError, baseline_run_from_capsule

    _file_link(capsule, secret)
    with pytest.raises(BaselineError, match="symlink"):
        baseline_run_from_capsule(RUN_ID, capsule)


def test_experiment_item_with_a_linked_capsule_is_an_error_not_a_score(
    tmp_path: Path, secret: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real capture whose capsule gains a link before scoring: item ERROR, no score."""
    from novafabric.eval import experiment_runner
    from novafabric.eval.experiment import ExperimentTarget, ItemRunStatus, TargetKind
    from novafabric.eval.experiment_dataset import load_dataset

    real_write_facet = experiment_runner.write_facet

    def facet_then_link(capsule_dir: Path, facet: Any) -> Any:
        out = real_write_facet(capsule_dir, facet)
        if not (capsule_dir / "outputs-stolen").is_symlink():
            os.symlink(secret, capsule_dir / "outputs-stolen")
        return out

    monkeypatch.setattr(experiment_runner, "write_facet", facet_then_link)
    items = tmp_path / "items.jsonl"
    items.write_text(json.dumps({"item_id": "a", "input": "hi", "expected": "hi"}) + "\n")
    experiment = experiment_runner.run_experiment(
        load_dataset(items),
        [sys.executable, "-c", "print('{input}')"],
        target=ExperimentTarget(kind=TargetKind.AGENT, ref="stub@1"),
        runs_dir=tmp_path / "runs",
    )
    [run] = experiment.runs
    assert run.status is ItemRunStatus.ERROR
    assert run.score_ids == []
    assert not (Path(run.capsule_ref) / "scores.jsonl").exists()


# -- CLI surfaces: one error line, exit 2, nothing written --------------------------

runner = CliRunner()


def _cli(args: list[str]) -> Any:
    from novafabric.cli.main import app

    return runner.invoke(app, args)


@pytest.fixture()
def iso_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "nfhome"
    home.mkdir()
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(home / "audit.jsonl"))
    monkeypatch.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)
    return home


def _assert_refused(result: Any) -> None:
    assert isinstance(result.exception, SystemExit), result.exception  # never a traceback
    assert result.exit_code == 2, result.output
    assert "symlink" in result.output
    assert CANARY.decode() not in result.output


def test_cli_bind_custody_refuses_a_linked_capsule(
    capsule: Path, secret: Path, iso_home: Path
) -> None:
    clean = _cli(["evidence", "bind-custody", str(capsule)])
    assert clean.exit_code == 0, clean.output
    _file_link(capsule, secret)
    _assert_refused(_cli(["evidence", "bind-custody", str(capsule)]))


def test_cli_comment_add_refuses_a_linked_capsule(
    capsule: Path, secret: Path, iso_home: Path
) -> None:
    _dir_link(capsule, secret)
    _assert_refused(_cli(["comment", "add", "--subject", str(capsule), "--body", "note"]))
    assert not (capsule / COMMENTS_FILENAME).exists()


def test_cli_score_submit_refuses_a_linked_capsule(
    capsule: Path, secret: Path, iso_home: Path
) -> None:
    subject = capsule_merkle_root(capsule)
    _file_link(capsule, secret)
    result = _cli([
        "score", "submit", "--capsule", str(capsule), "--name", "m", "--value", "1",
        "--evaluator", "ci://x", "--subject", subject, "--subject-kind", "capsule",
        "--eval-card", "sha256:" + "6" * 64,
    ])
    assert isinstance(result.exception, SystemExit), result.exception
    assert result.exit_code == 2, result.output
    error = json.loads(result.stderr.strip().splitlines()[-1])
    assert error["error"] == "capsule_unusable"
    assert not (capsule / "scores.jsonl").exists()


def test_cli_baseline_pin_refuses_a_linked_capsule(
    capsule: Path, secret: Path, iso_home: Path
) -> None:
    _file_link(capsule, secret)
    result = _cli([
        "assure-baseline", "pin", "--capsule", str(capsule), "--run", RUN_ID,
        "--id", "b1", "--criterion", "goal", "--pinned-at", "2026-10-23T00:00:00Z",
    ])
    assert isinstance(result.exception, SystemExit), result.exception
    assert result.exit_code == 2, result.output
    assert "symlink" in result.output


# -- serve surfaces: 422, no bundle, no score ---------------------------------------

SERVER_TOKEN = "server-token-0123456789abcdef"
HEADERS = {"host": "127.0.0.1:4321"}


@pytest.fixture()
def serve_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Any, Path, Path]]:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from novafabric.serve.app import create_app

    home = tmp_path / "home"
    (home / ".novafabric").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home / ".novafabric"))
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(home / "dashboard-audit.jsonl"))
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(home / "chained-audit.jsonl"))
    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_DIR", str(home / "evidence"))
    monkeypatch.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)
    runs = tmp_path / "runs"
    runs.mkdir()
    app = create_app(
        token=SERVER_TOKEN, capsule_dir=runs, db_path=tmp_path / "registry.db",
        static_mounted_by_caller=True,
    )
    with TestClient(app) as client:
        yield client, runs, home


def _captured(runs: Path) -> Path:
    from novafabric.capture.orchestrator import CaptureOrchestrator

    return CaptureOrchestrator(base_dir=runs).run(command=[sys.executable, "-c", "pass"]).capsule_dir


def _cart_item(ref: str) -> dict[str, str]:
    return {"kind": "run", "ref": ref, "added_at": "2026-10-23T10:00:00Z"}


@pytest.mark.parametrize("where", ["nested", "top-level"])
@pytest.mark.parametrize("accept_unresolved", [False, True])
def test_serve_evidence_cart_refuses_a_linked_capsule(
    serve_env: tuple[Any, Path, Path], secret: Path, accept_unresolved: bool, where: str
) -> None:
    """Not a 409 "unresolved" item and never a bundle that records it as an omission.

    A nested link is refused by the Merkle root; a top-level one already by run
    resolution (403). Either way the cart answers 422 and writes no bundle.
    """
    client, runs, home = serve_env
    clean, linked = _captured(runs), _captured(runs)
    parent = linked / "nested" if where == "nested" else linked
    parent.mkdir(exist_ok=True)
    os.symlink(secret, parent / "inputs-stolen")
    r = client.post(
        f"/api/evidence/cart/export?token={SERVER_TOKEN}",
        json={
            "items": [_cart_item(clean.name), _cart_item(linked.name)],
            "confirmed": True,
            "accept_unresolved": accept_unresolved,
        },
        headers=HEADERS,
    )
    assert r.status_code == 422, r.text
    assert "inputs-stolen" in r.json()["detail"]
    assert CANARY.decode() not in r.text
    assert str(secret) not in r.text
    evidence = home / "evidence"
    assert not evidence.exists() or not any(evidence.glob("*.zip"))


def test_serve_evidence_cart_still_exports_clean_capsules(
    serve_env: tuple[Any, Path, Path],
) -> None:
    client, runs, _ = serve_env
    a, b = _captured(runs), _captured(runs)
    r = client.post(
        f"/api/evidence/cart/export?token={SERVER_TOKEN}",
        json={"items": [_cart_item(a.name), _cart_item(b.name)], "confirmed": True},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    assert r.json()["capsule_count"] == 2


def test_serve_score_submission_refuses_a_linked_capsule(
    serve_env: tuple[Any, Path, Path], secret: Path
) -> None:
    client, runs, _ = serve_env
    cap = _captured(runs)
    subject = capsule_merkle_root(cap)
    (cap / "nested").mkdir(exist_ok=True)
    os.symlink(secret, cap / "nested" / "inputs-stolen")
    r = client.post(
        f"/api/runs/{cap.name}/scores?token={SERVER_TOKEN}",
        json={
            "name": "m", "value": 1.0, "value_type": "numeric", "source": "code",
            "evaluator_id": "ci://x", "subject": subject, "subject_kind": "capsule",
            "eval_card_digest": "sha256:" + "6" * 64,
        },
        headers=HEADERS,
    )
    assert r.status_code == 422, r.text
    assert CANARY.decode() not in r.text
    assert not (cap / "scores.jsonl").exists()


def test_serve_maps_any_unhandled_capsule_refusal_to_422(
    serve_env: tuple[Any, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.serve import authz

    client, _, _ = serve_env
    monkeypatch.setitem(
        authz.ROUTE_SCOPES, ("GET", "/api/__test_capsule_refusal"), authz.Scope.read
    )

    def boom() -> None:
        raise CapsuleLinkError("capsule /srv/x holds 1 symlink(s)", links=("inputs/x",))

    client.app.add_api_route("/api/__test_capsule_refusal", boom)
    r = client.get(f"/api/__test_capsule_refusal?token={SERVER_TOKEN}", headers=HEADERS)
    assert r.status_code == 422, r.text
    assert "inputs/x" in r.json()["detail"]
    assert "/srv/x" not in r.text, "no server path in the detail"
