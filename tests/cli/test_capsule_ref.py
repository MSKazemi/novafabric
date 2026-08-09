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

"""A capsule reference may be a path or the run id ``nova capture`` printed.

The behaviour that matters most here is the *precedence*: an existing path is used
as given and never triggers an id lookup. That is what makes this change additive —
every invocation that worked before resolves to exactly the same directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from novafabric.cli._capsule_ref import CapsuleRefError, resolve_capsule_ref

RUN_ID = "01KZMDB3V38GV86AYTBR77JJQD"


def _make_capsule(parent: Path, name: str) -> Path:
    d = parent / name
    d.mkdir(parents=True)
    (d / "capsule.yaml").write_text("run_id: x\n", encoding="utf-8")
    return d


def test_bare_run_id_resolves_against_the_capsule_dir(tmp_path: Path) -> None:
    store = tmp_path / "capsules"
    expected = _make_capsule(store, RUN_ID)

    assert resolve_capsule_ref(RUN_ID, capsule_dir=store) == expected


def test_absolute_path_still_resolves(tmp_path: Path) -> None:
    capsule = _make_capsule(tmp_path / "elsewhere", "run-a")

    assert resolve_capsule_ref(capsule, capsule_dir=tmp_path / "capsules") == capsule


def test_relative_path_still_resolves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_capsule(tmp_path / "nested", "run-a")
    monkeypatch.chdir(tmp_path)

    got = resolve_capsule_ref("nested/run-a", capsule_dir=tmp_path / "capsules")

    assert got == Path("nested/run-a")


def test_an_existing_path_wins_over_a_same_named_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression that would silently redirect an existing invocation.

    With a directory named for a run id in the working directory *and* a real run of
    that id in the store, the path must win — otherwise upgrading this resolver
    changes where an existing command reads from.
    """
    work = tmp_path / "work"
    local = _make_capsule(work, RUN_ID)
    store = tmp_path / "capsules"
    _make_capsule(store, RUN_ID)
    monkeypatch.chdir(work)

    assert resolve_capsule_ref(RUN_ID, capsule_dir=store) == Path(RUN_ID)
    assert (Path(RUN_ID)).resolve() == local.resolve()


def test_a_directory_without_a_manifest_is_not_a_capsule(tmp_path: Path) -> None:
    (tmp_path / "not-a-capsule").mkdir()

    with pytest.raises(CapsuleRefError):
        resolve_capsule_ref(tmp_path / "not-a-capsule", capsule_dir=tmp_path)


def test_unknown_run_id_names_the_directory_searched(tmp_path: Path) -> None:
    store = tmp_path / "capsules"
    store.mkdir()

    with pytest.raises(CapsuleRefError) as exc:
        resolve_capsule_ref(RUN_ID, capsule_dir=store)

    msg = str(exc.value)
    assert RUN_ID in msg
    assert str(store) in msg, "the message must say where it looked"
    assert "NOVAFABRIC_CAPSULE_DIR" in msg, "and how to point it elsewhere"


def test_a_multi_segment_ref_is_reported_as_a_path_not_an_id(tmp_path: Path) -> None:
    """``a/b`` is a path the user got wrong; calling it an unknown run id misleads."""
    with pytest.raises(CapsuleRefError) as exc:
        resolve_capsule_ref("nope/missing", capsule_dir=tmp_path)

    msg = str(exc.value)
    assert "No capsule at path" in msg
    assert "run id" not in msg


def test_capsule_dir_defaults_to_the_configured_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting capsule_dir honours NOVAFABRIC_CAPSULE_DIR, so callers need not."""
    store = tmp_path / "configured"
    expected = _make_capsule(store, RUN_ID)
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(store))

    assert resolve_capsule_ref(RUN_ID) == expected
