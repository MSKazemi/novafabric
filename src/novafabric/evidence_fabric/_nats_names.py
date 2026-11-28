"""Default NATS JetStream names for the evidence stream.

One place for the names every evidence-stream client defaults to, so the
producer and the consumers cannot drift apart again. The Go spool forwarder
(``collector/cmd/novafabric-spool-forwarder``) creates the stream under
:data:`DEFAULT_EVIDENCE_STREAM` and publishes on
``nova.evidence.<run_id>``; ``nova collector rebuild``
(:class:`~novafabric.evidence_fabric.rebuild.OffsetReplayRebuilder`) and
:class:`~novafabric.evidence_fabric.nats_consumer.NATSJetStreamConsumer`
read it. ``tests/evidence_fabric/test_evidence_stream_name.py`` pins the Go
default to this value.

The UPPER_SNAKE form follows the stream names in ADR-0061 (``NOVA_CAPSULES``,
``NOVA_LINEAGE``). ``NOVA_NATS_STREAM`` / ``--stream`` still override it.
"""

from __future__ import annotations

#: JetStream stream holding evidence events (created by the spool forwarder).
DEFAULT_EVIDENCE_STREAM = "NOVA_EVIDENCE"

#: Subject filter covering every run's evidence subject ``nova.evidence.<run_id>``.
DEFAULT_EVIDENCE_SUBJECT = "nova.evidence.>"
