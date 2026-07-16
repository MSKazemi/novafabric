"""Saved views — named, persisted ADR-0129 queries as plain files (ADR-0130).

A saved view wraps a verbatim ADR-0129 query object with a name and optional
advisory display preferences, stored one-per-file under
``.novafabric/views/`` (YAML default, JSON equally valid). Data, not code:
running a view is exactly running its stored query through the ADR-0129
engine. Spec: ``design/spec/saved-views-v0.md``. Status: **experimental**.
"""

from novafabric.views.errors import (
    ViewError,
    ViewExistsError,
    ViewNotFoundError,
    ViewParseError,
)
from novafabric.views.model import (
    VIEW_SCHEMA_VERSION,
    DisplayPrefs,
    SavedView,
    SortKey,
    view_hash,
)
from novafabric.views.store import (
    default_views_dir,
    delete_view,
    list_views,
    load_view,
    save_view,
    slugify_view_name,
)

__all__ = [
    "VIEW_SCHEMA_VERSION",
    "DisplayPrefs",
    "SavedView",
    "SortKey",
    "ViewError",
    "ViewExistsError",
    "ViewNotFoundError",
    "ViewParseError",
    "default_views_dir",
    "delete_view",
    "list_views",
    "load_view",
    "save_view",
    "slugify_view_name",
    "view_hash",
]
