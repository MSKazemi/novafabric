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

"""Shared fixtures for capsule Phase 3 tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure tests/capsule is on sys.path so helpers can be imported directly
_TESTS_CAPSULE_DIR = Path(__file__).parent
if str(_TESTS_CAPSULE_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_CAPSULE_DIR))

from helpers import make_child_manifest, make_parent_manifest, write_capsule  # noqa: F401, E402

from novafabric.capsule.ulid_util import new_ulid  # noqa: E402


@pytest.fixture
def spool_dir(tmp_path: Path) -> Path:
    """Return a temporary spool directory."""
    return tmp_path / "spool"


@pytest.fixture
def valid_parent_run_id() -> str:
    return new_ulid()


@pytest.fixture
def valid_child_run_id() -> str:
    return new_ulid()


@pytest.fixture
def valid_global_run_id() -> str:
    return new_ulid()


@pytest.fixture
def env_for_child(
    valid_global_run_id: str,
    valid_parent_run_id: str,
    tmp_path: Path,
) -> dict[str, str]:
    """Env dict for a child worker."""
    return {
        "NOVAFABRIC_GLOBAL_RUN_ID": valid_global_run_id,
        "NOVAFABRIC_PARENT_RUN_ID": valid_parent_run_id,
        "NOVAFABRIC_CAPSULE_DIR": str(tmp_path / "spool"),
        "NOVAFABRIC_RANK": "0",
        "NOVAFABRIC_WORLD_SIZE": "4",
        "NOVAFABRIC_DISTRIBUTION_ROLE": "WORKER",
    }


@pytest.fixture
def env_for_parent(
    valid_global_run_id: str,
    tmp_path: Path,
) -> dict[str, str]:
    """Env dict for a parent / driver process."""
    return {
        "NOVAFABRIC_GLOBAL_RUN_ID": valid_global_run_id,
        "NOVAFABRIC_CAPSULE_DIR": str(tmp_path / "spool"),
        "NOVAFABRIC_WORLD_SIZE": "4",
        "NOVAFABRIC_DISTRIBUTION_ROLE": "DRIVER",
    }
