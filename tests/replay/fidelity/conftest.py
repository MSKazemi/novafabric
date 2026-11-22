"""Session-scoped corpus for the replay fidelity tests (issue #59).

Each scenario is captured and replayed once per session, on first use, so ``-k S3``
runs only S3. Set ``NOVA_FIDELITY_BASELINE=<path>`` to write a JSON summary of every
scenario and control that ran (oracle results, report counters, verdict) at session end.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from replay.fidelity._harness import Corpus, Harness

BASELINE_ENV = "NOVA_FIDELITY_BASELINE"


@pytest.fixture(scope="session")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Corpus]:
    harness = Harness(tmp_path_factory.mktemp("replay-fidelity"))
    corpus = Corpus(harness)
    try:
        yield corpus
    finally:
        harness.close()
        target = os.environ.get(BASELINE_ENV)
        if target:
            Path(target).write_text(json.dumps(corpus.baseline(), indent=2, sort_keys=True))
