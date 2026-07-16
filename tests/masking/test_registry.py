"""Masker registration: entry points + dotted paths, fail-closed (ADR-0135 D2)."""
from __future__ import annotations

import logging
from typing import Any

import pytest

from novafabric.masking import (
    MaskerRegistrationError,
    MaskerSpec,
    load_maskers,
    resolve_masker,
)
from novafabric.masking._registry import ENTRY_POINT_GROUP


def test_resolve_by_dotted_colon_path() -> None:
    masker = resolve_masker(MaskerSpec(id="novafabric.masking.examples:EmailMasker"))
    assert masker.masker_id == "novafabric-email"
    assert masker.pattern_ids == ("email-address",)


def test_resolve_by_dotted_attr_path() -> None:
    masker = resolve_masker(MaskerSpec(id="novafabric.masking.examples.EmailMasker"))
    assert masker.masker_id == "novafabric-email"


def test_unknown_masker_fails_closed() -> None:
    with pytest.raises(MaskerRegistrationError, match="not found"):
        resolve_masker(MaskerSpec(id="no-such-masker-anywhere"))


def test_unimportable_dotted_path_fails_closed() -> None:
    with pytest.raises(MaskerRegistrationError, match="not found"):
        resolve_masker(MaskerSpec(id="no.such.module:Masker"))


def test_invalid_masker_contract_fails_closed() -> None:
    # _Unchanged instantiates fine but is not a masker: no masker_id, no mask().
    with pytest.raises(MaskerRegistrationError, match="does not satisfy"):
        resolve_masker(MaskerSpec(id="novafabric.masking._models:_Unchanged"))


def test_entry_point_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    from novafabric.masking import _registry
    from novafabric.masking import examples as _examples

    class FakeEntryPoint:
        name = "acme-masker"

        @staticmethod
        def load() -> Any:
            return _examples.EmailMasker

    def fake_entry_points(*, group: str) -> list[Any]:
        assert group == ENTRY_POINT_GROUP
        return [FakeEntryPoint()]

    monkeypatch.setattr(_registry.metadata, "entry_points", fake_entry_points)
    masker = resolve_masker(MaskerSpec(id="acme-masker"))
    assert masker.masker_id == "novafabric-email"


def test_entry_point_load_failure_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from novafabric.masking import _registry

    class BrokenEntryPoint:
        name = "broken-masker"

        @staticmethod
        def load() -> Any:
            raise ImportError("broken wheel")

    monkeypatch.setattr(
        _registry.metadata, "entry_points", lambda *, group: [BrokenEntryPoint()]
    )
    with pytest.raises(MaskerRegistrationError, match="failed to load"):
        resolve_masker(MaskerSpec(id="broken-masker"))


def test_version_mismatch_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="novafabric.masking._registry"):
        resolve_masker(
            MaskerSpec(id="novafabric.masking.examples:EmailMasker", version="999")
        )
    assert any("version mismatch" in rec.message for rec in caplog.records)


def test_uninstantiable_class_fails_closed() -> None:
    # MaskerSpec is a pydantic model requiring 'id' — instantiation with no
    # args raises, which must surface as a registration error.
    with pytest.raises(MaskerRegistrationError, match="could not be instantiated"):
        resolve_masker(MaskerSpec(id="novafabric.masking._models:MaskerSpec"))


def test_load_maskers_preserves_declared_order() -> None:
    specs = [
        MaskerSpec(id="novafabric.masking.examples:EmailMasker"),
        # tests/ is on pythonpath (pyproject [tool.pytest.ini_options]), so the
        # fixture masker is importable as masking.conftest.
        MaskerSpec(id="masking.conftest:CaseIdMasker"),
    ]
    loaded = load_maskers(specs)
    assert [lm.masker.masker_id for lm in loaded] == ["novafabric-email", "acme-case-id"]
