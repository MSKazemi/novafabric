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
"""Profile YAML loader for the compliance audit engine."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .models import Control, ControlProfile, EvidenceRequirement

# Built-in profiles shipped with the package
_PROFILES_DIR = Path(__file__).parent / "profiles"

# Map of short profile IDs to YAML filenames
_PROFILE_FILENAMES: dict[str, str] = {
    "nist-ai-rmf": "nist-ai-rmf.yaml",
    "eu-ai-act-high-risk": "eu-ai-act-high-risk.yaml",
    "gdpr": "gdpr.yaml",
    "soc2-type2": "soc2-type2.yaml",
    "iso42001": "iso42001.yaml",
    "scientific-reproducibility": "scientific-reproducibility.yaml",
}

BUILT_IN_PROFILES = list(_PROFILE_FILENAMES.keys())


class ProfileValidationError(ValueError):
    """A profile YAML exists but is not a valid control profile (L25).

    A ``ValueError`` subclass, so every caller that already reports an unknown
    profile (``nova audit``, ``nova serve``'s 422) reports this too. The message
    names the file and the offending field.
    """

    def __init__(self, path: Path, problem: str) -> None:
        super().__init__(f"Invalid audit profile {path}: {problem}")
        self.path = path
        self.problem = problem


def list_profiles() -> list[str]:
    """Return the list of built-in profile IDs."""
    return list(_PROFILE_FILENAMES.keys())


def load_profile(profile_id_or_path: str) -> ControlProfile:
    """Load a :class:`ControlProfile` by ID or by file path.

    Args:
        profile_id_or_path: A built-in profile ID (e.g. ``"nist-ai-rmf"``) or
            an absolute/relative path to a custom YAML profile.

    Returns:
        Parsed and validated :class:`ControlProfile`.

    Raises:
        ValueError: If the profile ID is unknown and the path does not exist.
        ProfileValidationError: If the file is not valid YAML or not a valid
            control profile (missing field, wrong type).
    """
    path = _resolve_path(profile_id_or_path)
    return _parse_profile_yaml(path)


def _resolve_path(profile_id_or_path: str) -> Path:
    filename = _PROFILE_FILENAMES.get(profile_id_or_path)
    if filename is not None:
        return _PROFILES_DIR / filename
    # Treat as file path
    p = Path(profile_id_or_path)
    if not p.exists():
        known = ", ".join(sorted(_PROFILE_FILENAMES))
        raise ValueError(
            f"Unknown profile {profile_id_or_path!r}. "
            f"Built-in profiles: {known}. "
            f"Or provide a path to a YAML file."
        )
    return p


def _parse_profile_yaml(path: Path) -> ControlProfile:
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ProfileValidationError(path, f"not valid YAML ({exc})") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ProfileValidationError(path, f"cannot be read ({exc})") from exc
    try:
        return _build_profile(data)
    except ValidationError as exc:
        raise ProfileValidationError(path, _first_error(exc)) from exc
    except _ProfileShapeError as exc:
        raise ProfileValidationError(path, str(exc)) from exc


class _ProfileShapeError(Exception):
    """Internal: a structural problem in the profile mapping (wrapped with the path)."""


def _mapping(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _ProfileShapeError(f"{where} must be a mapping, got {type(value).__name__}")
    return value


def _sequence(value: object, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise _ProfileShapeError(f"{where} must be a list, got {type(value).__name__}")
    return value


def _required(data: dict[str, Any], key: str, where: str) -> Any:
    if key not in data:
        raise _ProfileShapeError(f"{where} is missing required field {key!r}")
    return data[key]


def _build_profile(raw: object) -> ControlProfile:
    data = _mapping(raw, "the profile document")
    framework = _required(data, "framework", "the profile")
    controls: list[Control] = []
    for index, raw_ctrl in enumerate(_sequence(data.get("controls", []), "'controls'")):
        where = f"controls[{index}]"
        ctrl_data = _mapping(raw_ctrl, where)
        requirements: list[EvidenceRequirement] = []
        raw_reqs = _sequence(
            ctrl_data.get("evidence_requirements", []), f"{where}.evidence_requirements"
        )
        for req_index, raw_req in enumerate(raw_reqs):
            req_where = f"{where}.evidence_requirements[{req_index}]"
            req_data = _mapping(raw_req, req_where)
            requirements.append(
                EvidenceRequirement(
                    evidence_type=_required(req_data, "evidence_type", req_where),
                    required=req_data.get("required", True),
                    description=_required(req_data, "description", req_where),
                )
            )
        controls.append(
            Control(
                id=_required(ctrl_data, "id", where),
                title=_required(ctrl_data, "title", where),
                description=_required(ctrl_data, "description", where),
                framework=framework,
                evidence_requirements=requirements,
                weight=ctrl_data.get("weight", 1.0),
            )
        )

    return ControlProfile(
        id=_required(data, "id", "the profile"),
        name=_required(data, "name", "the profile"),
        framework=framework,
        version=str(_required(data, "version", "the profile")),
        controls=controls,
    )


def _first_error(exc: ValidationError) -> str:
    """One readable line from a pydantic error: ``field: message``."""
    errors = exc.errors()
    if not errors:
        return str(exc)
    first = errors[0]
    field = ".".join(str(part) for part in first.get("loc", ())) or "value"
    return f"field {field!r}: {first.get('msg', 'invalid')}"
