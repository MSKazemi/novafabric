"""Named exception classes for the saved-views layer (ADR-0130)."""

from __future__ import annotations


class ViewError(Exception):
    """Base class for all saved-view errors."""


class ViewNotFoundError(ViewError):
    """No saved view matches the requested id or name."""


class ViewExistsError(ViewError):
    """A saved view with this ``view_id`` already exists (use ``--force``)."""


class ViewParseError(ViewError):
    """A view file is corrupt, unparseable, or fails envelope validation."""
