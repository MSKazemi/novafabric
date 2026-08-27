from __future__ import annotations

import json
import logging
import os
import platform
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

import yaml

from novafabric.capture._ulid import new_span_id, new_ulid
from novafabric.capture.capsule import CapsuleWriter
from novafabric.capture.deployment_env import (
    resolve_deployment_environment,
    unconventional_warning,
)
from novafabric.capture.env import capture_environment
from novafabric.capture.replay import minimal_replay_policy
from novafabric.capture.secrets import SecretScannerV0, recompute_chain_hash
from novafabric.capture.session import resolve_session_membership
from novafabric.capture.variant import resolve_variant_attribution
from novafabric.cost.usage_types import usage_totals_from_model_calls
from novafabric.runners import LocalRunner, RunnerJobSpec, RunnerSpec

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _sha256(data: bytes) -> str:
    import hashlib
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _event_stream_refs(capsule_dir: Path) -> dict[str, str]:
    """Return manifest refs for event-stream files that exist and are non-empty.

    The EventRecorder (v0.16.0) writes ``network_events.jsonl``,
    ``file_events.jsonl`` and ``human_approvals.jsonl`` only when the
    corresponding events occur. We add a manifest ref only when the file has
    content so downstream consumers (eval harnesses, the dashboard, compliance
    exporters) can rely on a referenced stream actually existing.
    """
    refs: dict[str, str] = {}
    _streams = {
        "network_events_ref": "network_events.jsonl",
        "file_events_ref": "file_events.jsonl",
        "human_approvals_ref": "human_approvals.jsonl",
    }
    for ref_key, filename in _streams.items():
        path = capsule_dir / filename
        try:
            if path.exists() and path.stat().st_size > 0:
                refs[ref_key] = filename
        except OSError:
            pass
    return refs


def _build_host_info() -> dict[str, Any]:
    machine = platform.machine().lower()
    arch_map = {
        "x86_64": "x86_64", "amd64": "x86_64",
        "arm64": "arm64", "aarch64": "arm64",
        "riscv64": "riscv64", "s390x": "s390x", "ppc64le": "ppc64le",
    }
    arch = arch_map.get(machine, "x86_64")
    os_name = platform.system().lower()
    if os_name not in ("linux", "darwin", "windows"):
        os_name = "linux"
    try:
        cpu_count = os.cpu_count() or 1
    except Exception:
        cpu_count = 1
    memory_bytes = 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    memory_bytes = int(line.split()[1]) * 1024
                    break
    except Exception:
        pass
    return {
        "os": os_name,
        "arch": arch,
        "python": platform.python_version(),
        "cpu_count": cpu_count,
        "memory_bytes": memory_bytes,
        "gpu": [],
        "hostname_redacted": True,
    }


@dataclass
class CaptureResult:
    run_id: str
    capsule_dir: Path
    exit_code: int


class AssetStatusCheckError(Exception):
    """Raised when an asset fails the --require-asset-status pre-flight check."""


def check_asset_status(
    asset_ref: str,
    require_statuses: list[str] | None,
    warn_statuses: list[str] | None,
    require_registered: bool,
    db_path: Path | None = None,
) -> None:
    """Pre-flight asset status check for nova capture.

    Args:
        asset_ref: Asset reference in ``name@version`` format (or bare ``name``).
        require_statuses: If given, the asset's status must be in this set.
            Raises :class:`AssetStatusCheckError` if not.
        warn_statuses: If given, emit a structured log warning if the asset's
            status is in this set (does not block).
        require_registered: If ``True``, raise :class:`AssetStatusCheckError`
            when the asset is not in the registry.  Default behaviour is to
            warn only.
        db_path: Override the registry DB path.

    Raises:
        AssetStatusCheckError: Hard block — asset status does not match the
            required set, or asset is not registered and *require_registered*
            is ``True``.
    """
    import logging as _logging

    _log = _logging.getLogger(__name__)

    # Lazily import registry helpers to avoid a mandatory dependency at module
    # import time (keeps the orchestrator usable without a registry DB).
    try:
        from novafabric.registry.service import AssetNotFoundError, get_asset
    except ImportError as exc:
        _log.warning(
            "novafabric.registry not importable — skipping asset status check: %s",
            exc,
        )
        return

    # Parse name / version from the ref.
    if "@" in asset_ref:
        name, version = asset_ref.rsplit("@", 1)
    else:
        name, version = asset_ref, None

    try:
        asset = get_asset(name, version, db_path=db_path)
    except AssetNotFoundError:
        msg = (
            f"[novafabric] Asset '{asset_ref}' is not registered in the registry."
        )
        if require_registered:
            raise AssetStatusCheckError(
                f"Asset '{asset_ref}' is not registered. "
                "Remove --require-registered to allow unregistered assets."
            )
        _log.warning(msg)
        return

    current_status: str = asset["status"]

    # Warn-if check (non-blocking).
    if warn_statuses and current_status in warn_statuses:
        _log.warning(
            "Asset '%s' has status '%s' which matches --warn-if-asset-status set %r",
            asset_ref,
            current_status,
            warn_statuses,
        )

    # Require check (hard block).
    if require_statuses is not None and current_status not in require_statuses:
        raise AssetStatusCheckError(
            f"Asset '{asset_ref}' has status '{current_status}', "
            f"which is not in the required set {require_statuses!r}. "
            "Promote the asset first or update --require-asset-status."
        )


class CaptureOrchestrator:
    def __init__(
        self,
        base_dir: Path | None = None,
        runner: RunnerSpec | None = None,
        pii_gate: Any | None = None,
        legal_hold: bool = False,
        mark_provenance: bool = False,
        fast_emit: bool = False,
        emit_spool: bool = False,
        spool_dir: Path | None = None,
        masking_pipeline: Any | None = None,
        capture_media: bool = False,
    ) -> None:
        self._base_dir = base_dir or (Path.cwd() / ".novafabric" / "runs")
        self._base_dir.mkdir(parents=True, exist_ok=True)
        # ADR-0025: orchestrator depends only on the RunnerSpec protocol;
        # default to LocalRunner to preserve v0.5.x behavior.
        self._runner: RunnerSpec = runner if runner is not None else LocalRunner()
        # cap-001: PII detection gate (OQ-01 unresolved → legal-hold mode only)
        self._pii_gate = pii_gate
        self._legal_hold = legal_hold
        # ADR-0074: opt-in C2PA synthetic-content provenance marking at capture
        # time (EU AI Act Art.50). Writes c2pa-manifest.json into the capsule
        # before sealing so the disclosure is covered by the NovaSeal signature.
        self._mark_provenance = mark_provenance
        # ADR-0135 (experimental): opt-in pluggable PII masking pipeline. Runs
        # after the built-in ADR-0009 scanner (which is never disabled) and
        # before the capsule is finalized; every mask/failure is recorded in
        # redaction-proof.json (masker_findings[]/masker_errors[]). Fail-closed
        # on secrets, fail-safe for the workload: a masker error redacts the
        # field and never blocks capture. None ⇒ exact ADR-0009 behavior.
        self._masking_pipeline = masking_pipeline
        # ADR-0092 slice B: opt-in import-deferred hook install in the workload
        # subprocess (--fast-emit). Avoids importing unused SDKs at startup just
        # to patch them; honored by the sitecustomize loader via the
        # NOVAFABRIC_FAST_EMIT env var set in run().
        self._fast_emit = fast_emit
        # ADR-0092 slice C (C0): opt-in capture→spool emission. When on, the
        # orchestrator writes run-boundary EventEnvelope v1 records to the spool
        # so the resident drain can forward them to the collector tier. Off by
        # default — local capture is unaffected. Edge stays keyless (hub-sign,
        # OQ-C-1): no signing happens here.
        self._emit_spool = emit_spool
        self._spool_dir = spool_dir
        # ADR-0125 D2: opt-in byte capture for multimodal message parts
        # (--capture-media). Default off: MediaPart records reference metadata
        # only (content_hash + byte_size), per ADR-0021 §4 privacy-by-default.
        self._capture_media = capture_media

    def run(
        self,
        command: list[str],
        runner_options: dict[str, Any] | None = None,
        timeout_s: float | None = None,
        asset_ref: str | None = None,
        require_asset_statuses: list[str] | None = None,
        warn_if_asset_statuses: list[str] | None = None,
        require_registered: bool = False,
        registry_db_path: Path | None = None,
        environment: str | None = None,
        experiment: str | None = None,
        variant: str | None = None,
        variant_label: str | None = None,
        variant_source: str | None = None,
        variant_assigned_at: str | None = None,
        session_id: str | None = None,
        session_sequence: int | None = None,
    ) -> CaptureResult:
        # ------------------------------------------------------------------
        # C-1.5: Pre-flight asset status check — must happen BEFORE anything
        # is written to disk.  Never blocks by default; only blocks when the
        # caller has explicitly opted into enforcement.
        # ------------------------------------------------------------------
        if asset_ref and (
            require_asset_statuses is not None
            or warn_if_asset_statuses is not None
            or require_registered
        ):
            check_asset_status(
                asset_ref=asset_ref,
                require_statuses=require_asset_statuses,
                warn_statuses=warn_if_asset_statuses,
                require_registered=require_registered,
                db_path=registry_db_path,
            )

        # ADR-0126: resolve the deployment-environment tag pre-flight (CLI flag
        # > NOVAFABRIC_ENVIRONMENT env var; explicit sources only — never
        # inferred). An invalid explicit value raises here, BEFORE anything is
        # written to disk; if no source supplies a value the fields stay absent.
        deployment_env = resolve_deployment_environment(cli_value=environment)
        if deployment_env is not None:
            _warn = unconventional_warning(deployment_env.value)
            if _warn is not None:
                import logging as _logging
                _logging.getLogger(__name__).warning(_warn)

        # ADR-0116: resolve the variant attribution pre-flight (CLI flags >
        # NOVAFABRIC_VARIANT* env vars; record-only — every field is copied
        # verbatim, never derived or defaulted). An incomplete/invalid explicit
        # block raises here, BEFORE anything is written to disk; if no source
        # supplies a block the manifest stays unchanged.
        variant_attr = resolve_variant_attribution(
            cli_values={
                "experiment_id": experiment,
                "variant_id": variant,
                "variant_label": variant_label,
                "assignment_source": variant_source,
                "assigned_at": variant_assigned_at,
            }
        )

        # ADR-0122: resolve the session back-reference pre-flight (CLI flags >
        # NOVAFABRIC_SESSION_ID / NOVAFABRIC_SESSION_SEQUENCE env vars; explicit
        # sources only — never inferred). An invalid explicit value raises here,
        # BEFORE anything is written to disk; if no source supplies a value the
        # fields stay absent (a standalone run — today's behavior).
        session_membership = resolve_session_membership(
            cli_session_id=session_id, cli_sequence=session_sequence
        )

        run_id = new_ulid()
        root_span_id = new_span_id()
        created_at = _now()
        t0 = time.monotonic()

        writer = CapsuleWriter(run_id=run_id, base_dir=self._base_dir)
        writer.open()
        capsule_dir = writer.capsule_dir

        # --- Event recorder (file_events / network_events / human_approvals) ---
        # Set module-level singleton so wire-level hooks can call
        # get_current_recorder() without holding a reference to the orchestrator.
        from novafabric.capture.event_recorder import EventRecorder, set_current_recorder
        _event_recorder = EventRecorder(
            capsule_dir=capsule_dir,
            run_id=run_id,
            capsule_id=run_id,
        )
        set_current_recorder(_event_recorder)

        # --- ADR-0092 slice C (C0): capture→spool emission (opt-in) ---
        _spool_sink = None
        _spool_agent_id = asset_ref or Path(command[0]).name or "nova-capture"
        if self._emit_spool:
            try:
                from novafabric._paths import spool_dir as _default_spool_dir
                from novafabric.capture.spool_sink import SpoolSink

                _spool_sink = SpoolSink(self._spool_dir or _default_spool_dir())
                # Read the NOVAFABRIC_* contract vars directly (not via
                # capsule.env_contract.read_env(), which synthesizes its own
                # random placeholder global_run_id when unset — calling it a
                # second time here, independently of CapsuleWriter's own
                # read_env() call, would desync the two). Falling back to
                # run_id matches the schema's own documented invariant
                # (global_run_id == run_id for STANDALONE/PARENT capsules).
                _spool_sink.emit_event(
                    event_type="RunStarted",
                    run_id=run_id,
                    agent_id=_spool_agent_id,
                    global_run_id=os.environ.get("NOVAFABRIC_GLOBAL_RUN_ID") or run_id,
                    parent_run_id=os.environ.get("NOVAFABRIC_PARENT_RUN_ID") or None,
                )
            except Exception:
                _spool_sink = None  # fail-open: never block the run

        # Hot-path optimization (v0.6.8 / benchmark finding): skip
        # OpenLineage event construction entirely when no transport is
        # configured. Building the event was ~tens of ms; the env-var
        # probe is microseconds.
        try:
            from novafabric.lineage._ol_transport import (
                emit_if_configured,
                is_configured,
            )
            if is_configured():
                from novafabric.lineage._openlineage import build_start_event
                emit_if_configured([build_start_event(run_id, command, created_at)])
        except Exception as _ol_exc:
            import sys as _sys
            print(f"[novafabric] ⚠ OpenLineage START failed: {_ol_exc}", file=_sys.stderr)

        # Build env for the runner — same vars the previous inline subprocess
        # path used (NOVAFABRIC_CAPSULE_DIR + NOVAFABRIC_SPAN_ID); the runner
        # is responsible for adding PYTHONPATH for the sitecustomize loader.
        runner_env = dict(os.environ)
        runner_env["NOVAFABRIC_CAPSULE_DIR"] = str(capsule_dir)
        runner_env["NOVAFABRIC_SPAN_ID"] = root_span_id
        if self._fast_emit:
            runner_env["NOVAFABRIC_FAST_EMIT"] = "1"
        if self._capture_media:
            # ADR-0125 D2: enable media byte capture in the workload
            # subprocess, where the hooks (and CapsuleWriter funnel) run.
            runner_env["NOVAFABRIC_CAPTURE_MEDIA"] = "1"

        # Hot-path optimization (v0.6.8 / benchmark finding): the env-lock
        # capture (importlib.metadata walk over installed packages) costs
        # ~50 ms per invocation and is independent of the workload.
        # Run it in a worker thread alongside the subprocess so its cost
        # overlaps with the workload's wall-clock instead of stacking on
        # top of it. The subprocess typically dominates (≥ 50 ms for any
        # real workload), so the env-lock cost is fully hidden.
        from concurrent.futures import ThreadPoolExecutor
        env_executor = ThreadPoolExecutor(max_workers=1)
        env_future = env_executor.submit(
            capture_environment, created_at=created_at, run_id=run_id,
        )

        runner_result = self._runner.run(RunnerJobSpec(
            run_id=run_id,
            command=command,
            capsule_dir=capsule_dir,
            env=runner_env,
            runner_options=runner_options,
            timeout_s=timeout_s,
        ))
        exit_code = runner_result.exit_code
        stdout_bytes = runner_result.stdout
        stderr_bytes = runner_result.stderr

        finished_at = _now()
        duration_ms = int((time.monotonic() - t0) * 1000)
        status = "success" if exit_code == 0 else "failure"

        if stdout_bytes:
            (capsule_dir / "outputs" / "stdout.txt").write_bytes(stdout_bytes)
        if stderr_bytes:
            (capsule_dir / "outputs" / "stderr.txt").write_bytes(stderr_bytes)

        writer.append_trace_span({
            "span_id": root_span_id,
            "parent_span_id": None,
            "name": "novafabric.capture",
            "started_at": created_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "status": "ok" if exit_code == 0 else "error",
            "attributes": {"command": command, "exit_code": exit_code},
        })

        # Block on the parallel env-lock capture (started above). For a
        # workload longer than ~50 ms this is effectively free.
        env_lock = env_future.result()
        env_executor.shutdown(wait=False)
        writer.write_text("env.lock", yaml.dump(env_lock, allow_unicode=True))

        scanner = SecretScannerV0(capsule_dir=capsule_dir, run_id=run_id)
        proof = scanner.scan_and_redact()
        # ADR-0135: operator-registered maskers run after the built-in scanner
        # (built-ins are never disabled) and before the proof is written, so no
        # raw value the maskers redact ever persists. Absent pipeline ⇒ the
        # proof is byte-for-byte ADR-0009.
        if self._masking_pipeline is not None:
            try:
                masker_findings, masker_errors = self._masking_pipeline.run(
                    capsule_dir, run_id=run_id
                )
                proof["masker_findings"] = masker_findings
                proof["masker_errors"] = masker_errors
                proof = recompute_chain_hash(proof)
            except Exception:  # noqa: BLE001 — fail-safe: never block capture
                logger.exception(
                    "novafabric: masking pipeline failed; built-in redaction "
                    "already applied, capture continues"
                )
        writer.write_text("redaction-proof.json", json.dumps(proof, indent=2))

        writer.write_text("replay.yaml", yaml.dump(minimal_replay_policy(), allow_unicode=True))

        working_dir = str(Path.cwd()).replace(str(Path.home()), "~")
        model_call_count = sum(
            1
            for line in (capsule_dir / "model-calls.jsonl").read_text().splitlines()
            if line.strip()
        )
        tool_call_count = sum(
            1
            for line in (capsule_dir / "tool-calls.jsonl").read_text().splitlines()
            if line.strip()
        )

        # ADR-0092 slice C (C0) + ADR-0220 follow-up: re-emit each locally-
        # captured model/tool call as a spool event so `nova kg ingest
        # --source nats` has real per-call data (previously only
        # RunStarted/RunCompleted/RunFailed were emitted). Fail-open.
        if _spool_sink is not None:
            from novafabric.capture.spool_sink import emit_call_events_from_capsule

            emit_call_events_from_capsule(
                _spool_sink,
                capsule_dir,
                run_id=run_id,
                agent_id=_spool_agent_id,
                global_run_id=os.environ.get("NOVAFABRIC_GLOBAL_RUN_ID") or run_id,
            )

        # ADR-0125: list every stored media blob as an output Artifact so its
        # content_hash is inside the sealed manifest (NovaSeal signing scope) —
        # post-hoc blob tampering is then detectable. Empty when no media blob
        # was captured (the pre-ADR-0125 manifest is byte-identical). Fail-open.
        media_artifacts: list[dict[str, Any]] = []
        try:
            from novafabric.capture.media import collect_media_artifacts
            media_artifacts = collect_media_artifacts(capsule_dir)
        except Exception:  # noqa: BLE001 — manifest build must never fail on media
            media_artifacts = []

        manifest: dict[str, Any] = {
            "schema_version": "0.1.0",
            "run_id": run_id,
            "created_at": created_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "status": status,
            "command": command,
            "capture_mode": "cli-wrapper",
            "novafabric_version": _pkg_version("novafabric"),
            "working_directory": working_dir,
            "host": _build_host_info(),
            "environment_ref": "env.lock",
            "replay_policy_ref": "replay.yaml",
            "redaction_proof_ref": "redaction-proof.json",
            "trace_ref": "trace.jsonl",
            "trace_root_span_id": root_span_id,
            "model_calls_ref": "model-calls.jsonl",
            "tool_calls_ref": "tool-calls.jsonl",
            "assets_ref": "assets.jsonl",
            # Event streams (v0.16.0) — only referenced when the recorder
            # actually wrote them, so consumers can rely on the ref existing.
            **_event_stream_refs(capsule_dir),
            "inputs": [],
            "outputs": media_artifacts,
            "model_call_count": model_call_count,
            "tool_call_count": tool_call_count,
            "mutating_tool_count": 0,
            "exit_code": exit_code,
        }
        # ADR-0126: additive optional deployment-context tag. Absent when no
        # explicit source supplied it — the manifest is byte-compatible with
        # pre-ADR-0126 capsules in that case.
        if deployment_env is not None:
            manifest["deployment_environment"] = deployment_env.value
            manifest["environment_source"] = deployment_env.source
        # ADR-0116: additive optional variant-attribution block (record-only —
        # which experiment/variant an EXTERNAL allocator had active). Absent
        # when no explicit source supplied it — the manifest is byte-compatible
        # with pre-ADR-0116 capsules in that case.
        if variant_attr is not None:
            manifest["variant"] = variant_attr.to_manifest_block()
        # ADR-0122: additive optional session back-reference (record-only —
        # the session.json manifest stays the authoritative ordered index).
        # Absent when no explicit source supplied it — the manifest is
        # byte-compatible with pre-ADR-0122 capsules in that case.
        if session_membership is not None:
            manifest["session_id"] = session_membership.session_id
            if session_membership.sequence is not None:
                manifest["sequence"] = session_membership.sequence
        # ADR-0132 D3: per-usage-type roll-up over the nova.usage blocks in
        # model-calls.jsonl. Pure sum; absent per-call fields are skipped
        # (absent != zero). The key itself is absent when no call reported a
        # usage breakdown — additive and optional.
        usage_totals = usage_totals_from_model_calls(capsule_dir / "model-calls.jsonl")
        if usage_totals is not None:
            manifest["usage_totals"] = usage_totals
        if status == "failure":
            manifest["error"] = {
                "type": "NonZeroExit",
                "message": f"Command exited with code {exit_code}",
                "traceback_ref": None,
            }

        # ADR-0074: reference the C2PA provenance marker in the manifest (so it
        # is covered by the NovaSeal signature) when live marking is enabled and
        # the run produced synthetic content (≥1 model call).
        if self._mark_provenance and model_call_count > 0:
            manifest["content_provenance_ref"] = "c2pa-manifest.json"

        writer.write_text("capsule.yaml", yaml.dump(manifest, allow_unicode=True))

        # ADR-0074: write the C2PA synthetic-content provenance marker now —
        # after capsule.yaml exists (the exporter reads it) and before sealing,
        # so the EU AI Act Art.50 disclosure is part of the sealed evidence.
        if self._mark_provenance and model_call_count > 0:
            _mark_content_provenance(capsule_dir)

        # --- PII Detection Gate (cap-001 — OQ-01 unresolved, legal-hold mode) ---
        # If a PIIDetectionGate is configured, scan the manifest before sealing.
        # LEGAL-HOLD MODE: redacted capsules go to legal_hold/ staging, NOT sealed.
        # PIIScanError aborts with non-zero exit (fail-closed).
        if self._pii_gate is not None:
            import sys as _sys
            if self._legal_hold:
                try:
                    from novafabric.compliance.pii.gate import PIIScanError
                    pii_result = self._pii_gate.scan(
                        payload=manifest,
                        capsule_id=run_id,
                        subject_id="capsule_manifest",
                    )
                    if pii_result.redacted_count > 0:
                        # Write redacted manifest to legal_hold staging only.
                        legal_hold_dir = (
                            self._base_dir.parent / "legal_hold" / run_id
                        )
                        legal_hold_dir.mkdir(parents=True, exist_ok=True)
                        import yaml as _yaml
                        (legal_hold_dir / "capsule.yaml").write_text(
                            _yaml.dump(pii_result.payload, allow_unicode=True)
                        )
                        (legal_hold_dir / "redaction-manifest.json").write_text(
                            pii_result.manifest.model_dump_json(indent=2)
                        )
                        print(
                            f"[novafabric] PII gate: {pii_result.redacted_count} spans "
                            f"redacted → staged at {legal_hold_dir} (legal-hold mode)",
                            file=_sys.stderr,
                        )
                except PIIScanError as _pii_exc:
                    print(f"[novafabric] ✗ PII scan failed: {_pii_exc}", file=_sys.stderr)
                    raise SystemExit(3) from _pii_exc
            else:
                print(
                    "[novafabric] PII gate is active but --legal-hold was not passed. "
                    "Skipping PII scan (OQ-01 unresolved — legal-hold mode required).",
                    file=_sys.stderr,
                )

        # --- NovaSeal (v0.10 Phase 0 — opt-in, non-blocking) ---
        _seal_capsule(capsule_dir, manifest)

        # --- lineage emission (v0.4) ---
        # Always write lineage.jsonl AND index the run into the lineage
        # DB — even for no-edge captures, the run-node must be
        # queryable via `nova lineage provenance <run_id>`. The SQLite
        # open is the irreducible cost of that contract; deferring it
        # to first-query would be an architectural change (queued for
        # v0.7+, not v0.6.8).
        try:
            from novafabric.lineage._importer import index_capsule_lineage
            from novafabric.lineage._writer import LineageWriter

            lw = LineageWriter(capsule_dir=capsule_dir, run_id=run_id)
            lineage_edges = lw.infer()
            lw.write(lineage_edges)
            index_capsule_lineage(capsule_dir)
        except Exception as _lineage_exc:
            import sys as _sys
            print(
                f"[novafabric] ⚠ Lineage index update failed: {_lineage_exc}\n"
                f"  Run `nova lineage import {capsule_dir}` to repair.",
                file=_sys.stderr,
            )
        # --- end lineage emission ---

        # Hot-path optimization (v0.6.8): same is_configured() short-
        # circuit as the START event above.
        try:
            from novafabric.lineage._ol_transport import (
                emit_if_configured,
                is_configured,
            )
            if is_configured():
                from novafabric.lineage._openlineage import build_complete_event
                emit_if_configured([build_complete_event(
                    run_id, command, capsule_dir, exit_code, finished_at, created_at
                )])
        except Exception as _ol_exc:
            import sys as _sys
            print(f"[novafabric] ⚠ OpenLineage COMPLETE failed: {_ol_exc}", file=_sys.stderr)

        # Surface any fail-open capture loss as evidence before teardown:
        # writes capture-health.json only when events were actually dropped.
        _event_recorder.finalize_health()

        # Clear the module-level recorder singleton now that the run is done.
        set_current_recorder(None)

        # ADR-0092 slice C (C0): close the run on the spool. Fail-open.
        if _spool_sink is not None:
            try:
                _spool_sink.emit_event(
                    event_type="RunCompleted" if exit_code == 0 else "RunFailed",
                    run_id=run_id,
                    agent_id=_spool_agent_id,
                    global_run_id=os.environ.get("NOVAFABRIC_GLOBAL_RUN_ID") or run_id,
                    payload={"exit_code": exit_code},
                )
            finally:
                _spool_sink.close()

        # --- ADR-0137: capsule.created lifecycle event (opt-in via
        # NOVA_EVENTS_*; NullSink no-op when unconfigured; fail-safe) ---
        try:
            from novafabric.events.emitter import emit_lifecycle_event

            emit_lifecycle_event(
                event_type="capsule.created",
                subject_kind="capsule",
                subject_ref=run_id,
                subject_digest=_sha256((capsule_dir / "capsule.yaml").read_bytes()),
                payload={
                    "status": status,
                    "exit_code": exit_code,
                    "duration_ms": duration_ms,
                    "model_call_count": model_call_count,
                    "tool_call_count": tool_call_count,
                },
                source="nova capture",
            )
        except Exception:  # noqa: BLE001 — emission must never block the run
            pass

        return CaptureResult(run_id=run_id, capsule_dir=capsule_dir, exit_code=exit_code)


# ---------------------------------------------------------------------------
# NovaSeal integration (opt-in, non-blocking)
# ---------------------------------------------------------------------------

def _mark_content_provenance(capsule_dir: Path) -> None:
    """Write a C2PA synthetic-content provenance marker into *capsule_dir*.

    ADR-0074 / EU AI Act Art.50: emits ``c2pa-manifest.json`` containing the
    machine-readable ``c2pa.ai.generated: true`` disclosure for the model
    output recorded in this capsule.

    Non-blocking: any failure logs a warning and returns without raising —
    a provenance-marking failure must never fail the captured workload.
    """
    import sys as _sys
    try:
        from novafabric.evidence.c2pa_exporter import export_c2pa
        export_c2pa(capsule_dir, capsule_dir / "c2pa-manifest.json")
    except Exception as exc:
        print(
            f"[novafabric] ⚠ C2PA provenance marking failed (capsule is still valid): {exc}",
            file=_sys.stderr,
        )


def _seal_capsule(capsule_dir: Path, manifest: dict[str, Any]) -> None:
    """Apply NovaSeal signing to *capsule_dir* if NovaSeal is configured.

    Non-blocking: any failure logs a warning and returns without raising.
    NovaSeal is skipped entirely if no config is found.
    """
    import sys as _sys
    try:
        from novafabric.trust.novaseal.config import SealConfigError, load_signing_profile
        profile = load_signing_profile()
        if profile is None:
            return  # NovaSeal not configured — skip silently

        from novafabric.trust.novaseal import KeyConfig, NovaSeal

        config = KeyConfig(
            profile=profile.profile,
            key_path=str(profile.key_path or ""),
            cert_path=str(profile.cert_path),
        )
        # The cloud profiles have no local private key, so they must sign through
        # a SigningBackend. Without this the seal fell through to the local PEM
        # branch of create_envelope() and failed with a misleading
        # "No such file or directory: 'None'" — which meant aws_kms, azure_kv and
        # gcp_kms could never actually seal a capsule.
        backend = None
        if profile.profile != "local":
            from novafabric.trust.novaseal.config import build_signing_backend

            backend = build_signing_backend(profile)
        seal = NovaSeal(
            config=config,
            tsa_url=profile.tsa_url,
            tsa_urls=profile.tsa_urls,
            db_path=str(profile.merkle_db),
            backend=backend,
        )
        bundle = seal.seal(manifest)

        seal_dir = capsule_dir / ".seal"
        seal_dir.mkdir(exist_ok=True)
        (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
        (seal_dir / "manifest.dsse.tsr").write_bytes(bundle.tsr)
        import json as _json
        (seal_dir / "log-entry.json").write_text(
            _json.dumps(bundle.log_entry, indent=2), encoding="utf-8"
        )

    except SealConfigError as exc:
        print(f"[novafabric] ⚠ NovaSeal config error: {exc}", file=_sys.stderr)
    except Exception as exc:
        print(f"[novafabric] ⚠ NovaSeal sealing failed (capsule is still valid): {exc}",
              file=_sys.stderr)
