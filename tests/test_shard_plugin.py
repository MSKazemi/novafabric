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

"""``--shard=I/N`` splits the suite into a true partition (tests/_shard.py).

The property that matters is the one a CI split silently loses: every test runs
in exactly one shard. A test that falls between shards is never executed and
never reported, so it is asserted here over the pure functions *and* end to end
through the real conftest, with and without xdist.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
from _shard import (
    ShardSpec,
    ShardSpecError,
    assign,
    canonical_nodeid,
    file_of,
    load_durations,
    select,
    weights,
)

_REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# --shard parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "spec"), [("1/1", (1, 1)), ("3/6", (3, 6)), (" 2 / 4 ", (2, 4))])
def test_parse_accepts_index_over_total(text: str, spec: tuple[int, int]) -> None:
    assert ShardSpec.parse(text) == ShardSpec(*spec)


@pytest.mark.parametrize("text", ["", "3", "0/4", "5/4", "1/0", "a/b", "-1/3", "1/2/3"])
def test_parse_rejects_anything_else(text: str) -> None:
    with pytest.raises(ShardSpecError):
        ShardSpec.parse(text)


# ---------------------------------------------------------------------------
# weighting and assignment
# ---------------------------------------------------------------------------


def test_unknown_files_are_weighted_at_the_known_mean_per_test() -> None:
    # 10 s over 5 known tests = 2 s/test, so a 3-test file with no data weighs 6 s.
    w = weights({"a.py": 5, "new.py": 3}, {"a.py": 10.0})
    assert w == {"a.py": 10.0, "new.py": 6.0}


def test_without_any_durations_tests_count_as_one_second_each() -> None:
    assert weights({"a.py": 4, "b.py": 1}, {}) == {"a.py": 4.0, "b.py": 1.0}


def test_durations_for_deleted_files_are_ignored() -> None:
    assert weights({"a.py": 1}, {"a.py": 2.0, "gone.py": 99.0}) == {"a.py": 2.0}


def test_assign_is_a_partition_onto_valid_shards() -> None:
    files = {f"t{i}.py": float(i % 7 + 1) for i in range(50)}
    placement = assign(files, 6)
    assert set(placement) == set(files)
    assert set(placement.values()) <= set(range(1, 7))


def test_assign_balances_largest_first() -> None:
    # LPT's guarantee: the heaviest shard is at most the mean plus one item.
    files = {f"t{i}.py": float((i * 37) % 23 + 1) for i in range(200)}
    placement = assign(files, 5)
    loads = Counter[int]()
    for name, shard in placement.items():
        loads[shard] += int(files[name])
    mean = sum(files.values()) / 5
    assert max(loads.values()) <= mean + max(files.values())


def test_assign_is_deterministic_and_independent_of_input_order() -> None:
    files = {f"t{i}.py": 1.0 for i in range(30)}  # all ties: order must not matter
    reordered = dict(reversed(list(files.items())))
    assert assign(files, 4) == assign(reordered, 4)


def test_assign_rejects_zero_shards() -> None:
    with pytest.raises(ShardSpecError):
        assign({"a.py": 1.0}, 0)


def test_select_keeps_a_file_whole_and_covers_everything_once() -> None:
    ids = [f"tests/t{f}.py::test_{n}" for f in range(12) for n in range(f + 1)]
    shards = [select(ids, ShardSpec(i, 4), {}) for i in range(1, 5)]
    assert sorted(n for s in shards for n in s) == sorted(ids)  # union, no duplicates
    for s in shards:
        for f in {file_of(n) for n in s}:
            assert all(n in s for n in ids if file_of(n) == f), f"{f} split across shards"


class _Mark:
    def __init__(self, *args: str, **kwargs: str) -> None:
        self.args, self.kwargs = args, kwargs


class _Item:
    def __init__(self, nodeid: str, mark: _Mark | None) -> None:
        self.nodeid, self._mark = nodeid, mark

    def get_closest_marker(self, name: str) -> _Mark | None:
        return self._mark if name == "xdist_group" else None


@pytest.mark.parametrize(
    ("nodeid", "mark", "expected"),
    [
        ("tests/a.py::t", None, "tests/a.py::t"),
        ("tests/a.py::t@grp", _Mark("grp"), "tests/a.py::t"),  # loadgroup, positional
        ("tests/a.py::t@grp", _Mark(name="grp"), "tests/a.py::t"),  # loadgroup, keyword
        ("tests/a.py::t", _Mark("grp"), "tests/a.py::t"),  # not rewritten (no xdist)
        ("tests/a.py::t[x@grp]", _Mark("other"), "tests/a.py::t[x@grp]"),  # '@' in a param
    ],
)
def test_canonical_nodeid_drops_only_the_xdist_group_suffix(
    nodeid: str, mark: _Mark | None, expected: str
) -> None:
    assert canonical_nodeid(_Item(nodeid, mark)) == expected  # type: ignore[arg-type]


def test_file_of_strips_the_test_path() -> None:
    assert file_of("tests/a/test_b.py::TestC::test_d[x::y]") == "tests/a/test_b.py"


# ---------------------------------------------------------------------------
# the committed durations file
# ---------------------------------------------------------------------------


def test_absent_durations_file_means_no_data(tmp_path: Path) -> None:
    assert load_durations(tmp_path / "missing.json") == {}


@pytest.mark.parametrize("body", ["not json", "{}", '{"files": [1, 2]}', '{"files": {"a": "x"}}'])
def test_malformed_durations_file_fails_loudly(tmp_path: Path, body: str) -> None:
    path = tmp_path / "d.json"
    path.write_text(body)
    with pytest.raises(ShardSpecError):
        load_durations(path)


def test_committed_durations_file_loads_and_names_real_test_files() -> None:
    durations = load_durations()
    assert durations, "tests/.test-durations.json is missing or empty"
    stale = [f for f in durations if not (_REPO / f).is_file()]
    # A few stale entries are harmless (they are ignored); a mostly-stale file
    # means the refresh is broken and balancing has quietly degraded.
    assert len(stale) <= len(durations) // 10, f"{len(stale)} stale entries, e.g. {stale[:5]}"


# ---------------------------------------------------------------------------
# end to end through the real conftest
# ---------------------------------------------------------------------------


def _small_files() -> list[str]:
    return [
        "tests/test_hermetic_colour_env.py",
        "tests/docs/test_makefile_matches_ci_gate.py",
        "tests/packaging_metadata/test_ci_unit_job_install.py",
    ]


def _pytest(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:randomly", "-p", "no:cacheprovider", *args],
        cwd=_REPO,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def test_shards_partition_a_real_collection(tmp_path: Path) -> None:
    files = _small_files()
    full = tmp_path / "all.txt"
    assert _pytest("--collect-only", "-q", "--shard-manifest", str(full), *files).returncode == 0
    expected = full.read_text().splitlines()
    assert len(expected) > 10

    got: list[str] = []
    for i in (1, 2, 3):
        out = tmp_path / f"s{i}.txt"
        res = _pytest(
            "--collect-only", "-q", f"--shard={i}/3", "--shard-manifest", str(out), *files
        )
        assert res.returncode == 0, res.stdout[-2000:] + res.stderr[-2000:]
        got += out.read_text().splitlines()
    assert sorted(got) == sorted(expected)


def test_a_shard_runs_under_xdist_and_writes_one_manifest(tmp_path: Path) -> None:
    out = tmp_path / "m.txt"
    res = _pytest("-q", "-n", "2", "--shard=1/2", "--shard-manifest", str(out), *_small_files())
    assert res.returncode == 0, res.stdout[-3000:] + res.stderr[-2000:]
    ids = out.read_text().splitlines()
    assert ids and len(ids) == len(set(ids))
    assert f"{len(ids)} passed" in res.stdout


def test_manifest_ids_under_loadgroup_match_a_plain_collection(tmp_path: Path) -> None:
    """Regression: the first sharded CI run failed its own partition check.

    ``tests/jobs/test_runner.py`` carries ``xdist_group("jobs-runner")``, so under
    ``--dist=loadgroup`` xdist renames its tests ``…@jobs-runner`` in the workers.
    """
    target = "tests/jobs/test_runner.py"
    plain, grouped = tmp_path / "plain.txt", tmp_path / "grouped.txt"
    assert _pytest("--collect-only", "-q", "--shard-manifest", str(plain), target).returncode == 0
    res = _pytest(
        "-q", "-n", "2", "--dist=loadgroup", "--shard=1/1", "--shard-manifest", str(grouped), target
    )
    assert res.returncode == 0, res.stdout[-3000:] + res.stderr[-2000:]
    expected = plain.read_text().splitlines()
    assert expected, "the reference collection is empty"
    assert sorted(grouped.read_text().splitlines()) == sorted(expected)


def test_an_invalid_shard_is_a_usage_error_not_a_crash() -> None:
    res = _pytest("--collect-only", "-q", "--shard=4/3", "tests/test_hermetic_colour_env.py")
    assert res.returncode == 4, res.stdout + res.stderr  # pytest's USAGE_ERROR
    assert "INTERNALERROR" not in res.stdout + res.stderr
    assert "need 1 <= I <= N" in res.stderr


def test_durations_json_shape_is_what_the_refresh_script_writes(tmp_path: Path) -> None:
    path = tmp_path / "d.json"
    path.write_text(json.dumps({"version": 1, "files": {"tests/a.py": 1.5}}))
    assert load_durations(path) == {"tests/a.py": 1.5}
