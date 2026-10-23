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

"""Dataset-provenance facet + benchmark-contamination check (NF-028, ADR-0108).

Records the **dataset + split content hashes** an eval ran against so a capsule can be
flagged when it was run on a *contaminated* or *superseded* benchmark version
(contamination silently inflates scores; a provenance fabric should surface it). This is
detection/flagging only — no remediation.

The facet is stored additively under the capsule's
``extensions/dev.novafabric.dataset-provenance/`` namespace (run-capsule-v1 §extensions);
absence of the facet leaves today's behavior unchanged. ``check_contamination`` compares
each facet's hashes against a **configurable** known-contaminated/superseded registry
(no hardcoded URL) and resolves a status per dataset.

The check **fails closed**: a facet file that cannot be read, parsed or validated —
or a facet namespace that is not a directory — is reported as an ``invalid`` verdict
with a reason and is flagged exactly like contamination. Skipping it would let a
capsule whose only provenance record is unreadable pass a CI contamination gate.

What counts as unreadable, so that a facet cannot be shaped to slip past the gate:

* **Hashes are compared in one canonical form**, ``sha256:<lowercase hex>``. A
  facet or registry hash may be written with surrounding whitespace, in upper
  case, or without the ``sha256:`` prefix; it is normalised before the compare.
  A value that is not 4-64 hex digits after that cannot be normalised: the facet
  is ``invalid`` (a registry entry like it is a usage error).
* **A duplicate JSON key** (``{"dataset_hash": "...", "dataset_hash": ""}``)
  makes the facet ``invalid``: a JSON parser keeps the last value, a human reader
  sees the first.
* **Facet files match ``*.json`` case-insensitively** (``m.JSON`` is a facet).
* **The namespace is flat.** A subdirectory in it is ``invalid``: a facet placed
  one level down would otherwise never be checked.
* **No link is followed and nothing blocks** (:mod:`novafabric._capsule_fs`): a
  facet that is a link, a FIFO, a device or larger than :data:`MAX_FACET_BYTES`
  is ``invalid``, as is a namespace (or ``extensions``) that is a link.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from novafabric._capsule_fs import (
    CapsuleEntryError,
    make_capsule_dir,
    read_capsule_file,
    replace_capsule_file,
)

_log = logging.getLogger(__name__)

Status = Literal["current", "superseded", "contaminated", "unknown"]

#: A check verdict: a facet ``Status``, or ``invalid`` for a facet the check could
#: not read or validate. ``invalid`` is never a recordable facet status.
Verdict = Literal["current", "superseded", "contaminated", "unknown", "invalid"]

#: Verdicts that make the check non-OK (exit 4).
FLAGGED_VERDICTS: frozenset[str] = frozenset({"contaminated", "superseded", "invalid"})

#: additive capsule namespace for the facet (run-capsule-v1 §extensions).
FACET_DIRNAME = Path("extensions") / "dev.novafabric.dataset-provenance"

#: Largest facet file the check reads. A facet is a few hundred bytes.
MAX_FACET_BYTES: Final[int] = 1024 * 1024

_HEX_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{4,64}")
_PREFIX: Final[str] = "sha256:"


class DuplicateKeyError(ValueError):
    """A JSON object names the same key twice."""


def canonical_hash(value: str) -> str:
    """Normalise a dataset hash to ``sha256:<lowercase hex>``; ``""`` stays ``""``.

    Surrounding whitespace, upper case and a missing ``sha256:`` prefix are
    accepted. Raises ``ValueError`` for anything else, so two spellings of one
    hash can never compare unequal.
    """
    text = value.strip().lower()
    if not text:
        return ""
    if text.startswith(_PREFIX):
        text = text[len(_PREFIX) :].strip()
    if not _HEX_RE.fullmatch(text):
        raise ValueError(
            f"not a sha256 hash (expected 4-64 hex digits, optional 'sha256:' prefix): "
            f"{value[:80]!r}"
        )
    return _PREFIX + text


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in pairs:
        if key in out:
            raise DuplicateKeyError(f"duplicate key {key[:80]!r}")
        out[key] = val
    return out


def _loads_strict(raw: bytes | str) -> Any:
    """``json.loads`` that refuses a duplicate key (``DuplicateKeyError``)."""
    return json.loads(raw, object_pairs_hook=_reject_duplicate_keys)


# contaminated is the most severe; unknown the least.
_SEVERITY: dict[str, int] = {"contaminated": 3, "superseded": 2, "current": 1, "unknown": 0}


class DatasetProvenanceFacet(BaseModel):
    """A ``dataset_provenance`` facet (NF-028 R6)."""

    name: str = Field(min_length=1)
    version: str = ""
    dataset_hash: str = ""
    split_hash: str = ""
    status: Status = "unknown"

    @field_validator("dataset_hash", "split_hash")
    @classmethod
    def _canonical(cls, value: str) -> str:
        return canonical_hash(value)


class ContaminationRegistry(BaseModel):
    """A configurable known-contaminated/superseded hash registry (NF-028 R8).

    ``contaminated`` / ``superseded`` are lists of ``sha256:`` hashes; a facet whose
    ``dataset_hash`` **or** ``split_hash`` appears in a list resolves to that status.
    """

    contaminated: list[str] = Field(default_factory=list)
    superseded: list[str] = Field(default_factory=list)

    @field_validator("contaminated", "superseded")
    @classmethod
    def _canonical(cls, values: list[str]) -> list[str]:
        return [h for h in (canonical_hash(v) for v in values) if h]

    @classmethod
    def from_path(cls, path: Path) -> ContaminationRegistry:
        """Load a registry file. Raises ``ValueError`` (incl. a duplicate key) on bad input."""
        data = _loads_strict(path.read_text(encoding="utf-8"))
        return cls.model_validate(data)

    def resolve(self, facet: DatasetProvenanceFacet) -> Status:
        hashes = {h for h in (facet.dataset_hash, facet.split_hash) if h}
        if hashes & set(self.contaminated):
            return "contaminated"
        if hashes & set(self.superseded):
            return "superseded"
        if hashes:
            return "current"
        return "unknown"


class ContaminationResult(BaseModel):
    """Per-dataset contamination outcome.

    ``reason`` is set only for an ``invalid`` verdict and says why the facet
    could not be checked.
    """

    name: str
    version: str
    dataset_hash: str
    split_hash: str
    status: Verdict
    reason: str | None = None


class InvalidFacet(BaseModel):
    """A facet file (or facet namespace) that could not be read or validated."""

    path: Path
    reason: str


def write_facet(capsule_dir: str | Path, facet: DatasetProvenanceFacet) -> Path:
    """Write a facet into the capsule's dataset-provenance extension namespace (additive).

    Never follows a link inside the capsule: each directory of the namespace is
    created with ``mkdirat(2)`` under an ``O_NOFOLLOW`` walk (a linked
    ``extensions/`` is refused with :class:`~novafabric._capsule_fs.CapsuleLinkError`),
    and the facet file is replaced by an atomic rename, so a link or hard link
    planted at its name is replaced, never written through.
    """
    capsule_dir = Path(capsule_dir)
    capsule_dir.mkdir(parents=True, exist_ok=True)
    parts = FACET_DIRNAME.parts
    for depth in range(1, len(parts) + 1):
        make_capsule_dir(capsule_dir, PurePosixPath(*parts[:depth]), operation="write_facet")
    # deterministic filename from the dataset name (slugified).
    slug = "".join(c if c.isalnum() else "-" for c in facet.name.lower()).strip("-") or "dataset"
    replace_capsule_file(
        capsule_dir,
        PurePosixPath(*parts, f"{slug}.json"),
        facet.model_dump_json(indent=2).encode("utf-8"),
        operation="write_facet",
    )
    return capsule_dir / FACET_DIRNAME / f"{slug}.json"


def _load_facet(capsule_dir: Path, name: str) -> DatasetProvenanceFacet | InvalidFacet:
    """Parse one facet file; never raises — a failure becomes an ``InvalidFacet``.

    Read through :func:`novafabric._capsule_fs.read_capsule_file`: no link on the
    path is followed, a FIFO or device is refused without blocking, and the read
    stops at :data:`MAX_FACET_BYTES`.
    """
    path = Path(capsule_dir) / FACET_DIRNAME / name
    rel = PurePosixPath(*FACET_DIRNAME.parts, name)
    try:
        raw = read_capsule_file(
            Path(capsule_dir), rel, max_bytes=MAX_FACET_BYTES, operation="contamination-check"
        )
    except CapsuleEntryError as exc:
        return InvalidFacet(path=path, reason=f"could not read {name}: {str(exc)[:200]}")
    except OSError as exc:
        return InvalidFacet(path=path, reason=f"could not read {name}: {exc.strerror or exc}")
    try:
        data = _loads_strict(raw)
    except DuplicateKeyError as exc:
        return InvalidFacet(path=path, reason=f"{name} is not a valid facet: {exc}")
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError
        return InvalidFacet(path=path, reason=f"{name} is not valid JSON: {str(exc)[:200]}")
    try:
        return DatasetProvenanceFacet.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        )
        return InvalidFacet(path=path, reason=f"{name} is not a valid facet: {problems[:400]}")


def _namespace_problem(capsule_dir: Path) -> str | None:
    """Why the facet namespace cannot be scanned, ``""`` if absent, ``None`` if fine.

    Each component (``extensions``, then the namespace) is checked with
    ``lstat``: a link is never followed into, even to a directory.
    """
    path = capsule_dir
    for part in FACET_DIRNAME.parts:
        path = path / part
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return ""
        except OSError as exc:
            return f"{path.name} could not be read: {exc.strerror or exc}"
        if stat.S_ISLNK(mode):
            return f"{part} is a symlink; the check never follows one"
        if not stat.S_ISDIR(mode):
            return f"facet namespace {FACET_DIRNAME} is not a directory"
    return None


def scan_facets(
    capsule_dir: str | Path,
) -> tuple[list[DatasetProvenanceFacet], list[InvalidFacet]]:
    """Read every facet in a capsule, keeping the ones that failed alongside the valid.

    Returns ``(valid, invalid)``. A facet namespace that exists but is not a
    directory is itself one ``InvalidFacet``. Never raises.
    """
    capsule_dir = Path(capsule_dir)
    facet_dir = capsule_dir / FACET_DIRNAME
    valid: list[DatasetProvenanceFacet] = []
    invalid: list[InvalidFacet] = []
    problem = _namespace_problem(capsule_dir)
    if problem == "":
        return valid, invalid
    if problem is not None:
        invalid.append(InvalidFacet(path=facet_dir, reason=problem))
        return valid, invalid
    try:
        with os.scandir(facet_dir) as it:
            entries = sorted((e.name, e.is_dir(follow_symlinks=False)) for e in it)
    except OSError as exc:
        reason = f"facet namespace could not be listed: {exc.strerror or exc}"
        invalid.append(InvalidFacet(path=facet_dir, reason=reason))
        return valid, invalid
    for name, is_subdir in entries:
        if is_subdir:
            # The namespace is flat (write_facet never nests): a facet one level
            # down would otherwise go unchecked, so the subdirectory fails closed.
            reason = (
                f"{name}/ is a subdirectory of the facet namespace; facets are flat "
                "files and a nested facet is never checked"
            )
            invalid.append(InvalidFacet(path=facet_dir / name, reason=reason))
            continue
        if not name.lower().endswith(".json"):
            continue
        loaded = _load_facet(capsule_dir, name)
        if isinstance(loaded, InvalidFacet):
            _log.warning("dataset-provenance facet rejected: %s", loaded.reason)
            invalid.append(loaded)
        else:
            valid.append(loaded)
    return valid, invalid


def read_facets(capsule_dir: str | Path) -> list[DatasetProvenanceFacet]:
    """Read the *valid* dataset-provenance facets of a capsule (empty when none).

    Malformed facets are left out here. A caller that must not treat "unreadable"
    as "absent" — any gate — uses :func:`scan_facets` or :func:`check_contamination`,
    which report them.
    """
    return scan_facets(capsule_dir)[0]


def check_contamination(
    capsule_dir: str | Path, registry: ContaminationRegistry | None
) -> list[ContaminationResult]:
    """Resolve each capsule dataset facet's status against *registry* (NF-028 R8).

    With no registry, the resolved status is each facet's own recorded ``status``
    (so a facet pre-flagged ``contaminated`` still surfaces). With a registry, the
    registry decision wins when it is more severe than the recorded status.

    Fails closed: each facet that could not be read or validated yields an
    ``invalid`` result with a ``reason``; no registry can clear it.
    """
    valid, invalid = scan_facets(capsule_dir)
    results: list[ContaminationResult] = []
    for facet in valid:
        resolved: Status = facet.status
        if registry is not None:
            reg_status = registry.resolve(facet)
            if _SEVERITY[reg_status] > _SEVERITY[resolved]:
                resolved = reg_status
        results.append(
            ContaminationResult(
                name=facet.name,
                version=facet.version,
                dataset_hash=facet.dataset_hash,
                split_hash=facet.split_hash,
                status=resolved,
            )
        )
    for bad in invalid:
        results.append(
            ContaminationResult(
                name=bad.path.stem,
                version="",
                dataset_hash="",
                split_hash="",
                status="invalid",
                reason=bad.reason,
            )
        )
    return results


def is_flagged(results: list[ContaminationResult]) -> bool:
    """True when any dataset is contaminated, superseded or invalid (drives exit code 4)."""
    return any(r.status in FLAGGED_VERDICTS for r in results)


def as_dicts(results: list[ContaminationResult]) -> list[dict[str, Any]]:
    return [r.model_dump() for r in results]
