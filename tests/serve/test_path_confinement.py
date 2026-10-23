"""Request-supplied filesystem paths in ``nova serve`` are confined to allowlisted roots.

Several dashboard endpoints take a server-side path in the request body. The
guard was a *denylist* of system directories, so anything else was reachable:
an ``operate``-scoped token could make the server write an evidence bundle over
any file the server user owns, or read a "signing key" from anywhere. The guard
is now an allowlist (``novafabric.serve.paths``):

* writes: ``$NOVAFABRIC_HOME/exports``, ``$NOVAFABRIC_HOME/evidence``;
* reads: ``$NOVAFABRIC_HOME``, the server's capsule dir, and the write roots;
* plus operator-configured ``NOVAFABRIC_SERVE_PATH_ROOTS``.

Every route that accepts a filesystem path from the request is exercised against
absolute paths outside the roots, ``..`` traversal, symlink escapes, a NUL byte,
and a legitimate in-root path.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402
from novafabric.serve.paths import (  # noqa: E402
    PATH_ROOTS_ENV,
    InvalidRequestPathError,
    PathOutsideRootsError,
    confine,
    read_roots,
    write_roots,
)

TOKEN = "confine-token-0123456789abcdef"
HJ = {"host": "127.0.0.1:4321", "content-type": "application/json"}
P = {"token": TOKEN}


@dataclass
class Env:
    home: Path
    capsules: Path
    outside: Path
    client: TestClient

    @property
    def exports(self) -> Path:
        return self.home / "exports"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    home = tmp_path / "home" / ".novafabric"
    home.mkdir(parents=True)
    (home / "exports").mkdir()
    capsules = tmp_path / "capsules"
    capsules.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.txt").write_text("do not touch\n")
    (outside / "capsule").mkdir()
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(home / "audit.jsonl"))
    monkeypatch.delenv("NOVAFABRIC_EVIDENCE_DIR", raising=False)
    monkeypatch.delenv(PATH_ROOTS_ENV, raising=False)
    monkeypatch.delenv("NOVAFABRIC_SERVE_ALLOW_ANY_PATH", raising=False)
    app = create_app(token=TOKEN, capsule_dir=capsules, db_path=None, static_dir=None)
    with TestClient(app) as c:
        yield Env(home=home, capsules=capsules, outside=outside, client=c)


# ---------------------------------------------------------------------------
# confine() — the primitive
# ---------------------------------------------------------------------------


def test_confine_accepts_a_path_inside_a_root(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    assert confine(str(root / "a" / "b.zip"), [root]) == root / "a" / "b.zip"
    assert confine(root, [root]) == root


def test_confine_resolves_relative_paths_against_the_first_root(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    assert confine("sub/x.zip", [root]) == root / "sub" / "x.zip"


@pytest.mark.parametrize(
    "bad",
    ["/etc/passwd", "/root/.ssh/authorized_keys", "/tmp/elsewhere", "/"],
)
def test_confine_rejects_absolute_paths_outside(tmp_path: Path, bad: str) -> None:
    with pytest.raises(PathOutsideRootsError):
        confine(bad, [(tmp_path / "root").resolve()])


@pytest.mark.parametrize(
    "bad",
    ["../x", "a/../../x", "a/../b", "..", "./../x"],
)
def test_confine_rejects_every_dotdot_segment(tmp_path: Path, bad: str) -> None:
    """Even ``a/../b``, which would land back inside, is refused: no '..' at all."""
    root = (tmp_path / "root").resolve()
    root.mkdir()
    with pytest.raises(PathOutsideRootsError):
        confine(bad, [root])
    with pytest.raises(PathOutsideRootsError):
        confine(str(root) + "/" + bad, [root])


def test_confine_rejects_a_symlink_that_escapes(tmp_path: Path) -> None:
    root = (tmp_path / "root").resolve()
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    (root / "filelink").symlink_to(outside / "f.txt")
    with pytest.raises(PathOutsideRootsError):
        confine(str(root / "link" / "x.zip"), [root])
    with pytest.raises(PathOutsideRootsError):
        confine(str(root / "filelink"), [root])


def test_confine_accepts_a_symlink_that_stays_inside(tmp_path: Path) -> None:
    root = (tmp_path / "root").resolve()
    (root / "real").mkdir(parents=True)
    (root / "alias").symlink_to(root / "real")
    assert confine(str(root / "alias" / "x"), [root]) == root / "real" / "x"


def test_a_symlink_loop_never_resolves_outside(tmp_path: Path) -> None:
    """Python versions differ on whether a loop raises; either way nothing escapes."""
    root = (tmp_path / "root").resolve()
    root.mkdir()
    (root / "a").symlink_to(root / "b")
    (root / "b").symlink_to(root / "a")
    try:
        got = confine(str(root / "a" / "x"), [root])
    except InvalidRequestPathError:
        return
    assert got.is_relative_to(root)


def test_confine_rejects_a_sibling_with_a_shared_prefix(tmp_path: Path) -> None:
    """``/x/root-evil`` must not pass as inside ``/x/root``."""
    root = (tmp_path / "root").resolve()
    with pytest.raises(PathOutsideRootsError):
        confine(str(tmp_path / "root-evil" / "x"), [root])


@pytest.mark.parametrize("bad", ["", "   ", "a\x00b"])
def test_confine_rejects_unusable_input(tmp_path: Path, bad: str) -> None:
    with pytest.raises(InvalidRequestPathError):
        confine(bad, [tmp_path.resolve()])


@pytest.mark.parametrize("bad", [123, None, ["/x"], b"/bytes"])
def test_confine_rejects_non_string_input(tmp_path: Path, bad: Any) -> None:
    """A JSON body can carry any type in a path field; none of them may crash the route."""
    with pytest.raises(InvalidRequestPathError):
        confine(bad, [tmp_path.resolve()])


def test_a_non_string_path_field_is_a_400_not_a_500(env: Env) -> None:
    res = env.client.post(
        "/api/capsule-migrate", params=P, headers=HJ, json={"source": 123, "output": "x"}
    )
    assert res.status_code == 400, res.text


def test_operator_roots_skip_unusable_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.serve.paths import extra_roots

    good = tmp_path / "good"
    monkeypatch.setenv(PATH_ROOTS_ENV, os.pathsep.join(["", "  ", "rel", str(good)]))
    assert extra_roots() == (good,)


def test_confine_with_no_roots_refuses() -> None:
    with pytest.raises(PathOutsideRootsError):
        confine("/anything", [])


def test_tilde_expands_and_is_still_confined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    root = (tmp_path / "userhome" / ".novafabric").resolve()
    assert confine("~/.novafabric/x", [root]) == root / "x"
    with pytest.raises(PathOutsideRootsError):
        confine("~/.ssh/id_ed25519", [root])


# ---------------------------------------------------------------------------
# The root sets
# ---------------------------------------------------------------------------


def test_write_roots_are_narrower_than_home(env: Env) -> None:
    """A write root covering all of $NOVAFABRIC_HOME would expose the token store."""
    home = env.home.resolve()
    roots = write_roots()
    assert home not in roots
    assert home / "exports" in roots
    assert home / "evidence" in roots
    for secret in (".serve-token", "tokens.jsonl", "audit.jsonl", "registry.db"):
        with pytest.raises(PathOutsideRootsError):
            confine(str(home / secret), roots)


def test_read_roots_cover_home_and_the_capsule_dir(env: Env) -> None:
    roots = read_roots(env.capsules)
    assert env.home.resolve() in roots
    assert env.capsules.resolve() in roots


def test_operator_roots_extend_both_lists(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PATH_ROOTS_ENV, os.pathsep.join([str(env.outside), "relative/ignored", ""]))
    assert env.outside.resolve() in write_roots()
    assert env.outside.resolve() in read_roots(env.capsules)
    assert all(r.is_absolute() for r in write_roots())


def test_the_old_any_path_opt_out_no_longer_opens_anything(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVAFABRIC_SERVE_ALLOW_ANY_PATH", "1")
    with pytest.raises(PathOutsideRootsError):
        confine("/etc/passwd", read_roots(env.capsules))


# ---------------------------------------------------------------------------
# Every route that takes a path from the request body
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PathField:
    """One request field that names a server-side path."""

    route: str
    field: str
    kind: str  # "read" | "write"
    body: Callable[[str], dict[str, Any]]


def _with(**fixed: Any) -> Callable[[str], dict[str, Any]]:
    def build(field: str) -> dict[str, Any]:
        return dict(fixed)

    return build


PATH_FIELDS: list[PathField] = [
    PathField("/api/evidence/run-x", "output_path", "write", _with(confirmed=True)),
    PathField("/api/evidence/run-x", "key_path", "read", _with(confirmed=True)),
    PathField(
        "/api/policy/sign",
        "key_path",
        "read",
        _with(proposer_subjects=["a"], approver_subjects=["b"], cert_path="c.pem"),
    ),
    PathField(
        "/api/policy/sign",
        "cert_path",
        "read",
        _with(proposer_subjects=["a"], approver_subjects=["b"], key_path="k.pem"),
    ),
    PathField(
        "/api/policy/sign",
        "db_path",
        "write",
        _with(proposer_subjects=["a"], approver_subjects=["b"], key_path="k.pem", cert_path="c.pem"),
    ),
    PathField("/api/capsule-migrate", "source", "read", _with(output="migrated")),
    PathField("/api/capsule-migrate", "output", "write", _with(source="exports")),
    PathField("/api/lineage/import", "capsule_path", "read", _with()),
    PathField("/api/kg/ingest", "capsule_path", "read", _with()),
    PathField("/api/kg/detect", "capsule_path", "read", _with()),
    PathField(
        "/api/kg/attack-path", "capsule_path", "read", _with(from_entity="a:b", to_entity="c:d")
    ),
    PathField("/api/kg/blast-radius", "capsule_path", "read", _with(entity="a:b")),
    PathField("/api/kg/ingest-all", "capsule_base", "read", _with()),
    PathField("/api/policy/test", "bundle_path", "read", _with()),
    PathField("/api/admin/rebuild-metadata-db", "target_db", "write", _with(confirmed=True)),
]
_IDS = [f"{f.route}:{f.field}" for f in PATH_FIELDS]


def _post(env: Env, spec: PathField, value: str) -> Any:
    body = spec.body(spec.field)
    body[spec.field] = value
    return env.client.post(spec.route, params=P, headers=HJ, json=body)


def _attacks(env: Env) -> dict[str, str]:
    escape = env.home / "exports" / "escape"
    if not escape.exists():
        escape.symlink_to(env.outside)
    return {
        "absolute-outside": str(env.outside / "victim.txt"),
        "absolute-system": "/etc/passwd",
        "ssh-dir": str(Path("~").expanduser() / ".ssh" / "authorized_keys"),
        "dotdot-relative": "../../outside/victim.txt",
        "dotdot-absolute": str(env.home / "exports" / ".." / ".." / ".." / "outside" / "victim.txt"),
        "symlink-escape": str(escape / "victim.txt"),
        "token-store": str(env.home / "tokens.jsonl"),
    }


@pytest.mark.parametrize("spec", PATH_FIELDS, ids=_IDS)
def test_every_path_field_refuses_every_escape(env: Env, spec: PathField) -> None:
    for name, value in _attacks(env).items():
        if name == "token-store" and spec.kind == "read":
            continue  # $NOVAFABRIC_HOME is a read root by design
        res = _post(env, spec, value)
        assert res.status_code == 403, f"{name}: {res.status_code} {res.text}"
    assert (env.outside / "victim.txt").read_text() == "do not touch\n"


@pytest.mark.parametrize("spec", PATH_FIELDS, ids=_IDS)
def test_every_path_field_rejects_a_nul_byte(env: Env, spec: PathField) -> None:
    res = _post(env, spec, "exports/a\x00b")
    assert res.status_code == 400, res.text


@pytest.mark.parametrize("spec", PATH_FIELDS, ids=_IDS)
def test_every_path_field_accepts_a_path_inside_its_root(env: Env, spec: PathField) -> None:
    """The guard refuses escapes, not use: an in-root path gets past it."""
    value = str(env.exports / "inside")
    res = _post(env, spec, value)
    # Whatever the route answers next (404 no such capsule, 400 key not found,
    # 200 ok:false ...), it is not the confinement guard refusing the path.
    assert res.status_code != 403, res.text
    for refusal in ("outside the directories", "'..' segments", "NUL byte", "non-empty"):
        assert refusal not in res.text, res.text


@pytest.mark.parametrize(
    "spec", [f for f in PATH_FIELDS if f.kind == "write"], ids=lambda f: f"{f.route}:{f.field}"
)
def test_writes_refuse_the_rest_of_home(env: Env, spec: PathField) -> None:
    for name in (".serve-token", "tokens.jsonl", "audit.jsonl", "keys/local-key.pem"):
        res = _post(env, spec, str(env.home / name))
        assert res.status_code == 403, f"{name}: {res.text}"


def test_an_operator_root_admits_a_path_the_default_refuses(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = next(f for f in PATH_FIELDS if f.route == "/api/capsule-migrate" and f.field == "source")
    assert _post(env, spec, str(env.outside / "capsule")).status_code == 403
    monkeypatch.setenv(PATH_ROOTS_ENV, str(env.outside))
    assert _post(env, spec, str(env.outside / "capsule")).status_code != 403


def test_capsule_path_bare_run_id_still_resolves_in_the_capsule_dir(env: Env) -> None:
    """A bare run id keeps working, and a bare '..' is not a run id."""
    res = env.client.post("/api/kg/detect", params=P, headers=HJ, json={"capsule_path": "nope"})
    assert res.status_code == 404
    for route in ("/api/lineage/import", "/api/kg/detect", "/api/kg/ingest"):
        res = env.client.post(route, params=P, headers=HJ, json={"capsule_path": ".."})
        assert res.status_code in (400, 403), f"{route}: {res.text}"


def test_the_default_evidence_output_still_works_without_a_path(env: Env) -> None:
    """Server-chosen defaults are not request input and are not confined."""
    res = env.client.post(
        "/api/evidence/run-x", params=P, headers=HJ, json={"confirmed": True}
    )
    assert res.status_code == 404  # no such capsule; the default path was not refused
