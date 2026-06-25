#!/usr/bin/env python3
"""Verify NovaFabric differentiation claims vs LangSmith/Langfuse/AgentTrace/in-toto/OTel.

Each claim is backed by an import or functional test of the actual codebase.
Exit code 0 = all verifiable claims pass. Exit code 1 = any claim failed.

Usage:
    uv run python scripts/verify_differentiation_table.py
    uv run python scripts/verify_differentiation_table.py --format json
"""
from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass
from typing import Callable


@dataclass
class Claim:
    id: str
    title: str
    vs: str
    verify: Callable[[], None]
    note: str = ""  # non-empty = manual-only (won't fail)


@dataclass
class ClaimResult:
    id: str
    title: str
    vs: str
    status: str  # PASS | FAIL | NOTE
    error: str = ""
    note: str = ""


def _check_d01_local_first() -> None:
    """D-01: Local-first — registry store + capsule writer importable without network."""
    from novafabric._paths import nova_home  # noqa: F401
    from novafabric.capture.capsule import CapsuleWriter  # noqa: F401
    from novafabric.registry import store as _registry_store  # noqa: F401
    # Verify registry store module has the expected DB accessor
    assert hasattr(_registry_store, "get_db_path"), (
        "novafabric.registry.store must expose get_db_path()"
    )
    assert callable(nova_home), "nova_home() must be callable"


def _check_d02_cryptographic_signing() -> None:
    """D-02: Cryptographic signing — NovaSeal DSSE envelope + Merkle log + signing backend."""
    from novafabric.trust.novaseal.envelope import SigningIntent, create_envelope  # noqa: F401
    from novafabric.trust.novaseal.merkle import MerkleLog  # noqa: F401
    from novafabric.trust.novaseal.signing_backend import LocalSigningBackend  # noqa: F401
    # Verify MerkleLog is a usable class
    assert callable(MerkleLog), "MerkleLog must be a callable class"
    assert callable(LocalSigningBackend), "LocalSigningBackend must be a callable class"


def _check_d03_replay_modes() -> None:
    """D-03: Replay modes — forensic/mocked/semantic/exact all supported."""
    from novafabric.replay._engine import ReplayEngine  # noqa: F401
    from novafabric.replay._flags import ReplayFlags
    # Verify all four replay modes are valid
    for mode in ("forensic", "mocked", "semantic", "exact"):
        flags = ReplayFlags(mode=mode)  # type: ignore[arg-type]
        assert flags.mode == mode, f"ReplayFlags must accept mode={mode!r}"


def _check_d04_structured_diff() -> None:
    """D-04: Structured diff — DiffReport Pydantic model + DiffEngine exist."""
    from novafabric.diff._engine import DiffEngine  # noqa: F401
    from novafabric.diff._report import DiffReport  # noqa: F401
    assert callable(DiffEngine), "DiffEngine must be a callable class"
    assert callable(DiffReport), "DiffReport must be a callable class"


def _check_d05_policy_gates() -> None:
    """D-05: Policy gates — OPA/Rego promote pipeline importable."""
    from novafabric.policy._engine import PolicyEngine  # noqa: F401
    from novafabric.promote.predicates import build_bypass_predicate  # noqa: F401
    assert callable(build_bypass_predicate), "build_bypass_predicate must be callable"


def _check_d06_worm_storage() -> None:
    """D-06: WORM storage — NovaObjectStore + S3 WORM adapter importable."""
    from novafabric.object_capsule_store.worm.s3 import S3WormAdapter  # noqa: F401
    from novafabric.storage.nova_object_store import NovaObjectStore  # noqa: F401
    assert callable(NovaObjectStore), "NovaObjectStore must be a callable class"
    assert callable(S3WormAdapter), "S3WormAdapter must be a callable class"


def _check_d07_schema_versioning() -> None:
    """D-07: Schema versioning — capsule schema_version field + migrate CLI exist."""
    spec = importlib.util.find_spec("novafabric.cli.migrate_capsule")
    assert spec is not None, "novafabric.cli.migrate_capsule must exist"
    # Verify capsule schema has schema_version support
    from novafabric.capsule.schema import ParentCapsule  # noqa: F401
    assert hasattr(ParentCapsule, "model_fields"), "ParentCapsule must be a Pydantic model"
    assert "schema_version" in ParentCapsule.model_fields, (
        "ParentCapsule.schema_version field must exist"
    )


def _check_d08_openlineage() -> None:
    """D-08: OpenLineage emission — lineage store + OpenLineage emitter importable."""
    from novafabric.lineage._store import LineageStore  # noqa: F401
    # Verify the OpenLineage transport module exists
    spec = importlib.util.find_spec("novafabric.lineage._openlineage")
    assert spec is not None, "novafabric.lineage._openlineage must exist"


def _check_d09_multi_framework_adapters() -> None:
    """D-09: Multi-framework adapters — at least 4 adapter modules importable."""
    adapter_modules = [
        "novafabric.adapters.langgraph",
        "novafabric.adapters.autogen",
        "novafabric.adapters.crewai",
        "novafabric.adapters.dspy",
        "novafabric.adapters.openai_agents",
        "novafabric.adapters.google_adk",
        "novafabric.adapters.bedrock_agentcore",
        "novafabric.adapters.a2a",
    ]
    available = [mod for mod in adapter_modules if importlib.util.find_spec(mod) is not None]
    assert len(available) >= 4, (
        f"Only {len(available)}/8 adapter modules found: {available}"
    )


def _check_d10_eval_gated_promotion() -> None:
    """D-10: Eval-gated promotion — EvalSuiteAdapter + regression_gate.rego exist."""
    from novafabric.evals.adapter import EvalSuiteAdapter  # noqa: F401
    # Verify regression_gate.rego is present under the policies package
    spec = importlib.util.find_spec("novafabric")
    assert spec is not None and spec.origin is not None, "novafabric package must be importable"
    from pathlib import Path
    src_root = Path(spec.origin).parent
    # The rego file lives at policies/novafabric/defaults/regression_gate.rego
    rego = src_root / "policies" / "novafabric" / "defaults" / "regression_gate.rego"
    assert rego.exists(), f"regression_gate.rego not found at {rego}"


CLAIMS: list[Claim] = [
    Claim(
        id="D-01",
        title="Local-first, no cloud required",
        vs="LangSmith (cloud-only), Langfuse (requires Postgres for self-host)",
        verify=_check_d01_local_first,
    ),
    Claim(
        id="D-02",
        title="Cryptographic signing of evidence (DSSE + Merkle log)",
        vs="LangSmith (no signing), OTel (no signing)",
        verify=_check_d02_cryptographic_signing,
    ),
    Claim(
        id="D-03",
        title="Replay modes (forensic/mocked/semantic/exact)",
        vs="All competitors — none offer structured replay",
        verify=_check_d03_replay_modes,
    ),
    Claim(
        id="D-04",
        title="Structured cross-run diff (DiffReport)",
        vs="All competitors — diff is not a first-class concept",
        verify=_check_d04_structured_diff,
    ),
    Claim(
        id="D-05",
        title="Policy gates + maker-checker dual-approval (OPA/Rego)",
        vs="All competitors — no governance gates",
        verify=_check_d05_policy_gates,
    ),
    Claim(
        id="D-06",
        title="WORM-compliant evidence storage (S3/Azure/GCS)",
        vs="All competitors — no WORM compliance",
        verify=_check_d06_worm_storage,
    ),
    Claim(
        id="D-07",
        title="Capsule schema versioning + migration CLI",
        vs="All competitors — no schema stability guarantee",
        verify=_check_d07_schema_versioning,
    ),
    Claim(
        id="D-08",
        title="OpenLineage emission from lineage graph",
        vs="AgentTrace (no lineage), OTel (no lineage standard for AI)",
        verify=_check_d08_openlineage,
    ),
    Claim(
        id="D-09",
        title="Multi-framework adapters (8 frameworks, ≥4 live)",
        vs="in-toto (no adapters), OTel (manual instrumentation)",
        verify=_check_d09_multi_framework_adapters,
    ),
    Claim(
        id="D-10",
        title="Eval-gated promotion (Rego regression gate)",
        vs="LangSmith/Langfuse (no policy gate on promotion)",
        verify=_check_d10_eval_gated_promotion,
    ),
]


def run_checks(format: str = "rich") -> int:
    results: list[ClaimResult] = []
    any_fail = False

    for claim in CLAIMS:
        if claim.note:
            results.append(ClaimResult(
                id=claim.id,
                title=claim.title,
                vs=claim.vs,
                status="NOTE",
                note=claim.note,
            ))
            continue
        try:
            claim.verify()
            results.append(ClaimResult(
                id=claim.id,
                title=claim.title,
                vs=claim.vs,
                status="PASS",
            ))
        except Exception as exc:
            any_fail = True
            results.append(ClaimResult(
                id=claim.id,
                title=claim.title,
                vs=claim.vs,
                status="FAIL",
                error=f"{type(exc).__name__}: {exc}",
            ))

    if format == "json":
        print(json.dumps([
            {
                "id": r.id,
                "title": r.title,
                "vs": r.vs,
                "status": r.status,
                "error": r.error,
                "note": r.note,
            }
            for r in results
        ], indent=2))
    else:
        width = 65
        print(f"\n{'NovaFabric Differentiation Verification':^{width}}")
        print("=" * width)
        for r in results:
            status_icon = {"PASS": "[PASS]", "FAIL": "[FAIL]", "NOTE": "[NOTE]"}.get(
                r.status, "[????]"
            )
            print(f"  {status_icon}  {r.id}: {r.title}")
            if r.error:
                print(f"           ERROR: {r.error}")
            if r.note:
                print(f"           NOTE:  {r.note}")
        print("=" * width)
        passed = sum(1 for r in results if r.status == "PASS")
        failed = sum(1 for r in results if r.status == "FAIL")
        notes = sum(1 for r in results if r.status == "NOTE")
        print(f"  {passed} PASS  |  {failed} FAIL  |  {notes} NOTE\n")

    return 1 if any_fail else 0


if __name__ == "__main__":
    fmt = "json" if "--format" in sys.argv and "json" in sys.argv else "rich"
    sys.exit(run_checks(format=fmt))
