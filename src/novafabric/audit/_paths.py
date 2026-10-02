from __future__ import annotations

from pathlib import Path

# user-global: the XDG-style per-user audit log (~/.local/share), deliberately NOT
# under NOVAFABRIC_HOME, so a custom home cannot hide or fork the audit trail.
AUDIT_LOG_PATH: Path = Path.home() / ".local" / "share" / "novafabric" / "audit.jsonl"
