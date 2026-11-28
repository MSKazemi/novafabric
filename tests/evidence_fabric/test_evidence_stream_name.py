"""The evidence stream has one default name on the producer and consumer sides.

Regression: the Go spool forwarder created and published to ``NOVA_EVIDENCE``
while ``nova collector rebuild`` and ``NATSJetStreamConsumer`` defaulted to
``nova-evidence``, so a default deployment's consumers bound to a stream that
did not exist. This pins every side to
:data:`novafabric.evidence_fabric._nats_names.DEFAULT_EVIDENCE_STREAM`.

The Go side is checked by reading the forwarder source (no Go toolchain needed).
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from novafabric.evidence_fabric._nats_names import (
    DEFAULT_EVIDENCE_STREAM,
    DEFAULT_EVIDENCE_SUBJECT,
)
from novafabric.evidence_fabric.rebuild import OffsetReplayRebuilder, rebuild_from_events

REPO_ROOT = Path(__file__).resolve().parents[2]
FORWARDER_MAIN = REPO_ROOT / "collector" / "cmd" / "novafabric-spool-forwarder" / "main.go"

#: ``nova-evidence`` as a stream name; ``nova-evidence-consumer`` (the durable
#: consumer name) is a different, unchanged identifier.
_OLD_STREAM_NAME = re.compile(r"nova-evidence(?![-\w])")

#: Surfaces that state the default. CHANGELOG.md is history and is not scanned;
#: the built dashboard bundle under serve/static is regenerated from
#: generatedCommands.ts and is not scanned either.
_SCANNED = (
    "docs/collector-reference.md",
    "docs/cli-reference.md",
    "docs/glossary.md",
    "docs/architecture/deployment-topologies.md",
    "src/novafabric/cli/collector.py",
    "src/novafabric/evidence_fabric/nats_consumer.py",
    "src/novafabric/evidence_fabric/rebuild.py",
    "ui/dashboard/src/components/dashboard/commands/generatedCommands.ts",
    "collector/cmd/novafabric-spool-forwarder/main.go",
)


def _go_const(name: str) -> str:
    source = FORWARDER_MAIN.read_text(encoding="utf-8")
    match = re.search(rf"\b{name}\s*=\s*\"([^\"]+)\"", source)
    assert match, f"{name} not found in {FORWARDER_MAIN.relative_to(REPO_ROOT)}"
    return match.group(1)


def test_canonical_stream_name() -> None:
    assert DEFAULT_EVIDENCE_STREAM == "NOVA_EVIDENCE"
    assert DEFAULT_EVIDENCE_SUBJECT == "nova.evidence.>"


def test_go_forwarder_defaults_match_python() -> None:
    assert _go_const("defaultEvidenceStream") == DEFAULT_EVIDENCE_STREAM
    assert _go_const("defaultEvidenceSubjectPrefix") + ".>" == DEFAULT_EVIDENCE_SUBJECT
    source = FORWARDER_MAIN.read_text(encoding="utf-8")
    assert 'envOr("NOVAFABRIC_SPOOL_STREAM", defaultEvidenceStream)' in source
    assert 'envOr("NOVAFABRIC_SPOOL_SUBJECT", defaultEvidenceSubjectPrefix)' in source


def test_rebuild_defaults_to_canonical_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOVA_NATS_STREAM", raising=False)
    monkeypatch.delenv("NOVA_NATS_SUBJECT", raising=False)
    params = inspect.signature(rebuild_from_events).parameters
    assert params["stream"].default == DEFAULT_EVIDENCE_STREAM
    assert params["subject"].default == DEFAULT_EVIDENCE_SUBJECT
    rebuilder = OffsetReplayRebuilder()
    assert rebuilder._stream == DEFAULT_EVIDENCE_STREAM
    assert rebuilder._subject == DEFAULT_EVIDENCE_SUBJECT


def test_rebuild_env_override_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_NATS_STREAM", "nova-evidence")
    assert OffsetReplayRebuilder()._stream == "nova-evidence"


def test_nats_consumer_defaults_to_canonical_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("nats")
    from novafabric.evidence_fabric.nats_consumer import NATSJetStreamConsumer

    monkeypatch.delenv("NOVA_NATS_STREAM", raising=False)
    monkeypatch.delenv("NOVA_NATS_SUBJECT", raising=False)
    consumer = NATSJetStreamConsumer(accumulator=object())
    assert consumer._stream_name == DEFAULT_EVIDENCE_STREAM
    assert consumer._subject == DEFAULT_EVIDENCE_SUBJECT


@pytest.mark.parametrize("rel", _SCANNED)
def test_no_surface_states_the_old_stream_name(rel: str) -> None:
    text = (REPO_ROOT / rel).read_text(encoding="utf-8")
    hits = [
        f"{rel}:{n}: {line.strip()}"
        for n, line in enumerate(text.splitlines(), 1)
        if _OLD_STREAM_NAME.search(line)
        # The migration note names the old default on purpose.
        and "the default changes" not in line
        and "relied on the old" not in line
        and "NOVA_NATS_STREAM=nova-evidence" not in line
    ]
    assert not hits, "old stream name 'nova-evidence' still stated:\n" + "\n".join(hits)
