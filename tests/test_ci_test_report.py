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

"""scripts/ci_test_report.py — the checks that run after CI's unit shards."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "ci_test_report.py"

_JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest">
  <testcase classname="tests.test_a" name="test_ok" file="tests/test_hermetic_colour_env.py" time="1.5"/>
  <testcase classname="tests.test_a" name="test_two" file="tests/test_hermetic_colour_env.py" time="0.5"/>
  <testcase classname="tests.test_b" name="test_bad" file="tests/test_shard_plugin.py" time="3.0">
    <failure message="boom">boom</failure>
  </testcase>
  <testcase classname="tests.test_b" name="test_skip" file="tests/test_shard_plugin.py" time="0.0">
    <skipped message="no docker"/>
  </testcase>
</testsuite></testsuites>
"""


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _write(path: Path, lines: list[str]) -> Path:
    path.write_text("".join(f"{line}\n" for line in lines))
    return path


@pytest.fixture
def junit(tmp_path: Path) -> Path:
    path = tmp_path / "junit-shard-1.xml"
    path.write_text(_JUNIT)
    return path


# ---------------------------------------------------------------------------
# check-manifests: the partition proof
# ---------------------------------------------------------------------------


def test_partition_passes(tmp_path: Path) -> None:
    expected = _write(tmp_path / "all.txt", ["a::1", "a::2", "b::1"])
    s1 = _write(tmp_path / "s1.txt", ["a::1", "a::2"])
    s2 = _write(tmp_path / "s2.txt", ["b::1"])
    res = _run("check-manifests", "--expected", str(expected), str(s1), str(s2))
    assert res.returncode == 0, res.stderr
    assert "partition the suite" in res.stdout


@pytest.mark.parametrize(
    ("shards", "complaint"),
    [
        ([["a::1"], ["b::1"]], "never ran"),  # a::2 fell between shards
        ([["a::1", "a::2"], ["a::2", "b::1"]], "more than one shard"),
        ([["a::1", "a::2"], ["b::1", "c::9"]], "not in the reference collection"),
    ],
)
def test_partition_failures_are_named(
    tmp_path: Path, shards: list[list[str]], complaint: str
) -> None:
    expected = _write(tmp_path / "all.txt", ["a::1", "a::2", "b::1"])
    paths = [str(_write(tmp_path / f"s{i}.txt", ids)) for i, ids in enumerate(shards)]
    res = _run("check-manifests", "--expected", str(expected), *paths)
    assert res.returncode == 1
    assert complaint in res.stderr


def test_an_empty_reference_collection_is_a_failure_not_a_pass(tmp_path: Path) -> None:
    expected = _write(tmp_path / "all.txt", [])
    shard = _write(tmp_path / "s1.txt", [])
    res = _run("check-manifests", "--expected", str(expected), str(shard))
    assert res.returncode == 1
    assert "selected nothing" in res.stderr


# ---------------------------------------------------------------------------
# durations
# ---------------------------------------------------------------------------


def test_durations_sum_per_file_and_merge(tmp_path: Path, junit: Path) -> None:
    out = tmp_path / "d.json"
    out.write_text(
        json.dumps(
            {
                "version": 1,
                "files": {
                    "tests/test_ci_test_report.py": 7.0,  # absent from XML: kept
                    "tests/test_shard_plugin.py": 99.0,  # in XML: replaced
                    "tests/deleted_long_ago.py": 5.0,  # not on disk: dropped
                },
            }
        )
    )
    res = _run("durations", "--out", str(out), str(junit))
    assert res.returncode == 0, res.stderr
    assert json.loads(out.read_text()) == {
        "version": 1,
        "files": {
            "tests/test_ci_test_report.py": 7.0,
            "tests/test_hermetic_colour_env.py": 2.0,
            "tests/test_shard_plugin.py": 3.0,
        },
    }


def test_durations_refuse_reports_without_file_attributes(tmp_path: Path) -> None:
    xml = tmp_path / "j.xml"
    xml.write_text('<testsuites><testsuite><testcase name="t" time="1"/></testsuite></testsuites>')
    res = _run("durations", "--out", str(tmp_path / "d.json"), str(xml))
    assert res.returncode == 1
    assert "junit_family=xunit1" in res.stderr


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


def test_summary_names_failures_and_slow_files(junit: Path) -> None:
    res = _run("summary", str(junit))
    assert res.returncode == 0, res.stderr
    out = res.stdout
    assert "❌ failed" in out
    assert "4 tests · 1 failed · 1 skipped" in out
    assert "`tests.test_b::test_bad`" in out
    assert "| junit-shard-1 | 5 |" in out
    assert out.index("tests/test_shard_plugin.py") < out.index("tests/test_hermetic_colour_env.py")


def test_summary_of_a_green_run(tmp_path: Path) -> None:
    xml = tmp_path / "j.xml"
    xml.write_text(
        '<testsuites><testsuite><testcase classname="c" name="t" file="tests/x.py" time="1"/>'
        "</testsuite></testsuites>"
    )
    res = _run("summary", str(xml))
    assert "✅ passed" in res.stdout
    assert "Failures" not in res.stdout
