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

"""Opt-in OTel GenAI content bridge (NF-033, ADR-0098).

By default NovaFabric emits GenAI spans with **no** message/choice content (ADR-0021
§4: full content is opt-in). When the operator opts in (`--capture-content`), message
text is routed through the **same ADR-0009 secret-redaction rules** the capsule sealer
uses (`capture.secrets`), and each message is size-bounded (ADR-0021 span-size cap), so
no secret and no unbounded blob ever reaches a span attribute.

Invariants:
- **Off ⇒ no content.** ``bridge_messages(msgs, enabled=False)`` returns ``None``; the
  emitter then omits the ``gen_ai.*.messages`` attribute entirely.
- **On ⇒ redacted + bounded.** Every string is passed through the secret rules and
  truncated to :data:`MAX_CONTENT_CHARS`.
"""

from __future__ import annotations

from typing import Any

# Per-message character cap (ADR-0021 span-size bound).
MAX_CONTENT_CHARS = 4000


def redact_text(text: str, *, strategy: str = "mask") -> str:
    """Apply the ADR-0009 secret-redaction rules to *text* (reuses `capture.secrets`)."""
    from novafabric.capture.secrets import _RULES, _replacement

    redacted = text
    for rule in _RULES:
        rule_id = rule["id"]
        redacted = rule["pattern"].sub(
            lambda m, rid=rule_id: _replacement(rid, strategy, m.group()),
            redacted,
        )
    return redacted


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)[:MAX_CONTENT_CHARS]
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    return value


def bridge_messages(
    messages: list[dict[str, Any]] | None, *, enabled: bool
) -> list[dict[str, Any]] | None:
    """Return redacted+bounded messages when *enabled*, else ``None`` (no content).

    A ``None`` return signals the emitter to omit the content attribute entirely
    (NF-033 "bridge off ⇒ no message attribute"). When enabled, every message is
    deep-redacted through the secret rules and string-bounded.
    """
    if not enabled or not messages:
        return None
    return [_redact_value(dict(m)) for m in messages if isinstance(m, dict)]
