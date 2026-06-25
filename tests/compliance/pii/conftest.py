# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""PII test fixtures."""

from __future__ import annotations

import pytest

from novafabric.compliance.pii.detector import RegexDetector
from novafabric.compliance.pii.gate import PIIDetectionGate


@pytest.fixture
def pepper() -> bytes:
    return b"test-pepper-do-not-use-in-prod"


@pytest.fixture
def regex_detector() -> RegexDetector:
    return RegexDetector()


@pytest.fixture
def pii_gate(tmp_path, pepper) -> PIIDetectionGate:
    from novafabric.compliance.pii.detector import RegexDetector
    return PIIDetectionGate(
        detector=RegexDetector(),
        pepper=pepper,
        legal_hold_dir=tmp_path / "legal_hold",
    )
