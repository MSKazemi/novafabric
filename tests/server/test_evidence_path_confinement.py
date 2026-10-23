"""``nova server``: a request id can never name a path outside the server's own directories.

Review server-01 (P0, reproduced): ``POST /v0/evidence`` needed only the
``writer`` role, joined an absolute or ``..`` ``run_id`` onto the capsule
directory, and wrote the bundle to a client-chosen ``output_path``. A writer
token overwrote any file the server could write (the victim then started with
``PK\\x03\\x04``) and bundled any directory holding a ``capsule.yaml``. The
signing key it auto-generated was left mode ``0o664``.

Every case here is the attack first: absolute, ``..``, embedded separators, NUL,
a symlinked capsule entry, a client ``output_path``, a planted link at the
output name. Each must answer 400 (or 404 for a clean name that does not exist)
with the victim untouched and nothing written outside the evidence directory.
"""

from __future__ import annotations

import json
import stat
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import jwt
import pytest
import yaml

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric import _paths as nf_paths  # noqa: E402
from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.auth import JwksCache  # noqa: E402
from novafabric.server.config import OidcConfig, ServerConfig  # noqa: E402

RUN_ID = "01CONF00000000000000000001"
VICTIM_BYTES = b"precious server state\n"
_SPAN = "sha256:" + "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"


def _redaction_proof(run_id: str) -> dict[str, Any]:
    from novafabric.capture._ulid import new_ulid
    from novafabric.capture.secrets import recompute_chain_hash

    return recompute_chain_hash(
        {
            "schema_version": "0.1.0",
            "proof_id": new_ulid(),
            "capsule_run_id": run_id,
            "created_at": "2026-08-06T10:00:00+00:00",
            "scanner": {
                "name": "novafabric.secrets",
                "version": "0.2.0",
                "engine": "regex",
                "engine_version": "0.2.0",
            },
            "packs": [],
            "targets": [],
            "findings_count": {
                "total": 0,
                "by_severity": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            },
            "findings": [],
            "bytes_scanned": 0,
            "bytes_redacted": 0,
            "unsafe_skips": [],
        }
    )


def _write_capsule(parent: Path, dirname: str, run_id: str | None = None) -> Path:
    """A capsule the evidence builder accepts, so a 202 is the real bundle path."""
    run_id = run_id or dirname
    cdir = parent / dirname
    cdir.mkdir(parents=True)
    manifest = {
        "schema_version": "0.1.0",
        "novafabric_version": "0.6.12",
        "run_id": run_id,
        "created_at": "2026-08-06T10:00:00+00:00",
        "finished_at": "2026-08-06T10:00:01+00:00",
        "duration_ms": 1000,
        "command": ["python", "-c", "print('hi')"],
        "exit_code": 0,
        "status": "success",
        "capture_mode": "cli-wrapper",
        "model_call_count": 0,
        "tool_call_count": 0,
        "mutating_tool_count": 0,
    }
    (cdir / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    (cdir / "trace.jsonl").write_text(json.dumps({"span_id": "root", "span_digest": _SPAN}) + "\n")
    for name in ("model-calls.jsonl", "tool-calls.jsonl", "assets.jsonl"):
        (cdir / name).write_text("")
    (cdir / "env.lock").write_text("{}")
    (cdir / "redaction-proof.json").write_text(json.dumps(_redaction_proof(run_id)))
    (cdir / "inputs").mkdir()
    (cdir / "outputs").mkdir()
    return cdir


@pytest.fixture(autouse=True)
def _fresh_bundle_registry() -> Any:
    """The route keeps an in-process bundle registry; start each test without one."""
    from novafabric.server.routes import evidence

    with evidence._BUNDLES_LOCK:
        evidence._BUNDLES.clear()
    yield
    with evidence._BUNDLES_LOCK:
        evidence._BUNDLES.clear()


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """The server's store and evidence dir, plus an 'outside' capsule and a victim file."""
    store = tmp_path / "store"
    store.mkdir()
    _write_capsule(store, RUN_ID)
    outside = tmp_path / "outside"
    _write_capsule(outside, "foreign")  # another tenant's capsule, outside the store
    victim = tmp_path / "victim.txt"
    victim.write_bytes(VICTIM_BYTES)
    evidence = tmp_path / "evidence"
    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_DIR", str(evidence))
    return {"root": tmp_path, "store": store, "outside": outside, "victim": victim,
            "evidence": evidence}


def _app(cfg: ServerConfig, store: Path) -> Any:
    from novafabric.server import deps

    app = create_app(cfg)
    app.dependency_overrides[deps.get_capsule_dir] = lambda: store
    return app


@pytest.fixture
def client(layout: dict[str, Path]) -> TestClient:
    cfg = ServerConfig(db_path=str(layout["root"] / "server.db"), insecure_no_auth=True)
    return TestClient(_app(cfg, layout["store"]), raise_server_exceptions=False)


def _snapshot(root: Path) -> dict[str, bytes]:
    """Every regular file under *root* except the server's own db, home and evidence dir."""
    own_home = nf_paths.nova_home().resolve()
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(("server.db", "evidence")) or not path.is_file():
            continue
        if path.resolve().is_relative_to(own_home):
            continue
        out[rel] = path.read_bytes()
    return out


def _assert_nothing_written(layout: dict[str, Path], before: dict[str, bytes]) -> None:
    assert layout["victim"].read_bytes() == VICTIM_BYTES
    assert _snapshot(layout["root"]) == before
    evidence = layout["evidence"]
    assert not evidence.exists() or list(evidence.iterdir()) == []


def _hostile_run_ids(layout: dict[str, Path]) -> list[str]:
    outside_capsule = layout["outside"] / "foreign"
    return [
        str(outside_capsule),  # absolute: replaced capsule_dir in the old join
        "../outside/foreign",
        f"{RUN_ID}/../../outside/foreign",
        "..\\outside\\foreign",
        f"{RUN_ID}\x00",
        "..",
        ".",
        "/",
    ]


class TestEvidenceExportConfinement:
    def test_hostile_run_ids_are_400_and_write_nothing(
        self, client: TestClient, layout: dict[str, Path]
    ) -> None:
        before = _snapshot(layout["root"])
        for run_id in _hostile_run_ids(layout):
            resp = client.post("/v0/evidence", json={"run_id": run_id})
            assert resp.status_code == 400, (run_id, resp.text)
            assert "run_id" in resp.json()["error"]["message"]
        _assert_nothing_written(layout, before)

    def test_non_string_run_id_is_400(self, client: TestClient, layout: dict[str, Path]) -> None:
        for run_id in (["a"], {"x": 1}, 7):
            resp = client.post("/v0/evidence", json={"run_id": run_id})
            assert resp.status_code == 400, (run_id, resp.text)

    def test_an_encoded_separator_is_a_literal_name_not_a_path(
        self, client: TestClient, layout: dict[str, Path]
    ) -> None:
        """A JSON body is not URL-decoded: ``%2F`` stays three characters of one name."""
        before = _snapshot(layout["root"])
        resp = client.post("/v0/evidence", json={"run_id": "%2e%2e%2Foutside%2Fforeign"})
        assert resp.status_code == 404, resp.text
        _assert_nothing_written(layout, before)

    def test_a_symlinked_capsule_entry_is_not_followed(
        self, client: TestClient, layout: dict[str, Path]
    ) -> None:
        (layout["store"] / "linked").symlink_to(layout["outside"] / "foreign")
        before = _snapshot(layout["root"])
        resp = client.post("/v0/evidence", json={"run_id": "linked"})
        assert resp.status_code == 400, resp.text
        assert "symbolic link" in resp.json()["error"]["message"]
        _assert_nothing_written(layout, before)

    def test_output_path_is_refused_and_the_victim_is_untouched(
        self, client: TestClient, layout: dict[str, Path]
    ) -> None:
        """The reproduced P0: before the fix this was 202 and the victim became a ZIP."""
        before = _snapshot(layout["root"])
        for target in (str(layout["victim"]), str(layout["evidence"] / "x.zip"), "rel.zip"):
            resp = client.post(
                "/v0/evidence", json={"run_id": RUN_ID, "output_path": target}
            )
            assert resp.status_code == 400, (target, resp.text)
            assert "output_path" in resp.json()["error"]["message"]
        _assert_nothing_written(layout, before)

    def test_a_link_planted_at_the_bundle_name_is_refused(
        self, client: TestClient, layout: dict[str, Path]
    ) -> None:
        layout["evidence"].mkdir()
        planted = layout["evidence"] / f"{RUN_ID}.zip"
        planted.symlink_to(layout["victim"])
        resp = client.post("/v0/evidence", json={"run_id": RUN_ID})
        assert resp.status_code == 400, resp.text
        assert layout["victim"].read_bytes() == VICTIM_BYTES

    def test_a_clean_run_id_exports_into_the_evidence_dir_with_a_private_key(
        self, client: TestClient, layout: dict[str, Path]
    ) -> None:
        before = _snapshot(layout["root"])
        resp = client.post("/v0/evidence", json={"run_id": RUN_ID})
        assert resp.status_code == 202, resp.text
        bundle = Path(resp.json()["bundle_path"])
        assert bundle == layout["evidence"] / f"{RUN_ID}.zip"
        assert bundle.read_bytes()[:4] == b"PK\x03\x04"
        assert layout["victim"].read_bytes() == VICTIM_BYTES
        assert _snapshot(layout["root"]) == before

        key = nf_paths.local_key_path()
        assert key.is_file()
        assert stat.S_IMODE(key.stat().st_mode) == 0o600
        assert key.with_suffix(".pub.pem").is_file()
        # The key is reused on the next export, never regenerated.
        first = key.read_bytes()
        assert client.post("/v0/evidence", json={"run_id": RUN_ID}).status_code == 202
        assert key.read_bytes() == first


# --------------------------------------------------------------------------- #
# The same attack with a real writer credential (OIDC token, role ``writer``)
# --------------------------------------------------------------------------- #


def _rsa_key() -> Any:
    from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

    return generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(priv: Any) -> dict[str, Any]:
    from jwt.algorithms import RSAAlgorithm

    jwk: dict[str, Any] = json.loads(RSAAlgorithm.to_jwk(priv.public_key()))
    jwk["kid"] = "confine-kid"
    return jwk


def _writer_headers(priv: Any) -> dict[str, str]:
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": "ci@example.com",
            "aud": "nova-server",
            "iss": "https://oidc.example.com",
            "iat": now,
            "exp": now + 3600,
            "nova_roles": ["writer"],
        },
        priv,
        algorithm="RS256",
        headers={"kid": "confine-kid"},
    )
    return {"Authorization": f"Bearer {token}"}


def test_a_writer_token_cannot_write_outside_the_evidence_dir(layout: dict[str, Path]) -> None:
    import novafabric.server.auth as auth_mod

    priv = _rsa_key()
    jwk = _jwk(priv)

    def fake_fetch(self: JwksCache) -> None:
        self._keys = {jwk["kid"]: jwk}
        self._fetched_at = time.monotonic()

    auth_mod._jwks_cache = None
    cfg = ServerConfig(
        db_path=str(layout["root"] / "server.db"),
        oidc=OidcConfig(issuer_url="https://oidc.example.com", audience="nova-server"),
    )
    before = _snapshot(layout["root"])
    with patch.object(JwksCache, "_fetch", fake_fetch):
        client = TestClient(_app(cfg, layout["store"]), raise_server_exceptions=False)
        headers = _writer_headers(priv)
        attacks: list[dict[str, Any]] = [
            {"run_id": str(layout["outside"] / "foreign"), "output_path": str(layout["victim"])},
            {"run_id": RUN_ID, "output_path": str(layout["victim"])},
            {"run_id": "../outside/foreign"},
            {"run_id": str(layout["outside"] / "foreign")},
        ]
        for body in attacks:
            resp = client.post("/v0/evidence", json=body, headers=headers)
            assert resp.status_code == 400, (body, resp.text)
        _assert_nothing_written(layout, before)

        ok = client.post("/v0/evidence", json={"run_id": RUN_ID}, headers=headers)
        assert ok.status_code == 202, ok.text
        assert Path(ok.json()["bundle_path"]).parent == layout["evidence"]


# --------------------------------------------------------------------------- #
# Siblings in nova server that joined a request run_id onto the store
# --------------------------------------------------------------------------- #


class TestSiblingRoutes:
    def test_replay_rejects_hostile_run_ids(
        self, client: TestClient, layout: dict[str, Path]
    ) -> None:
        for run_id in _hostile_run_ids(layout):
            resp = client.post("/v0/replays", json={"run_id": run_id, "mode": "dry_run"})
            assert resp.status_code == 400, (run_id, resp.text)

    def test_replay_does_not_follow_a_symlinked_capsule_entry(
        self, client: TestClient, layout: dict[str, Path]
    ) -> None:
        (layout["store"] / "linked").symlink_to(layout["outside"] / "foreign")
        resp = client.post("/v0/replays", json={"run_id": "linked", "mode": "dry_run"})
        assert resp.status_code == 400, resp.text

    @pytest.mark.parametrize("run_id", ["..%5Coutside", "x%00y", "..%2Foutside"])
    def test_get_capsule_rejects_encoded_traversal(
        self, client: TestClient, run_id: str
    ) -> None:
        resp = client.get(f"/v0/capsules/{run_id}")
        assert resp.status_code in (400, 404), resp.text
        assert resp.status_code != 200

    def test_get_capsule_does_not_follow_a_symlinked_entry(
        self, client: TestClient, layout: dict[str, Path]
    ) -> None:
        (layout["store"] / "linked").symlink_to(layout["outside"] / "foreign")
        resp = client.get("/v0/capsules/linked")
        assert resp.status_code == 400, resp.text

    def test_upload_parent_run_id_cannot_probe_outside_the_store(
        self, layout: dict[str, Path]
    ) -> None:
        from novafabric.server.errors import BadRequestError
        from novafabric.server.routes.capsules import _check_parent_exists

        for parent in ("../outside/foreign", str(layout["outside"] / "foreign"), "a\x00"):
            with pytest.raises(BadRequestError):
                _check_parent_exists({"parent_run_id": parent}, layout["store"])
        # A clean parent that exists is still accepted.
        _check_parent_exists({"parent_run_id": RUN_ID}, layout["store"])

    def test_run_id_validator_shares_the_one_rule(self) -> None:
        from novafabric.server.capsule_delete import is_valid_run_id

        assert is_valid_run_id(RUN_ID)
        for bad in ("", "..", "a/b", "a\\b", "x\x00", "/etc"):
            assert not is_valid_run_id(bad), bad
