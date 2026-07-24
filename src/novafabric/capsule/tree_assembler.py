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

"""Lazy parent/child tree assembler — read path (cap-003, FR-12, FR-13).

FR-12: Accept any valid child capsule regardless of parent presence.
FR-13: Assemble tree lazily at query time via indexed join on parent_run_id.

The assembler operates on a spool directory of capsule.json files and does
NOT require a database — it uses os.scandir() + indexed dict joins at read time.
This is suitable for local-mode operation (SQLite-backed index for server mode
would extend this interface via a subclass).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ORPHAN_LABEL = "unknown"


class CyclicLineageError(Exception):
    """Raised when a circular parent reference is detected during tree assembly."""


class TreeDepthExceededError(Exception):
    """Raised when a capsule tree is deeper than ``MAX_TREE_DEPTH``.

    Bounds the recursive assembler so a pathologically deep (but acyclic) tree
    fails with a clear, named error instead of a bare ``RecursionError``.
    """


# Maximum parent→child nesting depth. Kept well below Python's default recursion
# limit (~1000) so assembly fails cleanly before the interpreter stack does.
MAX_TREE_DEPTH = 500


@dataclass
class CapsuleNode:
    """A node in the assembled capsule tree."""

    run_id: str
    global_run_id: str
    parent_run_id: str | None
    capsule_role: str
    status: str
    capsule_dir: Path
    children: list["CapsuleNode"] = field(default_factory=list)
    is_synthetic: bool = False


@dataclass
class AssembledTree:
    """Result of a lazy tree assembly query."""

    root: CapsuleNode
    total_nodes: int
    orphan_children: list[CapsuleNode]  # children with unknown parent


class CapsuleTreeAssembler:
    """Lazy tree assembler over a local capsule spool directory (FR-13).

    Scans <spool_dir>/<run_id>/capsule.json files and builds an indexed join
    on parent_run_id at query time — no persistent index required.
    """

    def __init__(self, spool_dir: Path) -> None:
        self._spool = spool_dir

    def _scan_capsules(self) -> dict[str, dict[str, Any]]:
        """Return {run_id: manifest} for all capsule.json files in spool."""
        manifests: dict[str, dict[str, Any]] = {}
        if not self._spool.is_dir():
            return manifests

        for entry in self._spool.iterdir():
            if not entry.is_dir():
                continue
            manifest_path = entry / "capsule.json"
            if not manifest_path.exists():
                continue
            try:
                data: dict[str, Any] = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
                run_id = data.get("run_id")
                if run_id:
                    manifests[run_id] = data
            except Exception as exc:
                logger.debug("tree_assembler: skipping %s: %s", manifest_path, exc)
        return manifests

    def get_children(self, parent_run_id: str) -> list[dict[str, Any]]:
        """Return all capsule manifests whose parent_run_id matches (FR-13)."""
        all_manifests = self._scan_capsules()
        return [
            m for m in all_manifests.values()
            if m.get("parent_run_id") == parent_run_id
        ]

    def assemble_tree(
        self,
        root_run_id: str,
        *,
        include_orphans: bool = True,
    ) -> AssembledTree:
        """Assemble a capsule tree starting from root_run_id (FR-13).

        Orphan children (whose parent_run_id is set but parent not in spool)
        are included with parent label 'unknown' when include_orphans=True.
        """
        all_manifests = self._scan_capsules()

        # Build parent_run_id index
        children_by_parent: dict[str, list[dict[str, Any]]] = {}
        for m in all_manifests.values():
            pid = m.get("parent_run_id")
            if pid:
                children_by_parent.setdefault(pid, []).append(m)

        # Find root
        root_manifest = all_manifests.get(root_run_id)
        if root_manifest is None:
            # Create a synthetic placeholder root for orphan parents
            root_manifest = {
                "run_id": root_run_id,
                "global_run_id": root_run_id,
                "parent_run_id": None,
                "capsule_role": "PARENT",
                "status": "unknown",
                "is_synthetic": True,
            }
        # Path-tracking set: detects circular parent references (A→B→A).
        _visiting: set[str] = set()

        def _build_node(m: dict[str, Any]) -> CapsuleNode:
            rid = m["run_id"]
            if rid in _visiting:
                raise CyclicLineageError(
                    f"Cyclic lineage detected: run_id '{rid}' forms a cycle"
                )
            # len(_visiting) is the current path depth (ancestors on the stack).
            if len(_visiting) >= MAX_TREE_DEPTH:
                raise TreeDepthExceededError(
                    f"Capsule tree exceeds MAX_TREE_DEPTH={MAX_TREE_DEPTH} at "
                    f"run_id '{rid}'"
                )
            _visiting.add(rid)
            node = CapsuleNode(
                run_id=rid,
                global_run_id=m.get("global_run_id", rid),
                parent_run_id=m.get("parent_run_id"),
                capsule_role=m.get("capsule_role", "STANDALONE"),
                status=m.get("status", "unknown"),
                capsule_dir=self._spool / rid,
                is_synthetic=bool(m.get("is_synthetic", False)),
            )
            for child_m in children_by_parent.get(rid, []):
                node.children.append(_build_node(child_m))
            _visiting.discard(rid)
            return node

        root_node = _build_node(root_manifest)

        # Collect orphan children (have parent_run_id = root_run_id but root wasn't in spool)
        orphan_children: list[CapsuleNode] = []
        if include_orphans and root_manifest.get("is_synthetic"):
            for child_m in children_by_parent.get(root_run_id, []):
                orphan_children.append(
                    CapsuleNode(
                        run_id=child_m["run_id"],
                        global_run_id=child_m.get("global_run_id", child_m["run_id"]),
                        parent_run_id=_ORPHAN_LABEL,
                        capsule_role=child_m.get("capsule_role", "CHILD"),
                        status=child_m.get("status", "unknown"),
                        capsule_dir=self._spool / child_m["run_id"],
                    )
                )

        # Count total nodes
        def _count(n: CapsuleNode) -> int:
            return 1 + sum(_count(c) for c in n.children)

        total = _count(root_node)

        return AssembledTree(
            root=root_node,
            total_nodes=total,
            orphan_children=orphan_children,
        )

    def list_orphan_children(self, parent_run_id: str) -> list[dict[str, Any]]:
        """Return children with parent_run_id set but no matching parent capsule.

        Orphan children have their parent_run_id preserved (FR-12).
        """
        all_manifests = self._scan_capsules()
        children = [
            m for m in all_manifests.values()
            if m.get("parent_run_id") == parent_run_id
        ]
        # Parent is orphan if not in spool
        if parent_run_id not in all_manifests:
            for c in children:
                c["_orphan_parent"] = True
        return children
