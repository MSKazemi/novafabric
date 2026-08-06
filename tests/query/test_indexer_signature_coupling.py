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

"""The cache signature must cover every capsule file the indexer reads (ADR-0225).

ADR-0225's stale-answer defect was a *class*, not an instance: a file the indexer
reads whose mutation the signature cannot see. Two instances of that class are
fixed (``capsule.json`` rewritten in place, ``scores.jsonl`` appended to). The
rule that keeps the *next* instance from reappearing is what these tests add.

The failure being prevented is silent. If someone teaches
:func:`novafabric.query.indexer.scan_capsule` to read a new file — say
``tool-calls.jsonl`` — and does not extend the signature, then appending to that
file moves neither the directory mtime nor any signed-for file, the cache serves
its stale rows forever, and every test that checks *today's* four files still
passes. So the coupling is asserted structurally rather than restated by hand:
the read-set is derived from the indexer module itself, never from the constant
it is being checked against.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from novafabric.query import cache, indexer

#: A capsule-relative filename: no spaces, no format placeholders. Deliberately
#: strict so an error message that merely *mentions* a filename is not mistaken
#: for a read (``f"{path}: invalid scores.jsonl: {exc}"`` must not match).
_FILENAME = re.compile(r"[A-Za-z0-9._-]+\.(?:jsonl|yaml|json)")


def _filenames_in_module_source() -> set[str]:
    """Every capsule filename appearing as a literal in ``indexer.py``."""
    source = Path(indexer.__file__).read_text(encoding="utf-8")
    return {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _FILENAME.fullmatch(node.value)
    }


def _filenames_in_module_namespace() -> set[str]:
    """Every capsule filename reachable as a constant in the indexer's namespace.

    Covers the names the indexer imports rather than spells — ``SCORES_FILENAME``
    is ``scores.jsonl`` and appears nowhere as a literal.
    """
    found: set[str] = set()
    for value in vars(indexer).values():
        if isinstance(value, str) and _FILENAME.fullmatch(value):
            found.add(value)
        elif isinstance(value, tuple):
            found.update(
                item
                for item in value
                if isinstance(item, str) and _FILENAME.fullmatch(item)
            )
    return found


def indexer_read_set() -> set[str]:
    """The files the indexer reads, derived independently of the cache."""
    return _filenames_in_module_source() | _filenames_in_module_namespace()


def test_every_file_the_indexer_reads_is_in_the_cache_signature() -> None:
    """The rule ADR-0225 fixed two instances of, stated once.

    Fails the moment the indexer learns to read a file the signature ignores —
    which is the only way the stale-answer defect can come back.
    """
    uncovered = indexer_read_set() - set(cache._INDEXED_FILES)

    assert not uncovered, (
        f"indexer reads {sorted(uncovered)} but the cache signature does not "
        f"cover them, so an append to those files would be invisible and "
        f"`nova query` would serve a stale answer. Add them to the indexer's "
        f"declared read-set."
    )


def test_cache_signature_derives_from_the_indexer_declaration() -> None:
    """One source of truth, not two lists that happen to agree today."""
    assert cache._INDEXED_FILES == indexer.INDEXED_FILENAMES


def test_indexer_declaration_matches_what_the_module_actually_reads() -> None:
    """The declaration cannot drift from the module either.

    Without this, the declaration could be satisfied by a constant nobody uses.
    """
    assert set(indexer.INDEXED_FILENAMES) == indexer_read_set()
