"""Read-only evidence lookups over an explicit capsule-directory scope (issue #244).

**Experimental.** These are the three tools the stdio MCP evidence server
(:mod:`novafabric.mcp.evidence_server`) exposes. They are plain functions over an
:class:`EvidenceScope`, with no protocol code in them, so the safety rules can
be tested without a client.

The rules every tool follows:

* **Explicit scope.** The operator names one or more scope directories when the
  server starts. Each is resolved once to its real path. There is no default
  scope and no way for a client to widen it.
* **One capsule = one direct child of a scope directory.** A client names a
  capsule by its directory name, which must be a single path segment
  (:func:`novafabric._safe_path.require_path_segment`: no ``/``, ``..``, NUL,
  control character or absolute path). The child must not be a symbolic link and
  must resolve directly inside the scope directory
  (:func:`novafabric._safe_path.safe_child`). Files inside the capsule are read
  only through :mod:`novafabric._capsule_fs`, which never follows a link.
* **Read only.** Nothing here opens a file for writing, runs a subprocess,
  opens a Merkle log, a database or the network. Verification is
  :func:`novafabric.trust.novaseal.capsule_verify.verify_capsule` with no
  NovaSeal profile: the capsule alone, the same check ``nova verify`` runs on a
  machine without ``novaseal.yaml``.
* **No content.** No tool returns prompt, response, tool-argument or tool-result
  content. The summary returns an allowlist of manifest fields
  (:data:`SUMMARY_FIELDS`) and names the fields it withheld. Every returned
  string passes the ADR-0009 secret-redaction rule pack and has the scope
  directories' absolute paths replaced by ``<scope N>`` *before* it is
  length-capped, so a cap never cuts a secret or a path in two.
* **Bounded.** Pages, lists, strings and nesting depth have fixed caps, and so
  has the total size of each returned manifest value (:data:`MAX_NODES`,
  :data:`MAX_TEXT_CHARS`): YAML anchors let a few kilobytes on disk alias one
  mapping under every key, which would otherwise expand to
  ``MAX_ITEMS ** MAX_DEPTH`` values.

Errors a client can act on raise :class:`EvidenceToolError` with one of the
``code`` values in :data:`ERROR_CODES`.
"""

from __future__ import annotations

import datetime as _dt
import itertools
import math
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from novafabric._capsule_fs import is_capsule_regular_file, iter_capsule_dirs
from novafabric._capsule_manifest import CapsuleManifestError, read_capsule_manifest
from novafabric._safe_path import (
    PathEscapeError,
    UnsafePathSegmentError,
    is_safe_path_segment,
    safe_child,
)

__all__ = [
    "ERROR_CODES",
    "MAX_SCOPE_ROOTS",
    "SUMMARY_FIELDS",
    "VERIFICATION_LEVEL",
    "EvidenceScope",
    "EvidenceToolError",
    "ScopeError",
]

#: Most scope directories one server accepts.
MAX_SCOPE_ROOTS: Final[int] = 16
#: ``list_capsules`` page size when the client gives none, and the largest it may ask for.
DEFAULT_PAGE_SIZE: Final[int] = 50
MAX_PAGE_SIZE: Final[int] = 200
#: Longest string returned (characters); longer ones are cut and marked.
MAX_STRING_CHARS: Final[int] = 512
#: Most entries returned from any one list or mapping.
MAX_ITEMS: Final[int] = 200
#: Deepest nesting returned from a manifest value.
MAX_DEPTH: Final[int] = 3
#: Most verification errors returned.
MAX_ERRORS: Final[int] = 50
#: Most values (at any depth, keys included) one returned manifest value may hold.
MAX_NODES: Final[int] = 2000
#: Most characters of text (strings and keys) one returned manifest value may hold.
MAX_TEXT_CHARS: Final[int] = 64 * 1024

#: A directory is a capsule when it holds one of these as a regular file.
CAPSULE_MARKERS: Final[tuple[str, ...]] = ("capsule.yaml", "capsule.json", ".seal/manifest.dsse")

#: Manifest fields ``get_capsule_summary`` returns. Every other field is withheld
#: and named in ``withheld_fields``: ``command``, ``working_directory``, ``host``,
#: ``metadata``, ``error``, ``git``, ``inputs``/``outputs`` and extension blocks
#: can carry command-line secrets, local paths, host names or free text.
SUMMARY_FIELDS: Final[tuple[str, ...]] = (
    "schema_version",
    "run_id",
    "created_at",
    "finished_at",
    "duration_ms",
    "status",
    "exit_code",
    "capture_mode",
    "novafabric_version",
    "model_call_count",
    "tool_call_count",
    "mutating_tool_count",
    "usage_totals",
    "alias",
    "parent_run_id",
    "replay_of_run_id",
    "replay_mode",
    "session_id",
    "sequence",
    "deployment_environment",
)

#: What ``verify_capsule`` checks, stated in every verification result.
VERIFICATION_LEVEL: Final[str] = (
    "offline, from the capsule alone: DSSE signature, seal-to-directory binding and "
    "evidence digests, a carried Merkle inclusion proof and RFC 3161 token if present; "
    "no CA bundle, TSA anchor or sealer Merkle log is consulted, so signer identity "
    "and timestamp are at most self-asserted (see unanchored_claims)"
)

INVALID_ARGUMENT: Final[str] = "invalid_argument"
OUTSIDE_SCOPE: Final[str] = "outside_scope"
CAPSULE_NOT_FOUND: Final[str] = "capsule_not_found"
AMBIGUOUS_CAPSULE: Final[str] = "ambiguous_capsule"
CAPSULE_UNREADABLE: Final[str] = "capsule_unreadable"

#: Every ``EvidenceToolError.code`` (the server adds ``response_too_large`` and
#: ``internal_error``).
ERROR_CODES: Final[tuple[str, ...]] = (
    INVALID_ARGUMENT,
    OUTSIDE_SCOPE,
    CAPSULE_NOT_FOUND,
    AMBIGUOUS_CAPSULE,
    CAPSULE_UNREADABLE,
)

_OPERATION: Final[str] = "the MCP evidence server"
_TRUNCATED: Final[str] = "…[truncated]"
#: Returned in place of a whole manifest value over :data:`MAX_NODES` or
#: :data:`MAX_TEXT_CHARS`; never a part-way cut of it.
TOO_LARGE: Final[str] = "[value omitted: too large to summarize]"


class ScopeError(ValueError):
    """The scope directories given at start-up cannot be used."""


class EvidenceToolError(Exception):
    """A tool call the server refuses, with a stable machine-readable ``code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _clip(text: str) -> str:
    if len(text) <= MAX_STRING_CHARS:
        return text
    return text[: MAX_STRING_CHARS - len(_TRUNCATED)] + _TRUNCATED


class _OverBudget(Exception):
    """A value grew past :data:`MAX_NODES` or :data:`MAX_TEXT_CHARS` while being walked."""


@dataclass
class _Budget:
    nodes: int = MAX_NODES
    chars: int = MAX_TEXT_CHARS

    def spend(self, chars: int = 0) -> None:
        self.nodes -= 1
        self.chars -= chars
        if self.nodes < 0 or self.chars < 0:
            raise _OverBudget


def _bounded(value: Any) -> Any:
    """*value* as plain JSON, with lists, mappings, depth and total size capped.

    Strings are kept whole here. They are cut to :data:`MAX_STRING_CHARS` only
    in :meth:`EvidenceScope._scrub`, after secret redaction and scope-path
    replacement, so a cut can never leave the first part of a secret or a path
    that no longer matches. A value past the node or text budget is replaced as
    a whole by :data:`TOO_LARGE`; the walk stops there, so a YAML alias bomb
    costs at most the budget.
    """
    try:
        return _walk(value, 0, _Budget())
    except _OverBudget:
        return TOO_LARGE


def _walk(value: Any, depth: int, budget: _Budget) -> Any:
    if value is None or isinstance(value, bool | int):
        budget.spend()
        return value
    if isinstance(value, float):
        budget.spend()
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        budget.spend(len(value))
        return value
    if isinstance(value, _dt.datetime | _dt.date):
        budget.spend()
        return value.isoformat()
    if depth >= MAX_DEPTH:
        budget.spend()
        return "[nested value omitted]"
    if isinstance(value, dict):
        budget.spend()
        out: dict[str, Any] = {}
        for key, item in itertools.islice(value.items(), MAX_ITEMS):
            text = str(key)
            budget.spend(len(text))
            out[text] = _walk(item, depth + 1, budget)
        return out
    if isinstance(value, list | tuple):
        budget.spend()
        return [_walk(item, depth + 1, budget) for item in itertools.islice(value, MAX_ITEMS)]
    text = str(value)
    budget.spend(len(text))
    return text


def _int_arg(name: str, value: object, *, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvidenceToolError(INVALID_ARGUMENT, f"{name} must be an integer")
    if not low <= value <= high:
        raise EvidenceToolError(INVALID_ARGUMENT, f"{name} must be between {low} and {high}")
    return value


@dataclass(frozen=True)
class EvidenceScope:
    """The scope directories a server may read, as canonical real paths."""

    roots: tuple[Path, ...]

    @classmethod
    def from_paths(cls, paths: Sequence[str | os.PathLike[str]]) -> EvidenceScope:
        """Resolve the operator's scope directories once.

        Raises:
            ScopeError: no directory was given, too many were, one is not a
                directory, or one resolves to the filesystem root.
        """
        if not paths:
            raise ScopeError("at least one scope directory is required")
        if len(paths) > MAX_SCOPE_ROOTS:
            raise ScopeError(f"at most {MAX_SCOPE_ROOTS} scope directories are accepted")
        roots: list[Path] = []
        for given in paths:
            real = Path(os.path.realpath(os.fspath(given)))
            if not real.is_dir():
                raise ScopeError(f"scope {os.fspath(given)!r} is not a directory")
            if real == Path(real.anchor):
                raise ScopeError(
                    f"scope {os.fspath(given)!r} is the filesystem root; name the "
                    "directory that holds your capsules"
                )
            if real not in roots:
                roots.append(real)
        return cls(tuple(roots))

    # -- addressing ---------------------------------------------------------

    def _root_indices(self, root: object) -> list[int]:
        if root is None:
            return list(range(len(self.roots)))
        if isinstance(root, bool) or not isinstance(root, int):
            raise EvidenceToolError(INVALID_ARGUMENT, "root must be an integer scope index")
        if not 0 <= root < len(self.roots):
            raise EvidenceToolError(
                INVALID_ARGUMENT, f"root must be between 0 and {len(self.roots) - 1}"
            )
        return [root]

    def resolve(self, capsule: object, root: object = None) -> tuple[int, Path]:
        """The ``(scope index, directory)`` of the capsule named *capsule*.

        Raises:
            EvidenceToolError: ``invalid_argument`` for a name that is not one
                safe path segment, ``outside_scope`` for a link or a name that
                resolves outside its scope directory, ``capsule_not_found``,
                or ``ambiguous_capsule`` when more than one scope holds it and
                no *root* was given.
        """
        if not isinstance(capsule, str):
            raise EvidenceToolError(INVALID_ARGUMENT, "capsule must be a string")
        found: list[tuple[int, Path]] = []
        for index in self._root_indices(root):
            try:
                child = safe_child(self.roots[index], capsule, field="capsule")
            except UnsafePathSegmentError as exc:
                raise EvidenceToolError(INVALID_ARGUMENT, str(exc)) from None
            except PathEscapeError:
                raise EvidenceToolError(
                    OUTSIDE_SCOPE,
                    f"capsule {capsule!r} in scope {index} is a symbolic link or resolves "
                    "outside the scope directory; it is not followed",
                ) from None
            try:
                mode = child.lstat().st_mode
            except OSError:
                continue
            if stat.S_ISDIR(mode) and self._is_capsule(child):
                found.append((index, child))
        if not found:
            raise EvidenceToolError(CAPSULE_NOT_FOUND, f"no capsule named {capsule!r} in scope")
        if len(found) > 1:
            raise EvidenceToolError(
                AMBIGUOUS_CAPSULE,
                f"capsule {capsule!r} exists in scopes {[i for i, _ in found]}; pass root",
            )
        return found[0]

    @staticmethod
    def _is_capsule(directory: Path) -> bool:
        return any(is_capsule_regular_file(directory, marker) for marker in CAPSULE_MARKERS)

    # -- output hygiene -----------------------------------------------------

    def _scrub(self, value: Any) -> Any:
        """Replace scope paths with ``<scope N>``, mask secrets, then cut every string.

        The order matters: cutting first could leave the head of a secret or of
        a scope path too short for its pattern to match, and return it.
        """
        from novafabric.capture.secrets import redact_secrets_in_text  # noqa: PLC0415

        if isinstance(value, str):
            for index, root in enumerate(self.roots):
                value = value.replace(str(root), f"<scope {index}>")
            return _clip(redact_secrets_in_text(value))
        if isinstance(value, dict):
            return {self._scrub(k): self._scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._scrub(v) for v in value]
        return value

    def _clean(self, payload: dict[str, Any]) -> dict[str, Any]:
        cleaned: dict[str, Any] = self._scrub(payload)
        return cleaned

    # -- tools --------------------------------------------------------------

    def list_capsules(
        self, root: object = None, cursor: object = None, limit: object = None
    ) -> dict[str, Any]:
        """One page of the capsules directly under the scope directories.

        Linked entries and directories without a capsule marker are never
        listed. ``next_cursor`` is ``None`` on the last page. A capsule whose
        name is not one safe, decodable path segment (a non-UTF-8 byte name,
        a control or invisible formatting character) could never be named in
        a later call, so it is skipped and counted in ``skipped_unaddressable``.
        """
        page = _int_arg("limit", limit, default=DEFAULT_PAGE_SIZE, low=1, high=MAX_PAGE_SIZE)
        indices = self._root_indices(root)
        start_index, after = self._parse_cursor(cursor, indices)
        items: list[dict[str, Any]] = []
        next_cursor: str | None = None
        skipped = 0
        for index in indices:
            if index < start_index:
                continue
            for directory in iter_capsule_dirs(self.roots[index], markers=CAPSULE_MARKERS):
                name = directory.name
                if index == start_index and after is not None and name <= after:
                    continue
                if not is_safe_path_segment(name):
                    skipped += 1
                    continue
                if len(items) == page:
                    last = items[-1]
                    next_cursor = f"{last['root']}/{last['capsule']}"
                    break
                items.append(
                    {
                        "root": index,
                        "capsule": name,
                        "sealed": is_capsule_regular_file(directory, ".seal/manifest.dsse"),
                    }
                )
            if next_cursor is not None:
                break
        return self._clean(
            {
                # Built here and already bounded: at most MAX_PAGE_SIZE entries of
                # one safe path segment (<= 255 bytes) each.
                "capsules": items,
                "count": len(items),
                "next_cursor": next_cursor,
                "skipped_unaddressable": skipped,
            }
        )

    def _parse_cursor(self, cursor: object, indices: list[int]) -> tuple[int, str | None]:
        if cursor is None:
            return indices[0], None
        if not isinstance(cursor, str) or "/" not in cursor:
            raise EvidenceToolError(INVALID_ARGUMENT, "cursor is not one this server returned")
        head, _, name = cursor.partition("/")
        if not head.isdigit() or not name or int(head) not in indices:
            raise EvidenceToolError(INVALID_ARGUMENT, "cursor is not one this server returned")
        return int(head), name

    def get_capsule_summary(self, capsule: object, root: object = None) -> dict[str, Any]:
        """Allowlisted manifest fields of one capsule; never content, never verified here."""
        index, directory = self.resolve(capsule, root)
        try:
            manifest = read_capsule_manifest(directory, operation=_OPERATION)
        except CapsuleManifestError as exc:
            raise EvidenceToolError(
                CAPSULE_UNREADABLE,
                f"the capsule manifest {exc.rel} is present but cannot be used "
                "(a link, special file, over-size or not a mapping)",
            ) from None
        if manifest is None:
            raise EvidenceToolError(CAPSULE_UNREADABLE, "the capsule has no manifest")
        data = manifest.data
        fields = {key: _bounded(data[key]) for key in SUMMARY_FIELDS if key in data}
        withheld = sorted(
            str(key) for key in data if key not in SUMMARY_FIELDS and key != "evidence_digests"
        )
        digests = data.get("evidence_digests")
        evidence_files: dict[str, Any] | None = None
        if isinstance(digests, dict):
            names = sorted(str(key) for key in digests)
            evidence_files = {
                "count": len(names),
                "names": names[:MAX_ITEMS],
                "truncated": len(names) > MAX_ITEMS,
            }
        return self._clean(
            {
                "root": index,
                "capsule": directory.name,
                "manifest_source": manifest.source,
                "sealed": manifest.sealed,
                "signature_checked": False,
                "fields": fields,
                "withheld_fields": _bounded(withheld),
                "evidence_files": _bounded(evidence_files),
            }
        )

    def verify_capsule(self, capsule: object, root: object = None) -> dict[str, Any]:
        """Verify one capsule offline, from the capsule alone. Fails closed."""
        index, directory = self.resolve(capsule, root)
        base: dict[str, Any] = {
            "root": index,
            "capsule": directory.name,
            "verification_level": VERIFICATION_LEVEL,
        }
        try:
            seal_mode: int | None = (directory / ".seal").lstat().st_mode
        except FileNotFoundError:
            seal_mode = None
        except OSError:
            seal_mode = 0  # present but unreadable: refused below, never "unsealed"
        if seal_mode is None:
            return self._clean(
                {
                    **base,
                    "sealed": False,
                    "valid": False,
                    "errors": ["capsule is not sealed (no .seal/ directory)"],
                }
            )
        if not stat.S_ISDIR(seal_mode):
            # A linked or special .seal is planted, never written by NovaFabric:
            # refuse it before anything opens a member through it.
            return self._clean(
                {
                    **base,
                    "sealed": True,
                    "valid": False,
                    "errors": [".seal is a link or not a directory; it is not followed"],
                }
            )
        from novafabric.trust.novaseal import unanchored_claims  # noqa: PLC0415
        from novafabric.trust.novaseal.capsule_verify import verify_capsule  # noqa: PLC0415

        report = verify_capsule(directory, None)
        result = report.result
        errors = report.all_errors()
        return self._clean(
            {
                **base,
                "sealed": True,
                "valid": bool(report.valid),
                "signature_ok": bool(result.signature_ok),
                "capsule_binding_ok": bool(report.binding.ok),
                "capsule_id_ok": bool(report.capsule_id_ok),
                "timestamp_ok": result.timestamp_ok,
                "timestamp_trust": _bounded(result.timestamp_trust),
                "log_inclusion": _bounded(result.log_inclusion),
                "identity_trust": _bounded(result.identity_trust),
                "unanchored_claims": _bounded(list(unanchored_claims(result))),
                "errors": [str(e) for e in errors[:MAX_ERRORS]],
                "errors_truncated": len(errors) > MAX_ERRORS,
            }
        )
