"""Per-tenant KEKs over the encrypting adapter (ADR-0243 slice 1).

The properties that matter, each as its own test:

- a tenant with a configured KEK gets its own wrapping (recorded additively
  as ``tenant_key_id``); a tenant without one falls back to the default KEK —
  flat-mode deployments observe zero change;
- key compromise/revocation is **tenant-scoped**: deleting one tenant's KEK
  makes exactly that tenant's objects unreadable (fail closed, named error)
  while every other tenant still decrypts;
- pre-0243 envelopes (no ``tenant_key_id``) stay readable forever;
- the env wiring fails closed on a misconfigured tenant-KEK directory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from novafabric.object_capsule_store.backend_router import make_adapter
from novafabric.object_capsule_store.cas import compute_sha256
from novafabric.object_capsule_store.encryption_wrapper import EncryptingAdapter
from novafabric.object_capsule_store.worm.base import WormAdapter
from novafabric.trust.envelope_encryption import DekUnwrapError, encrypt_blob
from novafabric.trust.novaseal.signing_backend import LocalSigningBackend
from novafabric.trust.tenant_keys import TenantKeyRegistry, tenant_from_object_key


def _write_kek(path: Path) -> Path:
    path.write_bytes(os.urandom(32))
    return path


@pytest.fixture()
def setup(tmp_path: Path) -> tuple[EncryptingAdapter, WormAdapter, Path]:
    from novafabric.object_capsule_store.backend_router import InMemoryWormAdapter

    default_kek = _write_kek(tmp_path / "default.kek")
    kek_dir = tmp_path / "tenants"
    kek_dir.mkdir()
    _write_kek(kek_dir / "acme.kek")
    _write_kek(kek_dir / "globex.kek")

    default_backend = LocalSigningBackend(default_kek, default_kek, kek_path=default_kek)
    inner = InMemoryWormAdapter()
    adapter = EncryptingAdapter(
        inner, default_backend, tenant_keys=TenantKeyRegistry(default_backend, kek_dir)
    )
    return adapter, inner, kek_dir


def _put(adapter: EncryptingAdapter, key: str, payload: bytes) -> None:
    adapter.put_object(key, payload, compute_sha256(payload), retention_days=1)


def test_tenant_from_object_key() -> None:
    assert tenant_from_object_key("capsules/acme/ab/abc123/data.zst") == "acme"
    assert tenant_from_object_key("_capsule_log/acme/run/000001.json") is None
    assert tenant_from_object_key("capsules/../evil/x") is None
    assert tenant_from_object_key("random-key") is None


def test_per_tenant_wrap_and_roundtrip(setup) -> None:
    adapter, inner, _ = setup
    _put(adapter, "capsules/acme/aa/sha1/data.zst", b"acme-secret")
    _put(adapter, "capsules/globex/bb/sha2/data.zst", b"globex-secret")

    # Both round-trip through their own keys.
    assert adapter.get_object("capsules/acme/aa/sha1/data.zst") == b"acme-secret"
    assert adapter.get_object("capsules/globex/bb/sha2/data.zst") == b"globex-secret"

    # The stored envelopes record the tenant and use DIFFERENT KEKs.
    env_a = json.loads(inner.get_object("capsules/acme/aa/sha1/data.zst"))
    env_g = json.loads(inner.get_object("capsules/globex/bb/sha2/data.zst"))
    assert env_a["tenant_key_id"] == "acme"
    assert env_g["tenant_key_id"] == "globex"
    assert env_a["kek_ref"] != env_g["kek_ref"]


def test_unconfigured_tenant_falls_back_to_default(setup) -> None:
    adapter, inner, _ = setup
    _put(adapter, "capsules/newco/cc/sha3/data.zst", b"newco-data")
    env = json.loads(inner.get_object("capsules/newco/cc/sha3/data.zst"))
    assert env["tenant_key_id"] is None  # flat mode for unconfigured tenants
    assert adapter.get_object("capsules/newco/cc/sha3/data.zst") == b"newco-data"


def test_revocation_is_tenant_scoped_and_fails_closed(setup) -> None:
    adapter, _, kek_dir = setup
    _put(adapter, "capsules/acme/aa/sha1/data.zst", b"acme-secret")
    _put(adapter, "capsules/globex/bb/sha2/data.zst", b"globex-secret")

    (kek_dir / "acme.kek").unlink()  # revoke / shred acme's KEK

    with pytest.raises(DekUnwrapError, match="acme"):
        adapter.get_object("capsules/acme/aa/sha1/data.zst")
    # Blast radius is one tenant: globex is untouched.
    assert adapter.get_object("capsules/globex/bb/sha2/data.zst") == b"globex-secret"


def test_pre_0243_envelope_still_readable(setup, tmp_path: Path) -> None:
    adapter, inner, _ = setup
    # A flat-mode envelope written before tenant keys existed: no
    # tenant_key_id field at all in the stored JSON.
    default_backend = adapter._backend  # noqa: SLF001 — the fixture's own backend
    blob = encrypt_blob(b"old-data", backend=default_backend)
    stored = blob.model_dump_json(exclude={"tenant_key_id"}).encode()
    inner.put_object(
        "capsules/acme/dd/sha4/data.zst",
        stored,
        compute_sha256(stored),
        retention_days=1,
        content_type="application/json",
    )
    assert adapter.get_object("capsules/acme/dd/sha4/data.zst") == b"old-data"


def test_env_wiring_fails_closed_on_missing_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kek = _write_kek(tmp_path / "kek.bin")
    monkeypatch.setenv("NOVA_OBJECT_STORE_ENCRYPTION", "1")
    monkeypatch.setenv("NOVA_OBJECT_STORE_KEK_PATH", str(kek))
    monkeypatch.setenv("NOVA_OBJECT_STORE_TENANT_KEK_DIR", str(tmp_path / "nope"))
    with pytest.raises(ValueError, match="not a directory"):
        make_adapter("local")


def test_env_wiring_builds_tenant_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kek = _write_kek(tmp_path / "kek.bin")
    kek_dir = tmp_path / "tenants"
    kek_dir.mkdir()
    _write_kek(kek_dir / "acme.kek")
    monkeypatch.setenv("NOVA_OBJECT_STORE_ENCRYPTION", "1")
    monkeypatch.setenv("NOVA_OBJECT_STORE_KEK_PATH", str(kek))
    monkeypatch.setenv("NOVA_OBJECT_STORE_TENANT_KEK_DIR", str(kek_dir))
    adapter = make_adapter("local")
    assert isinstance(adapter, EncryptingAdapter)
    payload = b"wired"
    adapter.put_object(
        "capsules/acme/aa/s/data.zst", payload, compute_sha256(payload), retention_days=1
    )
    assert adapter.get_object("capsules/acme/aa/s/data.zst") == payload
