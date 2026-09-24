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
"""Widgets and dashboards as portable, versioned JSON files — ADR-0235.

**Files are the storage, not an export format.** There is no database table of
user dashboards: creating one writes a file, editing one writes the file, and
`nova dashboard export` hands you the bytes that were already on disk. That is
what makes ADR-0235 D1's portability real rather than nominal — an export
feature over a database is a feature, whereas files *are* the thing.

It also preserves the removability property `non-goals.md` claims: deleting
`$NOVAFABRIC_HOME/dashboards` removes every user dashboard and leaves nothing
behind in a schema someone has to migrate.

## A widget is untrusted input (D7)

Widget files are *designed* to be shared, which means they will be shared by
people who should not be trusted. The threat model is "someone pastes a widget
from the internet", and the answer is structural rather than advisory:

1. the file is validated against the published JSON Schema **before any use**;
2. the embedded query is then handed to :func:`novafabric.query.validate_query_object`,
   so it inherits ADR-0129's **closed allow-list** — a widget cannot express a
   query the CLI itself forbids;
3. there is no user-supplied code, no raw SQL, and no templating that reaches
   the filesystem. The one field that touches a path — ``id`` — is constrained
   by the schema to ``[a-z0-9._-]``, so it cannot traverse.

## Unknown fields are preserved, never dropped (D6)

A file carrying fields from a newer version round-trips through an older
``nova dashboard export`` unchanged. Without that, a mixed-version team silently
destroys each other's work through ordinary use — one member opens and re-saves,
and another member's newer settings are gone with no error anywhere.

This is why :class:`Widget` keeps ``raw`` and every write path serialises *that*
rather than re-rendering from parsed fields.
"""

from __future__ import annotations

from novafabric.dashboards._models import (
    Dashboard,
    DashboardError,
    Widget,
    WidgetValidationError,
    load_dashboard,
    load_widget,
)
from novafabric.dashboards._store import (
    DashboardStore,
    apply_path,
    default_store,
)

__all__ = [
    "Dashboard",
    "DashboardError",
    "DashboardStore",
    "Widget",
    "WidgetValidationError",
    "apply_path",
    "default_store",
    "load_dashboard",
    "load_widget",
]
