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
"""H5: ``nova lineage-store migrate --from-ocs`` must never report success for nothing.

The command built an empty in-memory ObjectCapsuleStore, never read
``--ocs-data-dir``, and printed ``ok loaded=0 … committed=yes`` with exit 0.
NovaFabric has no on-disk ObjectCapsuleStore backend for it to read, so it now
refuses with a non-zero exit and writes nothing. L12: the group help named
targets (Postgres, KuzuDB) the command does not have.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def _migrate(tmp_path: Path, *extra: str) -> object:
    return runner.invoke(
        app,
        [
            "lineage-store", "migrate", "--from-ocs",
            "--ocs-tenant", "org", "--ocs-run-ids", "run-1,run-2",
            "--db", str(tmp_path / "lineage.db"), *extra,
        ],
    )


@pytest.mark.parametrize("mode", ["--dry-run", "--commit"])
def test_from_ocs_refuses_instead_of_reporting_an_empty_success(
    tmp_path: Path, mode: str
) -> None:
    result = _migrate(tmp_path, mode)
    assert isinstance(result.exception, SystemExit), result.exception
    assert result.exit_code == 1, result.output
    text = " ".join(result.output.split())
    assert "not implemented" in text
    assert "ok loaded" not in text
    assert "committed=yes" not in text
    assert not (tmp_path / "lineage.db").exists(), "the target store was created"


def test_from_ocs_refuses_even_when_the_data_dir_holds_capsules(tmp_path: Path) -> None:
    """The directory is named in the refusal, so the operator knows it was not read."""
    data = tmp_path / "ocs"
    (data / "org" / "run-1").mkdir(parents=True)
    (data / "org" / "run-1" / "capsule.zip").write_bytes(b"PK\x03\x04")
    result = _migrate(tmp_path, "--commit", "--ocs-data-dir", str(data))
    assert result.exit_code == 1, result.output
    assert str(data) in "".join(result.output.split())
    assert not (tmp_path / "lineage.db").exists()


def test_from_ocs_refuses_before_checking_its_other_arguments(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["lineage-store", "migrate", "--from-ocs", "--db", str(tmp_path / "l.db")]
    )
    assert result.exit_code == 1, result.output
    assert "not implemented" in " ".join(result.output.split())


def test_group_help_names_only_the_sqlite_target() -> None:
    """L12: the group's own description names SQLite as the migration target, nothing else."""
    from novafabric.cli.lineage_migrate import lineage_store_app

    result = runner.invoke(app, ["lineage-store", "--help"])
    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    assert "Lineage store migration (into a SQLite lineage store)" in text
    assert "Postgres" not in text
    assert "Postgres" not in str(lineage_store_app.info.help)


def test_migrate_help_says_from_ocs_is_not_implemented() -> None:
    result = runner.invoke(app, ["lineage-store", "migrate", "--help"])
    assert result.exit_code == 0, result.output
    assert "not implemented" in " ".join(result.output.split()).lower()
