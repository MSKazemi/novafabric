"""``seal_for_log_claims`` decides from ONE verification of the capsule.

It used to call ``capsule_seal_status`` twice (first bare, then with ``must_cover``).
A capsule that changed between the two calls had its second ``"invalid"`` read as
``"unbound"`` ("valid seal that does not bind the streams"), so a seal that had just
stopped verifying produced an export instead of a refusal. A single verification
cannot disagree with itself.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from novafabric.compliance.export._seal_guard import CapsuleSealInvalidError, seal_for_log_claims
from novafabric.trust.novaseal import capsule_verify

from .test_seal_invalid_is_refused import _sealed_capsule


def _count_verifications(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    real = capsule_verify.verify_capsule
    calls: list[Path] = []

    def counting(capsule_dir: Path, *a: Any, **kw: Any) -> Any:
        calls.append(capsule_dir)
        return real(capsule_dir, *a, **kw)

    monkeypatch.setattr(capsule_verify, "verify_capsule", counting)
    return calls


def _flip_after_first(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Verify genuinely once, then behave as if the capsule were edited meanwhile."""
    real = capsule_verify.verify_capsule
    calls: list[Path] = []

    def flipping(capsule_dir: Path, *a: Any, **kw: Any) -> Any:
        calls.append(capsule_dir)
        if len(calls) == 1:
            return real(capsule_dir, *a, **kw)
        raise RuntimeError("the capsule changed between two verifications")

    monkeypatch.setattr(capsule_verify, "verify_capsule", flipping)
    return calls


def test_a_bound_seal_is_verified_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _sealed_capsule(tmp_path)
    calls = _count_verifications(monkeypatch)
    assert seal_for_log_claims(cap) == "bound"
    assert len(calls) == 1


def test_an_unbound_seal_is_verified_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _sealed_capsule(tmp_path, bind_evidence=False)
    calls = _count_verifications(monkeypatch)
    assert seal_for_log_claims(cap) == "unbound"
    assert len(calls) == 1


def test_a_capsule_edited_between_two_verifications_is_not_reported_unbound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = _sealed_capsule(tmp_path)
    _flip_after_first(monkeypatch)
    # one verification -> the answer is that verification's, never an "unbound" made
    # of a valid first reading and an invalid second one
    assert seal_for_log_claims(cap) == "bound"


@pytest.mark.parametrize(
    "make",
    [lambda r: _sealed_capsule(r), lambda r: _sealed_capsule(r, bind_evidence=False)],
)
def test_a_failing_verification_still_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make: Callable[[Path], Path]
) -> None:
    cap = make(tmp_path)

    def broken(capsule_dir: Path, *a: Any, **kw: Any) -> Any:
        raise RuntimeError("cannot verify")

    monkeypatch.setattr(capsule_verify, "verify_capsule", broken)
    with pytest.raises(CapsuleSealInvalidError):
        seal_for_log_claims(cap)
