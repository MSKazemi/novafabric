"""Hub revision receipts (HF-03, experimental): strengths, failure vocabulary, untrusted metadata.

No test touches the network. The Hub client is a fake with the same call shape as
``huggingface_hub.HfApi``; one contract test drives the *real* client through a mock HTTP
transport when ``huggingface_hub`` is installed (it is not a NovaFabric dependency).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novafabric._jsonschema_offline import offline_validator
from novafabric.interop import hf_hub
from novafabric.interop.hf_hub import (
    CLAIMS,
    EXTENSION_KEY,
    HubReference,
    InvalidHubReferenceError,
    attach_hub_references,
    check_revision_drift,
    declare_reference,
    references_from_manifest,
    resolve_reference,
    verify_local_file,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
WEIGHTS = b"not real weights"
WEIGHTS_SHA = hashlib.sha256(WEIGHTS).hexdigest()
REPO_ROOT = Path(__file__).resolve().parents[2]
CLOCK = "2026-10-28T00:00:00Z"


class _HttpError(Exception):
    """Shaped like huggingface_hub's HfHubHTTPError: a response with a status code."""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message)
        self.response = SimpleNamespace(status_code=status)


class RepositoryNotFoundError(_HttpError):
    pass


class GatedRepoError(RepositoryNotFoundError):  # the real class subclasses RepositoryNotFoundError
    pass


class RevisionNotFoundError(_HttpError):
    pass


class FakeApi:
    """Records calls; answers from ``info`` or raises ``error``."""

    def __init__(self, info: Any = None, error: BaseException | None = None) -> None:
        self.info, self.error, self.calls = info, error, []

    def _go(self, kind: str, repo_id: str, **kwargs: Any) -> Any:
        self.calls.append((kind, repo_id, kwargs))
        if self.error is not None:
            raise self.error
        return self.info

    def model_info(self, repo_id: str, **kw: Any) -> Any:
        return self._go("model", repo_id, **kw)

    def dataset_info(self, repo_id: str, **kw: Any) -> Any:
        return self._go("dataset", repo_id, **kw)

    def space_info(self, repo_id: str, **kw: Any) -> Any:
        return self._go("space", repo_id, **kw)


def _info(sha: str = SHA_A, repo_id: str = "acme/tiny", **extra: Any) -> SimpleNamespace:
    return SimpleNamespace(
        sha=sha,
        id=repo_id,
        last_modified=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        siblings=[
            SimpleNamespace(rfilename="model.safetensors", lfs={"sha256": WEIGHTS_SHA, "size": 16}),
            SimpleNamespace(rfilename="config.json", lfs=None),
        ],
        **extra,
    )


def _resolve(ref: HubReference, api: FakeApi, **kw: Any) -> HubReference:
    return resolve_reference(ref, api=api, clock=lambda: CLOCK, environ={}, **kw)


def _roundtrip(ref: HubReference) -> HubReference:
    """model_copy skips validation; this re-runs every invariant on the final receipt."""
    return HubReference.model_validate(json.loads(ref.model_dump_json()))


# ── declared / resolved / verified are three different things ────────────────


def test_declared_reference_is_declared_only_and_makes_no_call() -> None:
    ref = declare_reference("model", "acme/tiny", revision="main")
    assert (ref.evidence_strength, ref.resolution_status) == ("declared_only", "declared_only")
    assert ref.resolved_sha is None and ref.pinned is False
    assert ref.claim == CLAIMS["declared_only"]


@pytest.mark.parametrize("repo_type", ["model", "dataset", "space"])
def test_resolution_asks_the_matching_endpoint_and_reports_hub_resolved(repo_type: str) -> None:
    api = FakeApi(_info())
    out = _roundtrip(_resolve(declare_reference(repo_type, "acme/tiny", revision="main"), api))
    assert [c[0] for c in api.calls] == [repo_type]
    assert (out.resolution_status, out.evidence_strength) == ("resolved", "hub_resolved")
    assert out.resolved_sha == SHA_A and out.resolved_at == CLOCK
    assert out.hub_last_modified == "2026-01-02T03:04:05Z"
    assert out.pinned is False  # a branch name is not a pin
    assert out.claim == CLAIMS["hub_resolved"]


def test_the_config_and_split_of_a_dataset_are_carried_through() -> None:
    ref = declare_reference("dataset", "acme/qa", revision=SHA_A, config="default", split="test")
    out = _roundtrip(_resolve(ref, FakeApi(_info(repo_id="acme/qa"))))
    assert (out.config, out.split) == ("default", "test")


def test_pinned_commit_is_pinned_and_a_different_answer_is_an_error_not_adopted() -> None:
    ref = declare_reference("model", "acme/tiny", revision=SHA_A)
    assert ref.pinned is True
    ok = _resolve(ref, FakeApi(_info(SHA_A)))
    assert ok.resolution_status == "resolved" and ok.pinned is True
    bad = _resolve(ref, FakeApi(_info(SHA_B)))
    assert (bad.resolution_status, bad.reason) == ("error", "pinned_revision_mismatch")
    assert bad.resolved_sha is None and bad.evidence_strength == "declared_only"


def test_a_moved_branch_is_reported_as_moved() -> None:
    ref = declare_reference("model", "acme/tiny", revision="main")
    before = _resolve(ref, FakeApi(_info(SHA_A)))
    after = _resolve(ref, FakeApi(_info(SHA_B)))
    assert check_revision_drift(before, before) == "same"
    assert check_revision_drift(before, after) == "moved"


def test_drift_is_unknown_when_either_side_is_unresolved_or_targets_differ() -> None:
    ref = declare_reference("model", "acme/tiny", revision="main")
    resolved = _resolve(ref, FakeApi(_info()))
    down = _resolve(ref, FakeApi(error=RepositoryNotFoundError(404)))
    assert check_revision_drift(resolved, down) == "unknown"  # unavailable is never "same"
    other = _resolve(declare_reference("model", "acme/tiny", revision="v2"), FakeApi(_info()))
    assert check_revision_drift(resolved, other) == "unknown"


# ── failure vocabulary: truthful, closed, never raised ───────────────────────


@pytest.mark.parametrize(
    ("error", "status", "reason"),
    [
        (RepositoryNotFoundError(404), "unavailable", "not_found"),
        (RevisionNotFoundError(404), "unavailable", "revision_not_found"),
        (RepositoryNotFoundError(401), "unavailable", "unauthorized_or_gated"),
        (GatedRepoError(403), "unavailable", "unauthorized_or_gated"),
        (_HttpError(403), "unavailable", "unauthorized_or_gated"),
        (_HttpError(429), "unavailable", "rate_limited"),
        (_HttpError(503), "unavailable", "hub_error"),
        (ConnectionError("dns"), "unavailable", "hub_unreachable"),
        (TimeoutError(), "unavailable", "hub_unreachable"),
        (ValueError("boom"), "error", "hub_error"),
    ],
)
def test_hub_failures_become_a_recorded_reason(error: BaseException, status: str, reason: str) -> None:
    out = _roundtrip(_resolve(declare_reference("model", "acme/tiny"), FakeApi(error=error)))
    assert (out.resolution_status, out.reason) == (status, reason)
    assert out.resolved_sha is None and out.evidence_strength == "declared_only"


def test_exception_text_never_reaches_the_receipt() -> None:
    secret = "hf_" + "A1b2C3d4" * 5
    err = RepositoryNotFoundError(401, f"Invalid credentials: Bearer {secret}")
    out = _resolve(declare_reference("model", "acme/tiny"), FakeApi(error=err), token=secret)
    assert secret not in out.model_dump_json()


def test_offline_mode_short_circuits_before_any_client_is_built() -> None:
    api = FakeApi(_info())
    out = resolve_reference(
        declare_reference("model", "acme/tiny"), api=api, environ={"HF_HUB_OFFLINE": "1"}
    )
    assert (out.resolution_status, out.reason) == ("unavailable", "offline")
    assert api.calls == []


def test_missing_client_library_is_unavailable_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "huggingface_hub", None)  # makes `import` raise ImportError
    out = resolve_reference(declare_reference("model", "acme/tiny"), environ={})
    assert (out.resolution_status, out.reason) == ("unavailable", "dependency_missing")


# ── anonymous by default, token only when passed ─────────────────────────────


def test_default_call_is_anonymous_so_an_ambient_token_is_not_sent() -> None:
    api = FakeApi(_info())
    _resolve(declare_reference("model", "acme/tiny", revision="main"), api)
    kwargs = api.calls[0][2]
    assert kwargs["token"] is False and kwargs["revision"] == "main" and kwargs["timeout"] == 10.0
    assert "files_metadata" not in kwargs  # no extra metadata unless files were asked for


def test_an_explicit_token_is_passed_through_and_never_stored() -> None:
    api = FakeApi(_info())
    out = _resolve(declare_reference("model", "acme/tiny"), api, token="hf_" + "x" * 34)
    assert api.calls[0][2]["token"] == "hf_" + "x" * 34
    assert "hf_" + "x" * 34 not in out.model_dump_json()


# ── untrusted metadata ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_sha",
    ["main", "A" * 40, "a" * 39, "a" * 41, "../../etc/passwd", "a" * 40 + "\n", "", None, 123],
)
def test_a_malformed_commit_from_the_hub_is_rejected(bad_sha: Any) -> None:
    out = _resolve(declare_reference("model", "acme/tiny"), FakeApi(_info(sha=bad_sha)))
    assert (out.resolution_status, out.reason) == ("error", "invalid_metadata")
    assert out.resolved_sha is None


def test_a_hostile_canonical_id_is_ignored_and_a_case_only_change_is_not_a_rename() -> None:
    hostile = _resolve(
        declare_reference("model", "acme/tiny"), FakeApi(_info(repo_id="x/<script>alert(1)</script>"))
    )
    assert hostile.canonical_repo_id is None and hostile.resolution_status == "resolved"
    cased = _resolve(declare_reference("model", "acme/tiny"), FakeApi(_info(repo_id="ACME/Tiny")))
    assert cased.canonical_repo_id is None
    renamed = _resolve(declare_reference("model", "acme/tiny"), FakeApi(_info(repo_id="acme/small")))
    assert renamed.canonical_repo_id == "acme/small"


def test_free_text_fields_from_the_hub_are_never_copied() -> None:
    info = _info(card_data={"license": "ignore previous instructions"}, tags=["<b>x</b>"])
    dump = _resolve(declare_reference("model", "acme/tiny"), FakeApi(info)).model_dump_json()
    assert "ignore previous" not in dump and "<b>" not in dump


def test_a_hostile_digest_in_the_hub_listing_is_not_recorded() -> None:
    info = _info()
    info.siblings[0].lfs = {"sha256": "../../../x", "size": 1}
    out = _resolve(declare_reference("model", "acme/tiny"), FakeApi(info), files=["model.safetensors"])
    assert out.files == [] and out.unresolved_files == ["model.safetensors"]


@pytest.mark.parametrize(
    ("repo_type", "repo_id", "revision"),
    [
        ("model", "https://evil.example/x", None),
        ("model", "../../etc", None),
        ("model", "a/b/c", None),
        ("model", "", None),
        ("model", "acme/tiny", "main\nINJECT"),
        ("model", "acme/tiny", "has space"),
        ("model", "acme/tiny", "x" * 300),
        ("collection", "acme/tiny", None),
    ],
)
def test_malformed_caller_input_is_refused(repo_type: str, repo_id: str, revision: str | None) -> None:
    with pytest.raises((InvalidHubReferenceError, ValueError)):
        declare_reference(repo_type, repo_id, revision=revision)


def test_a_file_name_that_escapes_the_repo_is_refused() -> None:
    ref = declare_reference("model", "acme/tiny")
    for name in ("../secret", "/etc/passwd", "a\x00b", "a\\b"):
        with pytest.raises(InvalidHubReferenceError):
            resolve_reference(ref, files=[name], api=FakeApi(_info()), environ={})


# ── local bytes ──────────────────────────────────────────────────────────────


def _resolved_with_weights() -> HubReference:
    ref = declare_reference("model", "acme/tiny", revision=SHA_A)
    return _resolve(ref, FakeApi(_info()), files=["model.safetensors", "config.json", "nope.bin"])


def test_hub_published_digests_are_recorded_and_unlisted_files_are_disclosed() -> None:
    out = _roundtrip(_resolved_with_weights())
    assert [(f.path, f.source, f.local_verified) for f in out.files] == [
        ("model.safetensors", "hub_lfs", None)
    ]
    assert out.files[0].size_bytes == 16
    assert out.unresolved_files == ["config.json", "nope.bin"]  # not LFS / not in the revision
    assert out.evidence_strength == "hub_resolved"  # recording a digest verifies nothing


def test_matching_local_bytes_raise_the_strength_only_for_that_file(tmp_path: Path) -> None:
    f = tmp_path / "w.safetensors"
    f.write_bytes(WEIGHTS)
    out = _roundtrip(verify_local_file(_resolved_with_weights(), "model.safetensors", f))
    assert out.evidence_strength == "local_bytes_verified"
    assert out.claim == CLAIMS["local_bytes_verified"]
    assert out.files[0].local_verified is True


def test_mismatching_local_bytes_are_recorded_and_never_raise_the_strength(tmp_path: Path) -> None:
    f = tmp_path / "w.safetensors"
    f.write_bytes(WEIGHTS + b"x")
    out = _roundtrip(verify_local_file(_resolved_with_weights(), "model.safetensors", f))
    assert (out.evidence_strength, out.reason) == ("hub_resolved", "local_digest_mismatch")
    assert out.files[0].local_verified is False


def test_one_failing_file_withholds_the_verified_strength_from_the_whole_receipt(tmp_path: Path) -> None:
    other = b"second shard"
    info = _info()
    info.siblings.append(
        SimpleNamespace(rfilename="shard2.safetensors", lfs={"sha256": hashlib.sha256(other).hexdigest(), "size": 12})
    )
    ref = _resolve(
        declare_reference("model", "acme/tiny", revision=SHA_A), FakeApi(info),
        files=["model.safetensors", "shard2.safetensors"],
    )
    good, bad = tmp_path / "good", tmp_path / "bad"
    good.write_bytes(WEIGHTS)
    bad.write_bytes(b"tampered shard")
    once = verify_local_file(ref, "model.safetensors", good)
    assert once.evidence_strength == "local_bytes_verified"  # a verified subset is a true claim...
    twice = _roundtrip(verify_local_file(once, "shard2.safetensors", bad))
    assert twice.evidence_strength == "hub_resolved"  # ...until any recorded file fails
    assert twice.reason == "local_digest_mismatch"
    assert {f.path: f.local_verified for f in twice.files} == {
        "model.safetensors": True, "shard2.safetensors": False
    }


def test_a_later_good_check_clears_a_stale_mismatch(tmp_path: Path) -> None:
    f = tmp_path / "w.safetensors"
    f.write_bytes(b"bad")
    bad = verify_local_file(_resolved_with_weights(), "model.safetensors", f)
    f.write_bytes(WEIGHTS)
    good = _roundtrip(verify_local_file(bad, "model.safetensors", f))
    assert (good.evidence_strength, good.reason) == ("local_bytes_verified", None)


def test_a_declared_digest_never_raises_the_strength(tmp_path: Path) -> None:
    f = tmp_path / "w"
    f.write_bytes(WEIGHTS)
    ref = declare_reference("model", "acme/tiny", revision=SHA_A)
    out = _roundtrip(verify_local_file(ref, "w", f, expected_sha256=WEIGHTS_SHA))
    assert out.files[0].source == "declared" and out.files[0].local_verified is True
    assert out.evidence_strength == "declared_only"
    resolved = _resolve(ref, FakeApi(_info()))
    assert verify_local_file(resolved, "w", f, expected_sha256=WEIGHTS_SHA).evidence_strength == "hub_resolved"


def test_unreadable_missing_symlinked_and_fifo_files_are_refused_without_blocking(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.write_bytes(WEIGHTS)
    link = tmp_path / "link"
    link.symlink_to(real)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)  # opening one for read would hang forever
    for target in (tmp_path / "missing", link, fifo, tmp_path):
        out = verify_local_file(_resolved_with_weights(), "model.safetensors", target)
        assert (out.evidence_strength, out.reason) == ("hub_resolved", "local_file_unreadable")


def test_verify_without_any_recorded_digest_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(InvalidHubReferenceError):
        verify_local_file(declare_reference("model", "acme/tiny"), "w", tmp_path / "w")


def test_a_large_file_is_hashed_in_chunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hf_hub, "_CHUNK", 7)
    f = tmp_path / "big"
    f.write_bytes(WEIGHTS * 5)
    ref = declare_reference("model", "acme/tiny", revision=SHA_A)
    out = verify_local_file(ref, "big", f, expected_sha256=hashlib.sha256(WEIGHTS * 5).hexdigest())
    assert out.files[0].local_verified is True


def test_a_receipt_cannot_claim_more_than_it_holds() -> None:
    with pytest.raises(ValueError):
        HubReference(repo_type="model", repo_id="acme/tiny", evidence_strength="hub_resolved",
                     claim=CLAIMS["hub_resolved"])  # no resolved_sha
    with pytest.raises(ValueError):
        HubReference(repo_type="model", repo_id="acme/tiny", resolved_sha=SHA_A,
                     evidence_strength="local_bytes_verified", claim=CLAIMS["local_bytes_verified"])
    with pytest.raises(ValueError):
        HubReference(repo_type="model", repo_id="acme/tiny", resolved_sha=SHA_A,
                     evidence_strength="hub_resolved", claim="trust me")


# ── capsule integration, compatibility, isolation ────────────────────────────

_MANIFEST = {"schema_version": "0.1.0", "run_id": "01M5ZZ3X8C893RA27ZNYBK9NS0", "status": "success"}


def test_old_and_empty_manifests_are_untouched_and_read_back_as_no_references() -> None:
    assert attach_hub_references(_MANIFEST, []) is _MANIFEST  # same object: nothing written
    assert references_from_manifest(_MANIFEST) == []
    assert references_from_manifest({"extensions": {"io.other.thing": {"x": 1}}}) == []


def test_receipts_round_trip_in_a_deterministic_order_and_keep_other_extensions() -> None:
    a = _resolve(declare_reference("model", "acme/zeta", revision="main"), FakeApi(_info(repo_id="acme/zeta")))
    b = declare_reference("dataset", "acme/alpha")
    manifest = {**_MANIFEST, "extensions": {"io.other.thing": {"x": 1}}}
    out = attach_hub_references(manifest, [a, b])
    assert out["extensions"]["io.other.thing"] == {"x": 1}
    assert [r.repo_id for r in references_from_manifest(out)] == ["acme/alpha", "acme/zeta"]
    assert out == attach_hub_references(manifest, [b, a])  # input order does not matter
    assert "extensions" not in _MANIFEST  # input not mutated


def test_a_malformed_extension_fails_closed_on_read() -> None:
    for block in ({"references": "nope"}, {"references": [{"repo_type": "model"}]}, "text"):
        with pytest.raises((InvalidHubReferenceError, ValueError)):
            references_from_manifest({"extensions": {EXTENSION_KEY: block}})


def test_the_run_capsule_schema_accepts_the_extension() -> None:
    schema = json.loads((REPO_ROOT / "schemas" / "run-capsule.schema.json").read_text())
    minimal = dict(_MANIFEST)
    ref = _resolve(declare_reference("model", "acme/tiny", revision="main"), FakeApi(_info()))
    manifest = attach_hub_references(minimal, [ref])
    errors = [e for e in offline_validator(schema).iter_errors(manifest) if "extensions" in e.json_path]
    assert errors == []  # other required fields are not under test here


def test_importing_the_module_loads_no_client_and_opens_no_connection() -> None:
    code = (
        "import sys, socket\n"
        "def deny(*a, **k): raise AssertionError('network at import')\n"
        "socket.socket.connect = deny\n"
        "sys.modules['huggingface_hub'] = None\n"
        "import novafabric.interop.hf_hub as m\n"
        "assert m.declare_reference('model', 'a/b').evidence_strength == 'declared_only'\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr


# ── contract: the REAL client, a mock transport, no network ──────────────────


def test_real_huggingface_hub_client_through_a_mock_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("huggingface_hub")
    httpx2 = pytest.importorskip("httpx2")
    import huggingface_hub as hub

    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    seen: list[tuple[str, bool]] = []

    def handler(request: Any) -> Any:
        seen.append((request.url.path, "authorization" in request.headers))
        path = request.url.path
        if "/gated" in path:
            return httpx2.Response(403, json={"error": "gated"}, headers={"X-Error-Code": "GatedRepo"})
        if "/missing" in path:
            return httpx2.Response(404, json={"error": "nf"}, headers={"X-Error-Code": "RepoNotFound"})
        if "/limited" in path:
            return httpx2.Response(429, json={"error": "rl"})
        payload = {
            "_id": "x", "id": "acme/tiny", "modelId": "acme/tiny", "sha": SHA_A,
            "lastModified": "2026-01-02T03:04:05.000Z", "private": False, "gated": False, "tags": [],
            "siblings": [
                {"rfilename": "model.safetensors", "size": 16,
                 "lfs": {"sha256": WEIGHTS_SHA, "size": 16, "pointerSize": 120}},
                {"rfilename": "config.json", "size": 5},
            ],
        }
        return httpx2.Response(200, json=payload)

    hub.set_client_factory(lambda: httpx2.Client(transport=httpx2.MockTransport(handler)))
    try:
        ok = resolve_reference(
            declare_reference("model", "acme/tiny", revision="main"),
            files=["model.safetensors", "config.json"], environ={},
        )
        assert (ok.resolution_status, ok.resolved_sha, ok.evidence_strength) == ("resolved", SHA_A, "hub_resolved")
        assert [(f.path, f.sha256) for f in ok.files] == [("model.safetensors", WEIGHTS_SHA)]
        assert ok.unresolved_files == ["config.json"]
        reasons = {
            name: resolve_reference(declare_reference("model", f"acme/{name}"), environ={}).reason
            for name in ("gated", "missing", "limited")
        }
        assert reasons == {"gated": "unauthorized_or_gated", "missing": "not_found", "limited": "rate_limited"}
        assert seen and not any(auth for _, auth in seen)  # anonymous: no Authorization header
    finally:
        # Put the library's own default back: the factory is process-global.
        from huggingface_hub.utils._http import default_client_factory

        hub.set_client_factory(default_client_factory)
        hub.close_session()
