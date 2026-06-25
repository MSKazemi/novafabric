# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""Tests for PII detector implementations (detector.py coverage targets)."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from novafabric.compliance.pii.detector import (
    CustomDetector,
    PIISpan,
    PresidioDetector,
    make_detector,
)


class TestPresidioDetector:
    def test_init_stores_config(self) -> None:
        d = PresidioDetector(entities=["EMAIL_ADDRESS"], language="en")
        assert d._entities == ["EMAIL_ADDRESS"]
        assert d._language == "en"
        assert d._analyzer is None

    def test_init_defaults(self) -> None:
        d = PresidioDetector()
        assert d._entities is None
        assert d._language == "en"

    def test_ensure_analyzer_raises_when_presidio_absent(self) -> None:
        d = PresidioDetector()
        with patch.dict(sys.modules, {"presidio_analyzer": None}):
            with pytest.raises(ImportError, match="Presidio Analyzer is not installed"):
                d._ensure_analyzer()

    def test_detect_delegates_to_analyzer(self) -> None:
        mock_result = MagicMock()
        mock_result.start = 0
        mock_result.end = 5
        mock_result.entity_type = "EMAIL_ADDRESS"
        mock_result.score = 0.9

        mock_analyzer = MagicMock()
        mock_analyzer.analyze.return_value = [mock_result]

        mock_engine_cls = MagicMock(return_value=mock_analyzer)
        mock_module = MagicMock()
        mock_module.AnalyzerEngine = mock_engine_cls

        d = PresidioDetector(entities=["EMAIL_ADDRESS"])
        with patch.dict(sys.modules, {"presidio_analyzer": mock_module}):
            spans = d.detect("hello world")

        assert len(spans) == 1
        assert spans[0].entity_type == "EMAIL_ADDRESS"
        assert spans[0].start == 0


class TestCustomDetector:
    def test_wraps_callable(self) -> None:
        def my_fn(text: str) -> list[PIISpan]:
            return [PIISpan(start=0, end=3, entity_type="CUSTOM", score=1.0)]

        d = CustomDetector(my_fn)
        spans = d.detect("abc def")
        assert len(spans) == 1
        assert spans[0].entity_type == "CUSTOM"

    def test_empty_return(self) -> None:
        d = CustomDetector(lambda t: [])
        assert d.detect("no pii here") == []


class TestMakeDetector:
    def test_returns_regex_by_default(self) -> None:
        from novafabric.compliance.pii.detector import RegexDetector

        d = make_detector()
        assert isinstance(d, RegexDetector)

    def test_returns_regex_explicit(self) -> None:
        from novafabric.compliance.pii.detector import RegexDetector

        d = make_detector({"backend": "regex"})
        assert isinstance(d, RegexDetector)

    def test_returns_presidio_detector(self) -> None:
        d = make_detector({"backend": "presidio", "entities": ["EMAIL_ADDRESS"]})
        assert isinstance(d, PresidioDetector)
        assert d._entities == ["EMAIL_ADDRESS"]

    def test_unknown_backend_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown PII detector backend"):
            make_detector({"backend": "unknown"})
