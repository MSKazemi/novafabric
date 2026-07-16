"""File store for saved views — plain files under ``.novafabric/views/`` (ADR-0130 D2).

One small human-readable file per view (YAML default, JSON equally valid),
intended to be committed to the project repo. The file is the source of truth;
there is no database. Saving is **fail-closed**: the embedded query object must
compile through the ADR-0129 parser before anything is written. A broken view
file fails only its own command and never blocks any other operation.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from novafabric.query.parser import validate_query_object
from novafabric.views.errors import ViewExistsError, ViewNotFoundError, ViewParseError
from novafabric.views.model import SavedView, is_valid_view_id

logger = logging.getLogger(__name__)

#: Recognized on-disk view file suffixes, in resolution order.
VIEW_SUFFIXES: tuple[str, ...] = (".yaml", ".yml", ".json")

_SLUG_RUN_RE = re.compile(r"[^a-z0-9]+")


def default_views_dir() -> Path:
    """Project-level views directory: ``$PWD/.novafabric/views``.

    Override with ``NOVAFABRIC_VIEWS_DIR``. Follows the existing project-config
    convention (``.novafabric/`` in the working directory, ADR-0029/ADR-0130);
    created lazily on first ``nova view save``.
    """
    env = os.environ.get("NOVAFABRIC_VIEWS_DIR")
    return Path(env) if env else Path.cwd() / ".novafabric" / "views"


def slugify_view_name(name: str) -> str:
    """Derive a ``view_id`` slug from a human-facing name (spec §Edge cases).

    Lowercase; runs of non-``[a-z0-9]`` collapse to a single ``-``; leading and
    trailing ``-`` trimmed. Raises :class:`ViewParseError` when nothing usable
    remains (the caller should ask for an explicit ``--view-id``).
    """
    slug = _SLUG_RUN_RE.sub("-", name.lower()).strip("-")
    if not slug or not is_valid_view_id(slug):
        raise ViewParseError(
            f"cannot derive a view id from name {name!r}; pass an explicit --view-id"
        )
    return slug


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_view_file(path: Path) -> SavedView:
    """Load one view file into a validated :class:`SavedView` (fail closed)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ViewParseError(f"cannot read view file {path}: {exc}") from exc
    try:
        if path.suffix.lower() == ".json":
            obj = json.loads(text)
        else:
            obj = yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ViewParseError(f"view file {path} is not valid JSON/YAML: {exc}") from exc
    if not isinstance(obj, dict):
        raise ViewParseError(f"view file {path} must contain a single saved-view object")
    try:
        return SavedView.model_validate(obj)
    except ValueError as exc:
        raise ViewParseError(f"view file {path} is not a valid saved view: {exc}") from exc


def _view_files(views_dir: Path) -> list[Path]:
    """All candidate view files, sorted for deterministic listings."""
    if not views_dir.is_dir():
        return []
    return sorted(p for p in views_dir.iterdir() if p.is_file() and p.suffix in VIEW_SUFFIXES)


def _files_for_id(views_dir: Path, view_id: str) -> list[Path]:
    """Existing files whose stem is ``view_id`` (any recognized suffix)."""
    return [
        views_dir / f"{view_id}{suffix}"
        for suffix in VIEW_SUFFIXES
        if (views_dir / f"{view_id}{suffix}").is_file()
    ]


def resolve_view_file(ref: str, views_dir: Path | None = None) -> Path:
    """Find the file for a view referenced by ``view_id`` or ``name``.

    Filename-stem match wins first (the spec says the stem SHOULD equal
    ``view_id``); otherwise every parseable view file is scanned for a matching
    ``view_id`` or ``name`` inside (``view_id`` inside the file wins over the
    filename). Corrupt files are skipped during the scan — they fail only their
    own direct commands, never an unrelated lookup.
    """
    base = views_dir if views_dir is not None else default_views_dir()
    direct = _files_for_id(base, ref)
    if direct:
        return direct[0]
    for path in _view_files(base):
        try:
            view = _parse_view_file(path)
        except ViewParseError:
            continue
        if ref in (view.view_id, view.name):
            return path
    known = ", ".join(p.stem for p in _view_files(base)) or "none"
    raise ViewNotFoundError(
        f"no saved view named {ref!r} in {base} (known views: {known})"
    )


def load_view(ref: str, views_dir: Path | None = None) -> SavedView:
    """Load a saved view by ``view_id`` or ``name`` (envelope-validated)."""
    return _parse_view_file(resolve_view_file(ref, views_dir))


def list_views(views_dir: Path | None = None) -> tuple[list[SavedView], list[str]]:
    """All parseable views plus warnings for files that failed to parse.

    A missing views directory is an empty listing, not an error; a corrupt
    file is reported as a warning and skipped (non-blocking, ADR-0130).
    """
    base = views_dir if views_dir is not None else default_views_dir()
    views: list[SavedView] = []
    warnings: list[str] = []
    for path in _view_files(base):
        try:
            views.append(_parse_view_file(path))
        except ViewParseError as exc:
            warnings.append(str(exc))
    views.sort(key=lambda v: v.view_id)
    return views, warnings


def save_view(
    view: SavedView,
    views_dir: Path | None = None,
    *,
    as_json: bool = False,
    force: bool = False,
) -> Path:
    """Persist a saved view — fail closed, refuse silent overwrites.

    The embedded ``query`` object must compile through the ADR-0129 parser
    (:func:`novafabric.query.parser.validate_query_object`) **before** anything
    touches disk; an invalid query is a
    :class:`~novafabric.query.errors.QueryParseError` and nothing is written.

    An existing ``view_id`` is refused unless ``force``; on ``force`` the
    original ``created_at`` is preserved, ``updated_at`` is set, and any
    old file for the same ``view_id`` in another format is removed.
    """
    validate_query_object(view.query, source=f"saved view {view.view_id!r}")

    base = views_dir if views_dir is not None else default_views_dir()
    existing = _files_for_id(base, view.view_id)
    if existing and not force:
        raise ViewExistsError(
            f"view {view.view_id!r} already exists ({existing[0]}); "
            "use --force to overwrite, or pass a different --view-id"
        )
    if existing:
        try:
            previous = _parse_view_file(existing[0])
            view = view.model_copy(
                update={"created_at": previous.created_at, "updated_at": _now_iso()}
            )
        except ViewParseError:
            # The old file is corrupt — a forced save simply replaces it.
            logger.warning("overwriting corrupt view file %s", existing[0])

    base.mkdir(parents=True, exist_ok=True)
    document = view.to_document()
    if as_json:
        path = base / f"{view.view_id}.json"
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    else:
        path = base / f"{view.view_id}.yaml"
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    for old in existing:
        if old != path:
            old.unlink()
    return path


def delete_view(ref: str, views_dir: Path | None = None) -> Path:
    """Remove the file backing a saved view; returns the removed path."""
    path = resolve_view_file(ref, views_dir)
    path.unlink()
    return path
