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
"""PII detector protocol and implementations (cap-001).

No ML dependencies in the default path.  Presidio support is opt-in and lazy-loaded.
Never use cloud PII APIs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable


@dataclass(frozen=True)
class PIISpan:
    """A detected PII span within a text string."""

    start: int
    end: int
    entity_type: str
    score: float  # 0.0 – 1.0 confidence


@runtime_checkable
class PIIDetector(Protocol):
    """Protocol for PII detectors.  Implementations must be thread-safe."""

    def detect(self, text: str) -> list[PIISpan]:
        """Return all PII spans found in *text*."""
        ...


# ---------------------------------------------------------------------------
# Default regex patterns
# ---------------------------------------------------------------------------

_DEFAULT_PATTERNS: list[dict[str, Any]] = [
    {
        "entity_type": "EMAIL",
        "pattern": re.compile(
            r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
            re.IGNORECASE,
        ),
        "score": 0.85,
    },
    {
        "entity_type": "PHONE",
        "pattern": re.compile(
            r"(?:\+?1[\s\-.]?)?"  # optional country code
            r"\(?\d{3}\)?[\s\-.]?"  # area code
            r"\d{3}[\s\-.]?"  # exchange
            r"\d{4}",
        ),
        "score": 0.75,
    },
    {
        # Very simple "Firstname Lastname" pattern — catches obvious names.
        # Intentionally conservative (avoids excessive false positives).
        "entity_type": "PERSON",
        "pattern": re.compile(
            r"\b[A-Z][a-z]{2,}\s+[A-Z][a-z]{2,}\b",
        ),
        "score": 0.60,
    },
]


class RegexDetector:
    """Pure-regex PII detector.  Zero ML dependencies.

    Args:
        patterns: List of dicts with keys ``entity_type``, ``pattern`` (compiled),
                  and ``score``.  Defaults to the built-in EMAIL / PHONE / PERSON patterns.
    """

    def __init__(
        self,
        patterns: list[dict[str, Any]] | None = None,
    ) -> None:
        self._patterns = patterns if patterns is not None else _DEFAULT_PATTERNS

    def detect(self, text: str) -> list[PIISpan]:
        spans: list[PIISpan] = []
        for spec in self._patterns:
            for m in spec["pattern"].finditer(text):
                spans.append(
                    PIISpan(
                        start=m.start(),
                        end=m.end(),
                        entity_type=spec["entity_type"],
                        score=spec["score"],
                    )
                )
        return sorted(spans, key=lambda s: s.start)


class PresidioDetector:
    """Presidio-backed PII detector.

    Presidio (MIT licence, Tier A under ADR-0024) is an optional dependency.
    Install it with ``pip install presidio-analyzer``.

    This class performs a lazy import so that the ``novafabric`` base package
    does not require Presidio to be installed.

    Args:
        entities: Entities to recognise.  Defaults to all Presidio built-ins.
        language: Language for analysis.  Defaults to ``"en"``.
    """

    def __init__(
        self,
        entities: list[str] | None = None,
        language: str = "en",
    ) -> None:
        self._entities = entities
        self._language = language
        self._analyzer: Any = None

    def _ensure_analyzer(self) -> Any:
        if self._analyzer is None:
            try:
                from presidio_analyzer import AnalyzerEngine
            except ImportError as exc:
                raise ImportError(
                    "Presidio Analyzer is not installed.  "
                    "Install it with: pip install 'novafabric[compliance]'  "
                    "(or directly: pip install presidio-analyzer>=2.2)"
                ) from exc
            self._analyzer = AnalyzerEngine()
        return self._analyzer

    def detect(self, text: str) -> list[PIISpan]:
        analyzer = self._ensure_analyzer()
        results = analyzer.analyze(
            text=text,
            entities=self._entities,
            language=self._language,
        )
        return sorted(
            [
                PIISpan(
                    start=r.start,
                    end=r.end,
                    entity_type=r.entity_type,
                    score=r.score,
                )
                for r in results
            ],
            key=lambda s: s.start,
        )


class CustomDetector:
    """Wraps any operator-provided callable as a :class:`PIIDetector`.

    Args:
        fn: A callable that takes ``text: str`` and returns a list of
            :class:`PIISpan` objects.
    """

    def __init__(self, fn: Callable[[str], list[PIISpan]]) -> None:
        self._fn = fn

    def detect(self, text: str) -> list[PIISpan]:
        return self._fn(text)


def make_detector(config: dict[str, Any] | None = None) -> PIIDetector:
    """Factory that returns a :class:`PIIDetector` based on *config*.

    Config keys:
      ``backend``: ``"regex"`` (default) | ``"presidio"`` | ``"custom"``
      ``patterns``: list of pattern dicts (for ``regex`` backend)
      ``entities``: list of Presidio entity names (for ``presidio`` backend)
      ``language``: language code (for ``presidio`` backend, default ``"en"``)

    Returns:
        A configured :class:`PIIDetector` instance.
    """
    cfg = config or {}
    backend = cfg.get("backend", "regex")

    if backend == "regex":
        return RegexDetector(patterns=cfg.get("patterns"))
    if backend == "presidio":
        return PresidioDetector(
            entities=cfg.get("entities"),
            language=cfg.get("language", "en"),
        )
    raise ValueError(
        f"Unknown PII detector backend: {backend!r}. "
        "Supported backends: 'regex', 'presidio'."
    )
