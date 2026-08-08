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

"""The published response schemas describe what the routes actually return (ADR-0227).

ADR-0227 declares response models through FastAPI's ``responses={...}``, which is
documentation-only: it names the schema in ``components.schemas`` and does not
touch the response body. That was chosen over ``response_model=``, which filters
the body to the model's fields and would have changed a published API's wire
format.

The cost of that choice is that nothing checks the declaration, so this module
is what pays it. For each declared route it calls the **real** route and
validates the **real** response against the model the route declares — and it
reads that model off the route object rather than restating it here, so a
changed declaration is followed automatically and a changed response shape fails
by name.

Two guards keep it from rotting:

* ``test_every_declared_model_is_exercised`` — a new declaration without a
  conformance case fails the suite, so the coverage cannot silently narrow.
* ``test_no_route_uses_response_model`` — the field-stripping fix ADR-0227
  rejected cannot be reintroduced by a later drive-by.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
import yaml

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from novafabric.serve.introspect import iter_routes  # noqa: E402
from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import ServerConfig  # noqa: E402
from novafabric.server.schemas import ErrorEnvelope  # noqa: E402

RUN_ID = "01TEST00000000000000000042"
_SPAN = "sha256:" + "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
_CARD = "sha256:" + "60303ae22b998861bce3b28f33eec1be758a213c86c93c076dbe9f558c11c752"

VALID_MODEL_YAML = """
novafabric_spec_version: "0.1"
asset_type: model
name: conformance-model
version: "1.0.0"
status: development
spec:
  framework: pytorch
  artifact_path: /tmp/model.pt
"""

# Every operation ADR-0227 brought into scope. Kept explicit so that a route
# gaining a declaration without a conformance case is a failure rather than a
# silent gap (see ``test_every_declared_model_is_exercised``).
IN_SCOPE_OPERATIONS = {
    "listAssets",
    "createAsset",
    "getAsset",
    "promoteAsset",
    "listCapsules",
    "uploadCapsule",
    "getCapsule",
    "submitCapsuleScore",
    "exportEvidence",
    "getEvidenceBundle",
    "downloadEvidenceBundle",
}


def _capsule_manifest(run_id: str) -> dict[str, Any]:
    return {
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


def _redaction_proof(run_id: str) -> dict[str, Any]:
    """A clean-scan redaction proof in the shape the real scanner emits.

    The evidence builder attests over ``proof_id``, ``chain_hash``,
    ``findings_count.total`` and ``scanner.name``, so a stub proof makes the
    export fail. Built through the real ``recompute_chain_hash`` rather than
    with a literal digest, so the fixture stays valid if the canonicalization
    changes.
    """
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
                "by_severity": {
                    "critical": 0,
                    "high": 0,
                    "medium": 0,
                    "low": 0,
                    "info": 0,
                },
            },
            "findings": [],
            "bytes_scanned": 0,
            "bytes_redacted": 0,
            "unsafe_skips": [],
        }
    )


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    base = tmp_path / "runs"
    base.mkdir()
    cdir = base / RUN_ID
    cdir.mkdir()
    (cdir / "capsule.yaml").write_text(yaml.safe_dump(_capsule_manifest(RUN_ID)))
    (cdir / "trace.jsonl").write_text(
        json.dumps({"span_id": "root", "span_digest": _SPAN}) + "\n"
    )
    # A complete capsule, so the evidence-export assertions exercise the real
    # bundle path instead of skipping on a fixture the builder rejects.
    for name in ("model-calls.jsonl", "tool-calls.jsonl", "assets.jsonl"):
        (cdir / name).write_text("")
    (cdir / "env.lock").write_text("{}")
    (cdir / "redaction-proof.json").write_text(json.dumps(_redaction_proof(RUN_ID)))
    (cdir / "inputs").mkdir()
    (cdir / "outputs").mkdir()
    return base


@pytest.fixture
def app(tmp_path: Path, capsule_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from novafabric.server import deps

    # The export route writes to ~/.novafabric/evidence unless redirected.
    # Without this the suite would deposit bundles in the developer's home.
    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_DIR", str(tmp_path / "evidence"))

    # ADR-0184: anonymous admin requires the explicit insecure opt-out.
    created = create_app(
        ServerConfig(db_path=str(tmp_path / "server.db"), insecure_no_auth=True)
    )
    created.dependency_overrides[deps.get_capsule_dir] = lambda: capsule_dir
    return created


@pytest.fixture
def client(app: Any) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


# --------------------------------------------------------------------------- #
# Reading the declaration off the routes
# --------------------------------------------------------------------------- #


def _declared_models(app: Any) -> dict[tuple[str, int], type[BaseModel]]:
    """Map ``(operation_id, status_code)`` to the model the route declares.

    Read from the route objects, not restated here — the point of this module is
    to check the declaration, so the declaration has to be its input.
    """
    declared: dict[tuple[str, int], type[BaseModel]] = {}
    for _info in iter_routes(app):
        route = _info.route
        if not getattr(route, "operation_id", None):
            continue
        for status, spec in (route.responses or {}).items():
            model = spec.get("model") if isinstance(spec, dict) else None
            if isinstance(model, type) and issubclass(model, BaseModel):
                declared[(route.operation_id, int(status))] = model
    return declared


def _success_models(app: Any) -> dict[tuple[str, int], type[BaseModel]]:
    """Declared models for 2xx responses only — the error envelope is shared."""
    return {
        key: model
        for key, model in _declared_models(app).items()
        if 200 <= key[1] < 300
    }


def _upload_capsule(client: TestClient, run_id: str) -> Any:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("capsule.yaml", yaml.safe_dump(_capsule_manifest(run_id)))
        zf.writestr("trace.jsonl", "")
    buf.seek(0)
    return client.post(
        "/v0/capsules", files={"capsule": ("capsule.zip", buf, "application/zip")}
    )


def _score_body() -> dict[str, Any]:
    return {
        "name": "answer_correct",
        "value": True,
        "value_type": "boolean",
        "source": "judge",
        "evaluator_id": "ci://acme/repo#judge@v3",
        "subject": _SPAN,
        "eval_card_digest": _CARD,
    }


def _assert_conforms(
    app: Any, operation_id: str, status: int, body: Any
) -> type[BaseModel]:
    """Validate a real response body against the model its route declares."""
    models = _declared_models(app)
    model = models.get((operation_id, status))
    assert model is not None, (
        f"{operation_id} has no declared model for {status}; "
        f"declared: {sorted(k for k in models if k[0] == operation_id)}"
    )
    model.model_validate(body)
    return model


# --------------------------------------------------------------------------- #
# Per-operation conformance — real route, real response, declared model
# --------------------------------------------------------------------------- #


class TestCapsuleConformance:
    def test_list_capsules_first_page(
        self, app: Any, client: TestClient
    ) -> None:
        resp = client.get("/v0/capsules")
        assert resp.status_code == 200, resp.text
        _assert_conforms(app, "listCapsules", 200, resp.json())

    def test_list_capsules_keyset_page_omits_total(
        self, app: Any, client: TestClient
    ) -> None:
        """The page that made ``total`` optional — it must still validate.

        This is the shape the previous published contract got wrong: it marked
        ``total`` required, which every page after the first disproves.
        """
        for i in range(3):
            _upload_capsule(client, f"01CONFORM00000000000000{i:03d}")
        first = client.get("/v0/capsules?limit=1")
        assert first.status_code == 200, first.text
        cursor = first.json()["next_cursor"]
        assert cursor, "expected a second page for this assertion to mean anything"

        resp = client.get(f"/v0/capsules?limit=1&cursor={cursor}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "total" not in body, "keyset pages are expected to omit total"
        _assert_conforms(app, "listCapsules", 200, body)

    def test_upload_capsule(self, app: Any, client: TestClient) -> None:
        resp = _upload_capsule(client, "01CONFORM00000000000000999")
        assert resp.status_code == 201, resp.text
        _assert_conforms(app, "uploadCapsule", 201, resp.json())

    def test_get_capsule(self, app: Any, client: TestClient) -> None:
        resp = client.get(f"/v0/capsules/{RUN_ID}")
        assert resp.status_code == 200, resp.text
        _assert_conforms(app, "getCapsule", 200, resp.json())

    def test_submit_score_created_and_idempotent_replay(
        self, app: Any, client: TestClient
    ) -> None:
        """Both statuses carry the same body — the contract said 200 was empty."""
        body = _score_body()
        created = client.post(f"/v0/capsules/{RUN_ID}/scores", json=body)
        assert created.status_code == 201, created.text
        _assert_conforms(app, "submitCapsuleScore", 201, created.json())

        score_id = created.json()["score"]["score_id"]
        replay = client.post(
            f"/v0/capsules/{RUN_ID}/scores", json={**body, "score_id": score_id}
        )
        assert replay.status_code == 200, replay.text
        assert replay.json(), "an idempotent replay was expected to carry a body"
        _assert_conforms(app, "submitCapsuleScore", 200, replay.json())


class TestAssetConformance:
    def test_list_assets(self, app: Any, client: TestClient) -> None:
        resp = client.get("/v0/assets")
        assert resp.status_code == 200, resp.text
        _assert_conforms(app, "listAssets", 200, resp.json())

    def test_create_get_and_promote_asset(
        self, app: Any, client: TestClient
    ) -> None:
        created = client.post("/v0/assets", json={"spec_yaml": VALID_MODEL_YAML})
        assert created.status_code == 201, created.text
        _assert_conforms(app, "createAsset", 201, created.json())

        asset_id = created.json()["id"]
        fetched = client.get(f"/v0/assets/{asset_id}")
        assert fetched.status_code == 200, fetched.text
        _assert_conforms(app, "getAsset", 200, fetched.json())

        promoted = client.put(
            f"/v0/assets/{asset_id}/promote", json={"to_status": "staging"}
        )
        assert promoted.status_code == 200, promoted.text
        _assert_conforms(app, "promoteAsset", 200, promoted.json())

    def test_promoted_asset_reports_a_status_the_contract_lists(
        self, app: Any, client: TestClient
    ) -> None:
        """``AssetStatus`` is the real enum, not the four-value one it replaced.

        The published contract omitted ``validated`` and ``pending_approval``,
        so a perfectly ordinary promotion produced a value the spec called
        impossible.
        """
        from novafabric.server.schemas import AssetSummary

        statuses = AssetSummary.model_json_schema(
            ref_template="#/$defs/{model}"
        )["$defs"]["AssetStatus"]["enum"]
        assert {"validated", "pending_approval"} <= set(statuses)


class TestEvidenceConformance:
    def test_export_and_get_bundle(self, app: Any, client: TestClient) -> None:
        resp = client.post("/v0/evidence", json={"run_id": RUN_ID})
        assert resp.status_code == 202, resp.text
        _assert_conforms(app, "exportEvidence", 202, resp.json())

        bundle_id = resp.json()["bundle_id"]
        fetched = client.get(f"/v0/evidence/{bundle_id}")
        assert fetched.status_code == 200, fetched.text
        _assert_conforms(app, "getEvidenceBundle", 200, fetched.json())


class TestErrorEnvelopeConformance:
    """The declared error body is the one the handlers actually produce."""

    @pytest.mark.parametrize(
        ("method", "path", "status"),
        [
            ("get", "/v0/capsules/no-such-run", 404),
            ("get", "/v0/assets/no-such-asset", 404),
            ("get", "/v0/evidence/no-such-bundle", 404),
            ("post", "/v0/evidence", 400),
        ],
    )
    def test_error_responses_match_the_envelope(
        self, client: TestClient, method: str, path: str, status: int
    ) -> None:
        resp = client.request(method, path, json={} if method == "post" else None)
        assert resp.status_code == status, resp.text
        ErrorEnvelope.model_validate(resp.json())


# --------------------------------------------------------------------------- #
# Guards — the declaration cannot narrow, and the rejected fix cannot return
# --------------------------------------------------------------------------- #


def test_every_declared_model_is_exercised(app: Any) -> None:
    """Every 2xx declaration in scope has a conformance case above.

    Derived from the routes, so adding a declaration without a case fails here
    instead of shipping an unchecked promise.
    """
    declared_ops = {op for op, _status in _success_models(app)}
    missing = declared_ops - IN_SCOPE_OPERATIONS
    assert not missing, (
        "these routes declare a response model but are not listed in "
        f"IN_SCOPE_OPERATIONS, so nothing checks them: {sorted(missing)}"
    )


def test_in_scope_operations_all_exist(app: Any) -> None:
    """The scope list names real operations — it cannot go stale silently."""
    operation_ids = {
        oid
        for info in iter_routes(app)
        if (oid := getattr(info.route, "operation_id", None))
    }
    missing = IN_SCOPE_OPERATIONS - operation_ids
    assert not missing, f"IN_SCOPE_OPERATIONS names operations that do not exist: {sorted(missing)}"


def test_declared_routes_do_not_bind_a_response_model(app: Any) -> None:
    """The declaration stays documentation-only (ADR-0227's central invariant).

    ``response_model`` filters the response body to the model's fields. Every
    route in scope returns a wider ``dict`` than its declared model describes —
    ``list_capsules`` alone conditionally includes ``total`` — so binding the
    model would silently drop fields from a published API. Declaring the shape
    and enforcing it are different acts, and only the first one is wanted here.

    Checked as ``response_model is None``, which is what the explicit
    ``response_model=None`` markers on these routes produce. Note that FastAPI
    otherwise *infers* a response model from the return annotation, so those
    markers are load-bearing rather than decorative, and this is the test that
    says so.
    """
    offenders = sorted(
        info.route.name
        for info in iter_routes(app)
        if getattr(info.route, "operation_id", None) in IN_SCOPE_OPERATIONS
        and getattr(info.route, "response_model", None) is not None
    )
    assert not offenders, (
        "these routes bind a response_model, which filters the response body "
        f"and would drop fields from the published API (ADR-0227): {offenders}"
    )
