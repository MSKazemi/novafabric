"""``nova serve`` never returns the target of a symlink planted in a capsule.

The adversarial review read ``~/.ssh/id_rsa`` with a **read-scope** token
through ``GET /api/runs/{id}/file/inputs/stolen`` and
``POST /api/compliance/export/rocrate``. Both followed a link someone planted
in the capsule. A planted ``capsule.yaml`` or ``redaction-proof.json`` link
went further: ``GET /api/runs/{id}``, ``/redaction-proof``, ``/scan-secrets``
and ``/validate`` returned the parsed target.

Now:

* the file route opens through :mod:`novafabric._capsule_fs` (``O_NOFOLLOW`` on
  every path component) and answers 404 for a link or a special file;
* a run whose capsule has a symlink at its top level is refused (403) by every
  ``{run_id}`` route, because they all resolve the run through
  ``_resolve_capsule``;
* the run listing never reads a linked ``capsule.yaml``, and the ``inputs``/
  ``outputs`` listings leave links out, so their target's size does not leak.
"""

from __future__ import annotations

import base64
import io
import os
import sys
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve import token_store  # noqa: E402
from novafabric.serve.app import create_app  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")

SERVER_TOKEN = "link-read-token-0123456789abcdef"
READ_SECRET = "issued-read-token-abcdefghijklmnop"
CANARY = "CANARY-SSH-PRIVATE-KEY-7f3a"
RUN = "01J9ZZSERVELINKREADS000001"
HEADERS = {"host": "127.0.0.1:4321", "authorization": f"Bearer {READ_SECRET}"}


@dataclass
class Env:
    client: TestClient
    capsule: Path
    secret: Path


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(home / "audit.jsonl"))
    secret = tmp_path / "outside" / "id_rsa"
    secret.parent.mkdir()
    # A YAML/JSON-parseable secret, like a kubeconfig or a cloud credential file.
    secret.write_text(f'{{"canary": "{CANARY}", "run_id": "{RUN}"}}\n')
    capsules = tmp_path / "capsules"
    cap = capsules / RUN
    (cap / "inputs").mkdir(parents=True)
    (cap / "outputs").mkdir()
    (cap / "capsule.yaml").write_text(yaml.safe_dump({"run_id": RUN, "status": "success"}))
    (cap / "trace.jsonl").write_text('{"span": 1}\n')
    (cap / "inputs" / "data.txt").write_text("input\n")
    token_store.issue("test-read", READ_SECRET, "read")
    app = create_app(token=SERVER_TOKEN, capsule_dir=capsules, db_path=None, static_dir=None)
    with TestClient(app) as c:
        yield Env(client=c, capsule=cap, secret=secret)


def _leaks(text: str) -> bool:
    return CANARY in text


# -- the file route ------------------------------------------------------------


def test_file_route_serves_a_regular_file(env: Env) -> None:
    r = env.client.get(f"/api/runs/{RUN}/file/inputs/data.txt", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["content"] == "input\n"


@pytest.mark.parametrize(
    ("plant", "rel"),
    [
        (lambda c, s: os.symlink(s, c / "inputs" / "stolen"), "inputs/stolen"),
        (lambda c, s: os.symlink(s, c / "inputs" / "stolen.jsonl"), "inputs/stolen.jsonl"),
        (lambda c, s: os.symlink(s.parent / "gone", c / "inputs" / "gone"), "inputs/gone"),
        (lambda c, s: os.symlink(c / "trace.jsonl", c / "outputs" / "t.jsonl"), "outputs/t.jsonl"),
    ],
    ids=["outside", "outside-jsonl", "dangling", "inside"],
)
def test_file_route_never_follows_a_link_in_inputs_or_outputs(
    env: Env, plant: Callable[[Path, Path], None], rel: str
) -> None:
    plant(env.capsule, env.secret)
    r = env.client.get(f"/api/runs/{RUN}/file/{rel}", headers=HEADERS)
    assert r.status_code == 404
    assert not _leaks(r.text)


def test_file_route_never_follows_a_symlinked_subdirectory(env: Env) -> None:
    (env.capsule / "outputs").rmdir()
    os.symlink(env.secret.parent, env.capsule / "outputs")
    r = env.client.get(f"/api/runs/{RUN}/file/outputs/id_rsa", headers=HEADERS)
    assert r.status_code in (403, 404)
    assert not _leaks(r.text)


def test_file_route_refuses_a_fifo_without_hanging(env: Env) -> None:
    os.mkfifo(env.capsule / "inputs" / "pipe")
    r = env.client.get(f"/api/runs/{RUN}/file/inputs/pipe", headers=HEADERS)
    assert r.status_code == 404


def test_file_route_never_follows_a_link_planted_after_the_existence_check(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old route did ``exists()``/``is_file()`` and only then opened the path."""
    from novafabric import _capsule_fs

    real_open = _capsule_fs._open_regular_fd

    def racing(root: Path, rel: str, operation: str, error: type) -> int:  # type: ignore[type-arg]
        target = Path(root) / rel
        target.unlink()
        os.symlink(env.secret, target)
        return real_open(root, rel, operation, error)

    monkeypatch.setattr(_capsule_fs, "_open_regular_fd", racing)
    r = env.client.get(f"/api/runs/{RUN}/file/inputs/data.txt", headers=HEADERS)
    assert r.status_code == 404
    assert not _leaks(r.text)


# -- a top-level link refuses the whole run --------------------------------------


@pytest.mark.parametrize(
    ("name", "method", "path"),
    [
        ("notes.txt", "GET", "/api/runs/{run}/file/notes.txt"),
        ("capsule.yaml", "GET", "/api/runs/{run}"),
        ("redaction-proof.json", "GET", "/api/runs/{run}/redaction-proof"),
        ("redaction-proof.json", "GET", "/api/runs/{run}/scan-secrets"),
        ("capsule.yaml", "POST", "/api/runs/{run}/validate"),
        ("model-calls.jsonl", "GET", "/api/runs/{run}"),
    ],
)
def test_a_top_level_link_refuses_every_run_route(
    env: Env, name: str, method: str, path: str
) -> None:
    target = env.capsule / name
    if target.exists():
        target.unlink()
    os.symlink(env.secret, target)
    r = env.client.request(method, path.format(run=RUN), headers=HEADERS)
    assert r.status_code in (403, 404), r.text[:200]
    assert not _leaks(r.text)


def test_rocrate_export_refuses_a_linked_capsule(env: Env) -> None:
    os.symlink(env.secret, env.capsule / "inputs" / "stolen")
    r = env.client.post(
        "/api/compliance/export/rocrate", headers=HEADERS, json={"run_id": RUN}
    )
    assert r.status_code == 422, r.text[:200]
    assert not _leaks(r.text)


def test_rocrate_export_refuses_a_top_level_link(env: Env) -> None:
    os.symlink(env.secret, env.capsule / "notes.txt")
    r = env.client.post(
        "/api/compliance/export/rocrate", headers=HEADERS, json={"run_id": RUN}
    )
    assert r.status_code in (403, 422), r.text[:200]
    assert not _leaks(r.text)


def test_rocrate_export_of_a_clean_capsule_still_works(env: Env) -> None:
    r = env.client.post(
        "/api/compliance/export/rocrate", headers=HEADERS, json={"run_id": RUN}
    )
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(r.json()["zip_base64"]))) as zf:
        assert "trace.jsonl" in zf.namelist()


# -- listings -------------------------------------------------------------------


def test_run_listing_never_reads_a_linked_manifest(env: Env, tmp_path: Path) -> None:
    other = env.capsule.parent / "01J9ZZSERVELINKREADS000002"
    other.mkdir()
    os.symlink(env.secret, other / "capsule.yaml")
    r = env.client.get("/api/runs", headers=HEADERS)
    assert r.status_code == 200
    assert not _leaks(r.text)


def test_inputs_listing_leaves_links_out(env: Env) -> None:
    os.symlink(env.secret, env.capsule / "inputs" / "stolen")
    r = env.client.get(f"/api/runs/{RUN}", headers=HEADERS)
    assert r.status_code == 200
    names = [f["name"] for f in r.json()["inputs"]]
    assert names == ["data.txt"]
