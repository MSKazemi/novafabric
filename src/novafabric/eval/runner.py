from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from novafabric.registry.service import get_asset
from novafabric.registry.store import get_connection, init_schema


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_evals(
    agent_name: str,
    agent_version: str,
    db_path: Path | None = None,
    dataset_version: str | None = None,
) -> dict[str, Any]:
    """Run an asset's eval suites and persist versioned provenance (ADR-0085).

    ``dataset_version`` (optional) pins the eval dataset version into every result
    so a stored result can be tied back to exactly what was measured. The asset's
    own version is always pinned. Both are additive and backward-compatible.
    """
    asset = get_asset(agent_name, agent_version, db_path=db_path)
    # Pin the asset version actually resolved (not the caller's ref, which may be
    # "latest" or a normalized alias).
    pinned_asset_version = str(asset["version"])
    spec_data = json.loads(asset["spec_json"])
    eval_suite_names: list[str] = spec_data.get("spec", {}).get("evals", [])

    available = {ep.name: ep for ep in entry_points(group="novafabric.evals")}

    suite_results: list[dict[str, Any]] = []
    for suite_name in eval_suite_names:
        if suite_name not in available:
            suite_results.append(
                {
                    "suite_name": suite_name,
                    "passed": False,
                    "reason": (
                        f"eval suite '{suite_name}' is not registered as a "
                        "novafabric.evals entry point"
                    ),
                }
            )
        else:
            try:
                fn = available[suite_name].load()
                raw = fn(spec_data)
                passed = (
                    bool(raw.get("passed", False))
                    if isinstance(raw, dict)
                    else bool(raw)
                )
                suite_results.append(
                    {
                        "suite_name": suite_name,
                        "passed": passed,
                        "score": raw.get("score") if isinstance(raw, dict) else None,
                    }
                )
            except Exception as exc:
                suite_results.append(
                    {
                        "suite_name": suite_name,
                        "passed": False,
                        "reason": f"suite raised exception: {exc}",
                    }
                )

    overall_passed = all(s["passed"] for s in suite_results)

    # Version pinning (ADR-0085): attach the resolved asset version to every
    # result, and the dataset version when one was supplied. Omitted cleanly
    # from the return shape when not provided so existing callers are unaffected.
    for s in suite_results:
        s["asset_version"] = pinned_asset_version
        if dataset_version is not None:
            s["dataset_version"] = dataset_version

    conn = get_connection(db_path)
    init_schema(conn)
    try:
        for s in suite_results:
            conn.execute(
                """
                INSERT INTO eval_results
                    (id, asset_id, suite_name, passed, score_json, run_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    asset["id"],
                    s["suite_name"],
                    int(s["passed"]),
                    # null score: suite ran but returned no numeric score (binary
                    # pass/fail). asset_version/dataset_version pin provenance
                    # (ADR-0085); dataset_version is null when not supplied.
                    json.dumps(
                        {
                            "score": s.get("score"),
                            "asset_version": pinned_asset_version,
                            "dataset_version": dataset_version,
                        }
                    ),
                    _now(),
                ),
            )
        conn.commit()
    finally:
        conn.close()

    return {"passed": overall_passed, "suites": suite_results}
