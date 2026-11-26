"""Session-scoped corpus for the replay fidelity tests (issue #59).

Each scenario is captured and replayed once per session, on first use, so ``-k S3``
runs only S3. Set ``NOVA_FIDELITY_BASELINE=<path>`` to write a JSON summary of every
scenario and control that ran (oracle results, report counters, verdict) at session end;
add ``NOVA_FIDELITY_EVIDENCE=1`` for provenance and the hashed raw oracle files
(``replay.fidelity._report`` turns baselines into the per-surface report).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from replay.fidelity import _report
from replay.fidelity._harness import Corpus, Harness

BASELINE_ENV = "NOVA_FIDELITY_BASELINE"
#: ``1``: also write provenance and copy the raw oracle files (hashed) beside the baseline.
EVIDENCE_ENV = "NOVA_FIDELITY_EVIDENCE"


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
            baseline = corpus.baseline()
            if os.environ.get(EVIDENCE_ENV) == "1":
                # Raw oracle files, hashed, next to the baseline: <baseline stem>.evidence/
                evidence_dir = Path(target).with_name(Path(target).stem + ".evidence")
                baseline["provenance"] = _report.provenance()
                baseline["evidence"] = {
                    "files": _report.collect_evidence(harness.root, evidence_dir)
                }
            Path(target).write_text(json.dumps(baseline, indent=2, sort_keys=True))
