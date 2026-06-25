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

import yaml

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
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    controls: list[Control] = []
    for ctrl_data in data.get("controls", []):
        requirements: list[EvidenceRequirement] = []
        for req_data in ctrl_data.get("evidence_requirements", []):
            requirements.append(
                EvidenceRequirement(
                    evidence_type=req_data["evidence_type"],
                    required=req_data.get("required", True),
                    description=req_data["description"],
                )
            )
        controls.append(
            Control(
                id=ctrl_data["id"],
                title=ctrl_data["title"],
                description=ctrl_data["description"],
                framework=data["framework"],
                evidence_requirements=requirements,
                weight=ctrl_data.get("weight", 1.0),
            )
        )

    return ControlProfile(
        id=data["id"],
        name=data["name"],
        framework=data["framework"],
        version=str(data["version"]),
        controls=controls,
    )
