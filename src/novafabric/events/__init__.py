"""Lifecycle event emitter & webhooks (ADR-0137, experimental).

Opt-in, outbound, fail-safe surface: on defined lifecycle transitions
NovaFabric emits one structured, non-sensitive ``LifecycleEvent`` to a local
append-only ``events.jsonl`` and, optionally, to user-configured webhook URLs.

Nothing is emitted until the user configures a sink (``NOVA_EVENTS_LOG`` /
``NOVA_EVENTS_WEBHOOK``) — the default is a no-op ``NullSink``. Emission is
emit-and-forget: a sink failure is logged and swallowed, never raised into the
capture/validate/promote workload path.
"""

from novafabric.events.adapters import (
    PAGERDUTY_SEVERITY,
    Adapter,
    build_email_message,
    render_pagerduty,
    render_slack,
    send_email,
)
from novafabric.events.alerts import (
    OPS_EVENT_TYPES,
    AlertEndpoint,
    AlertRouter,
    AlertsConfig,
    emit_ops_alert,
    load_alerts_config_from_env,
)
from novafabric.events.emitter import (
    EventsConfig,
    LifecycleEventEmitter,
    build_emitter_from_env,
    emit_lifecycle_event,
    load_config_from_env,
)
from novafabric.events.model import (
    SCHEMA_VERSION,
    EventSeverity,
    EventType,
    LifecycleEvent,
    Signature,
    Subject,
    SubjectKind,
)
from novafabric.events.signing import canonical_body, sign_record, verify_record
from novafabric.events.sinks import (
    EventSink,
    FileSink,
    MultiSink,
    NullSink,
    WebhookSink,
)

__all__ = [
    "OPS_EVENT_TYPES",
    "PAGERDUTY_SEVERITY",
    "SCHEMA_VERSION",
    "Adapter",
    "AlertEndpoint",
    "AlertRouter",
    "AlertsConfig",
    "EventSeverity",
    "EventSink",
    "EventType",
    "EventsConfig",
    "FileSink",
    "LifecycleEvent",
    "LifecycleEventEmitter",
    "MultiSink",
    "NullSink",
    "Signature",
    "Subject",
    "SubjectKind",
    "WebhookSink",
    "build_email_message",
    "build_emitter_from_env",
    "canonical_body",
    "emit_lifecycle_event",
    "emit_ops_alert",
    "load_alerts_config_from_env",
    "load_config_from_env",
    "render_pagerduty",
    "render_slack",
    "send_email",
    "sign_record",
    "verify_record",
]
