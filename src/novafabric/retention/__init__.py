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
"""Data-retention policy scheduler (ADR-0134).

A WORM-aware, crypto-shred-integrated, audited sweep that applies the
ADR-0031 retention windows over time. Thin orchestration only: the policy is
ADR-0031, the shred mechanism is ADR-0069, the dual-object split is ADR-0062.
Local-first — no daemon; run ``nova retention apply`` from cron/systemd.
"""

from novafabric.retention.actions import SweepExecutor
from novafabric.retention.models import (
    Decision,
    ItemKind,
    PlannedDecision,
    RetentionAction,
    RetentionActionRecord,
    RetentionBinding,
    RetentionMatch,
    SweepItem,
    SweepOutcome,
)
from novafabric.retention.sweep import (
    EXPIRED_MARKER,
    HoldContext,
    enumerate_capsules,
    load_bindings,
    matches,
    plan_sweep,
)
from novafabric.retention.windows import (
    WindowParseError,
    compute_due_at,
    is_due,
    parse_iso_duration,
    parse_window,
)

__all__ = [
    "EXPIRED_MARKER",
    "Decision",
    "HoldContext",
    "ItemKind",
    "PlannedDecision",
    "RetentionAction",
    "RetentionActionRecord",
    "RetentionBinding",
    "RetentionMatch",
    "SweepExecutor",
    "SweepItem",
    "SweepOutcome",
    "WindowParseError",
    "compute_due_at",
    "enumerate_capsules",
    "is_due",
    "load_bindings",
    "matches",
    "parse_iso_duration",
    "parse_window",
    "plan_sweep",
]
