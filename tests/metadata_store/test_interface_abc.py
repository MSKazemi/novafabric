"""Tests for the MetadataStore abstract base class and typed exceptions.

TDD — written before implementation. These tests must fail with ImportError first,
then pass after interface.py and __init__.py are created.
"""
from __future__ import annotations

import pytest

from novafabric.metadata_store import (
    BackendModeError,
    MetadataStore,
    MigrationVerificationFailed,
    RLSContextMissing,
)

# ---------------------------------------------------------------------------
# Test 1: partial subclass (missing abstract methods) raises TypeError
# ---------------------------------------------------------------------------

class _PartialStore(MetadataStore):
    """Subclass that only implements one of the abstract methods."""

    def register_run(self, run_id, tenant_id, **fields):
        pass


def test_partial_subclass_raises_type_error_on_instantiation():
    """A subclass that leaves abstract methods unimplemented cannot be instantiated."""
    with pytest.raises(TypeError):
        _PartialStore()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# Test 2: __abstractmethods__ is exactly the specified set
# ---------------------------------------------------------------------------

EXPECTED_ABSTRACT_METHODS = {
    "register_run",
    "lookup_run",
    "query_runs",
    "begin_tenant_context",
    "record_signature",
    "bootstrap",
    "health_check",
}


def test_abstract_methods_are_exactly_the_required_set():
    """MetadataStore.__abstractmethods__ must equal the canonical FR-01 set."""
    assert MetadataStore.__abstractmethods__ == frozenset(EXPECTED_ABSTRACT_METHODS)


# ---------------------------------------------------------------------------
# Test 3: exception classes are subclasses of Exception
# ---------------------------------------------------------------------------

def test_rls_context_missing_is_exception_subclass():
    assert issubclass(RLSContextMissing, Exception)


def test_backend_mode_error_is_exception_subclass():
    assert issubclass(BackendModeError, Exception)


def test_migration_verification_failed_is_exception_subclass():
    assert issubclass(MigrationVerificationFailed, Exception)


# ---------------------------------------------------------------------------
# Test 4: exception messages are accessible via str()
# ---------------------------------------------------------------------------

def test_rls_context_missing_message_accessible():
    msg = "transaction required"
    exc = RLSContextMissing(msg)
    assert str(exc) == msg


def test_backend_mode_error_message_accessible():
    msg = "SQLite does not support multi-worker"
    exc = BackendModeError(msg)
    assert str(exc) == msg


def test_migration_verification_failed_message_accessible():
    msg = "checksum mismatch after migration"
    exc = MigrationVerificationFailed(msg)
    assert str(exc) == msg


# ---------------------------------------------------------------------------
# Test 5: a fully-implemented concrete subclass can be instantiated
# ---------------------------------------------------------------------------

class _ConcreteStore(MetadataStore):
    """Minimal concrete implementation used only for instantiation verification."""

    def register_run(self, run_id, tenant_id, **fields):
        pass

    def lookup_run(self, run_id, tenant_id):
        return None

    def query_runs(self, tenant_id, *, limit=50, cursor=None, **filters):
        return [], None

    def begin_tenant_context(self, tenant_id):
        from contextlib import nullcontext
        return nullcontext(self)

    def record_signature(self, run_id, tenant_id, signature_hash, payload):
        pass

    def bootstrap(self):
        pass

    def health_check(self):
        return {"status": "ok", "backend": "concrete_stub"}


def test_fully_implemented_subclass_can_be_instantiated():
    """A complete implementation should not raise on instantiation."""
    store = _ConcreteStore()
    assert isinstance(store, MetadataStore)


def test_health_check_returns_status_and_backend():
    store = _ConcreteStore()
    result = store.health_check()
    assert "status" in result
    assert "backend" in result
    assert result["status"] in ("ok", "degraded")
