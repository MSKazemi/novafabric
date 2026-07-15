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
"""First-class incident object with Art. 73 deadline clock (ADR-0088, gap-010).

Experimental. Self-contained: everything here works offline against the SQLite
store under ``NOVAFABRIC_HOME``.
"""

from novafabric.compliance.incident.clock import (
    DeadlineClock,
    Obligation,
    ObligationDeadline,
)
from novafabric.compliance.incident.models import (
    Incident,
    IncidentNotFoundError,
    IncidentSeverity,
    IncidentStatus,
    IncidentTransitionError,
)
from novafabric.compliance.incident.store import IncidentStore, incidents_db_path

__all__ = [
    "DeadlineClock",
    "Incident",
    "IncidentNotFoundError",
    "IncidentSeverity",
    "IncidentStatus",
    "IncidentStore",
    "IncidentTransitionError",
    "Obligation",
    "ObligationDeadline",
    "incidents_db_path",
]
