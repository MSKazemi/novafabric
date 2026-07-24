"""In-process integration: the sync client against the real FastAPI server app.

Covers the ADR-0202 P1 acceptance round-trips with no real network:
upload (201 / 400 / 409 ``conflict`` / 409 ``parent_not_found``), scores
(201 vs 200-replay ⇒ ``None``), pagination round-trip, health, and
``nvfk_`` + local-token bearer auth (401/403 paths).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402

from novafabric.capture._ulid import new_ulid  # noqa: E402
from novafabric.client import (  # noqa: E402
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
    NovaFabricAPIError,
    NovaFabricClient,
    ScoreSubmission,
)
from novafabric.server.api_keys import create_key  # noqa: E402
from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import ServerConfig  # noqa: E402

from .conftest import SyncASGITransport  # noqa: E402

TEST_TOKEN = "test-local-token-adr0202"
_SPAN = "sha256:" + "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
_CARD = "sha256:" + "60303ae22b998861bce3b28f33eec1be758a213c86c93c076dbe9f558c11c752"


def _make_capsule_source(
    base: Path,
    run_id: str,
    *,
    parent_run_id: str | None = None,
    created_at: str = "2026-07-24T00:00:00+00:00",
) -> Path:
    """Build an on-disk capsule directory suitable for upload."""
    cdir = base / run_id
    cdir.mkdir(parents=True)
    manifest: dict[str, Any] = {
        "schema_version": "0.1.0",
        "run_id": run_id,
        "created_at": created_at,
        "status": "success",
    }
    if parent_run_id is not None:
        manifest["parent_run_id"] = parent_run_id
    (cdir / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    (cdir / "trace.jsonl").write_text(
        json.dumps({"span_id": "root", "span_digest": _SPAN}) + "\n"
    )
    return cdir


@pytest.fixture
def capsule_store(tmp_path: Path) -> Path:
    store = tmp_path / "capsule-store"
    store.mkdir()
    return store


def _build_app(tmp_path: Path, capsule_store: Path, **config_kwargs: Any) -> FastAPI:
    from novafabric.server import deps

    config = ServerConfig(db_path=str(tmp_path / "server.db"), **config_kwargs)
    app = create_app(config)
    app.dependency_overrides[deps.get_capsule_dir] = lambda: capsule_store
    return app


@pytest.fixture
def app(tmp_path: Path, capsule_store: Path) -> FastAPI:
    """Server app with the ADR-0184 insecure opt-out (no-auth paths)."""
    return _build_app(tmp_path, capsule_store, insecure_no_auth=True)


@pytest.fixture
def client(app: FastAPI) -> Iterator[NovaFabricClient]:
    with NovaFabricClient(
        "http://testserver/v0", transport=SyncASGITransport(app)
    ) as nc:
        yield nc


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


def test_health_reaches_root_mounted_endpoint(client: NovaFabricClient) -> None:
    result = client.health()
    assert result.meta.status == 200
    assert result.data.ok is True
    assert result.data.service == "nova-server"
    assert isinstance(result.data.version, str)


# ---------------------------------------------------------------------------
# capsule upload contract (201 / 400 / 409 conflict / 409 parent_not_found)
# ---------------------------------------------------------------------------


class TestUpload:
    def test_directory_upload_201_then_get(
        self, client: NovaFabricClient, tmp_path: Path
    ) -> None:
        source = _make_capsule_source(tmp_path / "src", "run-int-001")
        result = client.upload_capsule(source)
        assert result.meta.status == 201
        assert result.data.run_id == "run-int-001"
        detail = client.get_capsule("run-int-001").data
        assert detail.run_id == "run-int-001"
        assert detail.status == "success"
        assert detail.schema_version == "0.1.0"

    def test_zip_file_and_bytes_upload(
        self, client: NovaFabricClient, tmp_path: Path
    ) -> None:
        from novafabric.client._client import _zip_directory

        source = _make_capsule_source(tmp_path / "src", "run-int-zip")
        payload = _zip_directory(source)
        # determinism: packing the same tree twice gives identical bytes
        assert payload == _zip_directory(source)

        zip_path = tmp_path / "capsule.zip"
        zip_path.write_bytes(payload)
        assert client.upload_capsule(zip_path).meta.status == 201

        source2 = _make_capsule_source(tmp_path / "src2", "run-int-bytes")
        assert client.upload_capsule(_zip_directory(source2)).meta.status == 201

    def test_duplicate_upload_409_conflict(
        self, client: NovaFabricClient, tmp_path: Path
    ) -> None:
        source = _make_capsule_source(tmp_path / "src", "run-int-dup")
        client.upload_capsule(source)
        with pytest.raises(ConflictError) as exc_info:
            client.upload_capsule(source)
        assert exc_info.value.code == "conflict"

    def test_orphan_child_409_parent_not_found(
        self, client: NovaFabricClient, tmp_path: Path
    ) -> None:
        child = _make_capsule_source(
            tmp_path / "src",
            "run-int-child",
            parent_run_id="run-int-missing-parent",
            created_at="2099-01-01T00:00:00+00:00",  # inside the orphan window
        )
        with pytest.raises(ConflictError) as exc_info:
            client.upload_capsule(child)
        assert exc_info.value.code == "parent_not_found"

    def test_empty_payload_400(self, client: NovaFabricClient) -> None:
        with pytest.raises(NovaFabricAPIError) as exc_info:
            client.upload_capsule(b"")
        assert exc_info.value.status == 400
        assert exc_info.value.code == "bad_request"

    def test_non_zip_payload_400(self, client: NovaFabricClient) -> None:
        with pytest.raises(NovaFabricAPIError) as exc_info:
            client.upload_capsule(b"not a zip archive")
        assert exc_info.value.status == 400
        assert exc_info.value.code == "bad_request"


# ---------------------------------------------------------------------------
# pagination round-trip
# ---------------------------------------------------------------------------


class TestPaginationRoundTrip:
    def test_list_and_iter_walk_real_cursors(
        self, client: NovaFabricClient, tmp_path: Path
    ) -> None:
        for index in range(5):
            client.upload_capsule(
                _make_capsule_source(tmp_path / f"s{index}", f"run-page-{index}")
            )
        first = client.list_capsules(limit=2).data
        assert len(first.items) == 2
        assert first.total == 5
        assert first.next_cursor is not None

        second = client.list_capsules(limit=2, cursor=first.next_cursor).data
        assert len(second.items) == 2
        page_one_ids = {c.run_id for c in first.items}
        page_two_ids = {c.run_id for c in second.items}
        assert page_one_ids.isdisjoint(page_two_ids)

        walked = [c.run_id for c in client.iter_capsules(limit=2)]
        assert len(walked) == 5
        assert len(set(walked)) == 5

    def test_get_capsule_404(self, client: NovaFabricClient) -> None:
        with pytest.raises(NotFoundError) as exc_info:
            client.get_capsule("run-does-not-exist")
        assert exc_info.value.code == "not_found"

    def test_assets_list_and_404(self, client: NovaFabricClient) -> None:
        page = client.list_assets().data
        assert page.items == []
        assert page.total == 0
        assert list(client.iter_assets()) == []
        with pytest.raises(NotFoundError):
            client.get_asset("00000000-0000-0000-0000-000000000000")


# ---------------------------------------------------------------------------
# scores: 201 vs 200-idempotent-replay ⇒ None
# ---------------------------------------------------------------------------


class TestScores:
    @pytest.fixture
    def scored_run(self, client: NovaFabricClient, tmp_path: Path) -> str:
        run_id = "run-int-scores"
        client.upload_capsule(_make_capsule_source(tmp_path / "sc", run_id))
        return run_id

    def _submission(self) -> ScoreSubmission:
        return ScoreSubmission(
            name="answer_correct",
            value=True,
            value_type="boolean",
            source="judge",
            evaluator_id="ci://acme/repo#judge@v3",
            subject=_SPAN,
            eval_card_digest=_CARD,
            score_id=new_ulid(),
        )

    def test_201_returns_stored_record(
        self, client: NovaFabricClient, scored_run: str
    ) -> None:
        result = client.submit_score(scored_run, self._submission())
        assert result.meta.status == 201
        assert result.data is not None
        assert result.data.idempotent_replay is False
        assert result.data.score["name"] == "answer_correct"

    def test_200_idempotent_replay_data_is_none(
        self, client: NovaFabricClient, scored_run: str
    ) -> None:
        submission = self._submission()
        assert client.submit_score(scored_run, submission).meta.status == 201
        replay = client.submit_score(scored_run, submission)
        assert replay.meta.status == 200
        assert replay.data is None

    def test_dict_body_accepted(
        self, client: NovaFabricClient, scored_run: str
    ) -> None:
        body = self._submission().model_dump(exclude_none=True)
        body["score_id"] = new_ulid()
        assert client.submit_score(scored_run, body).meta.status == 201

    def test_malformed_submission_400(
        self, client: NovaFabricClient, scored_run: str
    ) -> None:
        with pytest.raises(NovaFabricAPIError) as exc_info:
            client.submit_score(scored_run, {"name": "only-a-name"})
        assert exc_info.value.status == 400

    def test_unknown_run_404(self, client: NovaFabricClient) -> None:
        with pytest.raises(NotFoundError):
            client.submit_score("run-missing", self._submission())


# ---------------------------------------------------------------------------
# auth: nvfk_ API key + local bearer token against the real dispatch
# ---------------------------------------------------------------------------


class TestAuth:
    @pytest.fixture
    def secured_app(self, tmp_path: Path, capsule_store: Path) -> FastAPI:
        return _build_app(tmp_path, capsule_store, local_token=TEST_TOKEN)

    def _client(self, app: FastAPI, **kwargs: Any) -> NovaFabricClient:
        return NovaFabricClient(
            "http://testserver/v0", transport=SyncASGITransport(app), **kwargs
        )

    def test_no_credential_401(self, secured_app: FastAPI) -> None:
        with self._client(secured_app) as nc:
            with pytest.raises(AuthenticationError) as exc_info:
                nc.list_capsules()
        assert exc_info.value.status == 401

    def test_wrong_token_401(self, secured_app: FastAPI) -> None:
        with self._client(secured_app, token="wrong-token") as nc:
            with pytest.raises(AuthenticationError):
                nc.list_capsules()

    def test_local_token_authenticates(self, secured_app: FastAPI) -> None:
        with self._client(secured_app, token=TEST_TOKEN) as nc:
            assert nc.list_capsules().meta.status == 200

    def test_token_provider_authenticates(self, secured_app: FastAPI) -> None:
        with self._client(secured_app, token=lambda: TEST_TOKEN) as nc:
            assert nc.list_capsules().meta.status == 200

    def test_api_key_authenticates_via_nvfk_dispatch(
        self, secured_app: FastAPI
    ) -> None:
        key, _record = create_key("sdk-int@example.com", ["reader"], actor="test")
        with self._client(secured_app, api_key=key) as nc:
            assert nc.list_capsules().meta.status == 200

    def test_invalid_api_key_401(self, secured_app: FastAPI) -> None:
        with self._client(secured_app, api_key="nvfk_deadbeef_" + "x" * 43) as nc:
            with pytest.raises(AuthenticationError):
                nc.list_capsules()

    def test_reader_api_key_403_on_writer_route(
        self, secured_app: FastAPI, tmp_path: Path
    ) -> None:
        key, _record = create_key("sdk-reader@example.com", ["reader"], actor="test")
        source = _make_capsule_source(tmp_path / "src", "run-int-rbac")
        with self._client(secured_app, api_key=key) as nc:
            with pytest.raises(AuthorizationError) as exc_info:
                nc.upload_capsule(source)
        assert exc_info.value.status == 403
        assert exc_info.value.code == "forbidden"

    def test_writer_api_key_can_upload(
        self, secured_app: FastAPI, tmp_path: Path
    ) -> None:
        key, _record = create_key("sdk-writer@example.com", ["writer"], actor="test")
        source = _make_capsule_source(tmp_path / "src", "run-int-writer")
        with self._client(secured_app, api_key=key) as nc:
            assert nc.upload_capsule(source).meta.status == 201
