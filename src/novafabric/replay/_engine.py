from __future__ import annotations

import difflib
import hashlib
import json
import os
import subprocess
import tempfile
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from novafabric.audit import AuditEventType, AuditLog, resolve_audit_log_path
from novafabric.capture._tool_codec import MUTATION_CLASSES
from novafabric.capture._ulid import new_ulid
from novafabric.capture.record_roles import logical_model_calls
from novafabric.policy import (
    PolicyDeniedError,
    PolicyInput,
    PolicyResource,
    PolicySubject,
    deployment_environment_from_capsule,
    get_policy_engine,
)
from novafabric.replay._contract import (
    ReplayContractReport,
    interceptable_tool_calls,
    is_servable_tool_record,
    read_events,
    summarize,
)
from novafabric.replay._dispatcher import REPLAY_DISPATCHER_UNAVAILABLE_EXIT, TOOL_LADDER_ENV
from novafabric.replay._env_check import EnvironmentResolver
from novafabric.replay._errors import CapsuleNotReplayableError
from novafabric.replay._flags import ReplayFlags
from novafabric.replay._policy import PolicyEvaluator
from novafabric.replay._replayability import (
    not_reexecutable_reason,
    refusal_message,
    require_reexecutable_command,
)
from novafabric.replay._result import ReplayResult, write_replay_result

#: Written as ``sitecustomize.py`` into the replayed process (ADR-0300). If the
#: replayed interpreter cannot even import novafabric (e.g. a different venv),
#: the failure is logged with the stdlib only, and a strict replay stops the
#: process before the workload runs instead of letting it call the network.
_MOCK_HOOK_LOADER = textwrap.dedent(f"""\
    import os as _os, sys as _sys, json as _json
    if _os.environ.get("NOVAFABRIC_REPLAY_QUEUE_PATH", ""):
        try:
            from novafabric.replay._dispatcher import install_from_env as _nf_install
        except Exception as _e:
            print(f"[novafabric] mock dispatcher install failed: {{_e}}", file=_sys.stderr)
            _events = _os.environ.get("NOVAFABRIC_REPLAY_EVENTS_PATH", "")
            if _events:
                try:
                    with open(_events, "a", encoding="utf-8") as _fh:
                        _fh.write(_json.dumps({{
                            "event": "install_failed", "pid": _os.getpid(),
                            "error": type(_e).__name__ + ": " + str(_e),
                        }}) + "\\n")
                except OSError:
                    pass
            if _os.environ.get("NOVAFABRIC_REPLAY_DIVERGENCE_POLICY") != "warn":
                _os._exit({REPLAY_DISPATCHER_UNAVAILABLE_EXIT})
        else:
            _nf_install()
""")

#: How long a mocked/intervention replay subprocess may run before it is killed.
#: Named because the timeout is now reported as a distinct outcome rather than
#: being flattened into an exit code.
REPLAY_SUBPROCESS_TIMEOUT_S = 600



def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def _servable_tool_count(tool_calls: list[dict[str, Any]]) -> int:
    """ADR-0300: `tool_calls_available` = recorded calls a tool dispatcher can
    serve (MCP, and servable `record.tool` records -- ADR-0306);
    `tool_calls_recorded` carries the full count beside it."""
    return sum(1 for r in interceptable_tool_calls(tool_calls) if is_servable_tool_record(r))


def _contract_fields(report: ReplayContractReport) -> dict[str, Any]:
    """The ADR-0300 counters a mocked replay result carries."""
    return {
        "model_calls_mocked": report.model_calls_mocked,
        "model_calls_available": report.model_calls_available,
        "model_calls_unmatched": report.model_calls_unmatched,
        "tool_calls_mocked": report.tool_calls_mocked,
        "tool_calls_available": report.tool_calls_available,
        "tool_calls_recorded": report.tool_calls_recorded,
        "tool_calls_live": report.tool_calls_live,
        "tool_calls_unmatched": report.tool_calls_unmatched,
        "queues_fully_consumed": report.queues_fully_consumed,
        "divergence_reason": report.divergence_reason,
        "replay_contract": report.as_dict(),
    }


def _load_capsule(capsule_dir: Path) -> dict[str, Any]:
    manifest_path = capsule_dir / "capsule.yaml"
    if not manifest_path.exists():
        raise ValueError(f"capsule.yaml not found in {capsule_dir}")
    return yaml.safe_load(manifest_path.read_text()) or {}


def _load_replay_policy(capsule_dir: Path) -> dict[str, Any]:
    path = capsule_dir / "replay.yaml"
    if not path.exists():
        return {"schema_version": "0.1.0"}
    return yaml.safe_load(path.read_text()) or {}


def _load_env_lock(capsule_dir: Path) -> dict[str, Any]:
    path = capsule_dir / "env.lock"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


class ReplayEngine:
    def __init__(
        self,
        capsule_dir: Path,
        flags: ReplayFlags,
        base_dir: Path | None = None,
        actor: str = "cli",
    ) -> None:
        self._capsule_dir = capsule_dir
        self._flags = flags
        self._base_dir = base_dir or (Path.cwd() / ".novafabric" / "replays")
        self._actor = actor

    def run(self) -> ReplayResult:
        manifest = _load_capsule(self._capsule_dir)
        # ADR-0126 opt-in gate: refuse before anything runs (no policy call,
        # no replay directory) unless the capsule recorded the environment.
        if self._flags.required_environment is not None:
            from novafabric.replay.environment_gate import check_replay_environment

            check_replay_environment(self._capsule_dir, self._flags.required_environment)
        replay_policy = _load_replay_policy(self._capsule_dir)
        env_lock = _load_env_lock(self._capsule_dir)
        # ADR-0305: replay sees logical calls only -- the wire hook's transport
        # records are neither served nor counted (model_calls_recorded).
        model_calls = logical_model_calls(
            _read_jsonl(self._capsule_dir / "model-calls.jsonl")
        )
        tool_calls = _read_jsonl(self._capsule_dir / "tool-calls.jsonl")

        env_warnings = EnvironmentResolver().check(env_lock)
        run_id = manifest.get("run_id", "unknown")
        evaluator = PolicyEvaluator(replay_policy, self._flags)

        # ADR-0128: re-validate stored tool calls against their *current*
        # declared schemas. Drift is recorded on the replay result in every
        # mode; only `exact` gates on it (hard refusal via exact_reasons).
        from novafabric.capture.schema_validation import revalidate_tool_calls

        schema_drift = revalidate_tool_calls(tool_calls, self._capsule_dir)

        # Policy gate — only for mutating replay; read-only/forensic modes are
        # never blocked because they never execute live side-effecting tools.
        if self._flags.allow_mutating:
            engine = get_policy_engine()
            inp = PolicyInput(
                action="replay_mutating",
                subject=PolicySubject(user=self._actor),
                resource=PolicyResource(
                    kind="capsule",
                    ref=run_id,
                    # ADR-0126: recorded value verbatim; null when absent.
                    deployment_environment=deployment_environment_from_capsule(
                        self._capsule_dir
                    ),
                ),
            )
            decision = engine.evaluate(inp)
            AuditLog(resolve_audit_log_path()).append(
                event_type=(
                    AuditEventType.POLICY_ALLOW
                    if decision.allow
                    else AuditEventType.POLICY_DENY
                ),
                actor=self._actor,
                resource_id=run_id,
                details={
                    "decision_id": decision.decision_id,
                    "reason": decision.reason,
                    "action": "replay_mutating",
                },
            )
            if not decision.allow:
                raise PolicyDeniedError(decision.reason, decision.decision_id)

        if self._flags.dry_run:
            return self._dry_run(run_id, manifest, evaluator, tool_calls, env_warnings)

        if self._flags.mode == "forensic":
            return self._forensic(
                run_id, manifest, model_calls, tool_calls, env_warnings, schema_drift
            )

        if self._flags.mode == "semantic":
            return self._semantic(run_id, model_calls, env_warnings, schema_drift)

        if self._flags.mode == "exact":
            return self._exact(
                run_id, env_lock, model_calls, env_warnings, schema_drift,
                manifest=manifest,
            )

        if self._flags.mode == "intervention":
            return self._intervention(
                manifest, run_id, model_calls, tool_calls, env_warnings
            )

        return self._mocked(
            manifest,
            run_id,
            model_calls,
            tool_calls,
            evaluator,
            env_warnings,
            schema_drift,
        )

    def _intervention(
        self,
        manifest: dict[str, Any],
        run_id: str,
        model_calls: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]],
        env_warnings: list[Any],
    ) -> ReplayResult:
        """Counterfactual replay (ADR-0086, experimental).

        Source capsule is read-only; substitution applied to deep copies;
        downstream steps re-execute under mocked semantics; output is a
        minimal capsule hard-marked ``replay_mode: intervention``.
        """
        from novafabric.replay._intervention import (
            apply_intervention,
            load_intervention_spec,
            run_checks,
            write_intervention_capsule,
        )

        start = _now()
        t0 = time.monotonic()
        replay_id = new_ulid()
        result_dir = self._base_dir / replay_id

        spec = load_intervention_spec(self._flags.intervention_file)
        mutated_model_calls, mutated_tool_calls, matched_index = apply_intervention(
            spec, model_calls, tool_calls
        )
        check_outcomes = run_checks(
            spec.checks, mutated_model_calls, mutated_tool_calls
        )
        fatal_failures = [
            o.name
            for o, c in zip(check_outcomes, spec.checks)
            if not o.passed and c.fatal
        ]

        intervention_meta: dict[str, Any] = {
            **spec.describe(),
            "matched_event_index": matched_index,
            "checks": [o.model_dump() for o in check_outcomes],
        }

        if fatal_failures:
            result = ReplayResult(
                replay_id=replay_id,
                replay_of_run_id=run_id,
                mode="intervention",
                status="aborted",
                start_time=start,
                end_time=_now(),
                duration_ms=int((time.monotonic() - t0) * 1000),
                policy_flags_used=self._flags.active_flag_names(),
                env_warnings=[w.as_dict() for w in env_warnings],
                intervention=intervention_meta,
                error={
                    "type": "FatalCheckFailed",
                    "message": (
                        "fatal check(s) failed: " + ", ".join(fatal_failures)
                    ),
                },
            )
            write_replay_result(result, result_dir)
            return result

        exit_code: int | None = None
        not_reexecuted = not_reexecutable_reason(manifest)
        if not_reexecuted is None:
            command: list[str] = manifest.get("command", [])
            exit_code, run_error = self._run_mocked_subprocess(
                command, mutated_model_calls
            )
            status = "success" if exit_code == 0 else "failure"
        else:
            # No re-executable command (an empty command, an otel-import or a
            # framework-adapter `@framework:name` label): never spawn it --
            # emit the mutated streams only, and say so on the result.
            status = "success"
            run_error = None
        intervention_meta["downstream_reexecuted"] = not_reexecuted is None
        if not_reexecuted is not None:
            intervention_meta["downstream_not_reexecuted_reason"] = not_reexecuted

        # Only the model-calls stream feeds the re-executed workload (through the
        # mocked queue). Intervention installs no tool dispatcher (ADR-0300), so a
        # tool-calls substitution changes the output capsule and the checks, never
        # what the workload saw — say so rather than imply the effect was measured.
        reexecuted = not_reexecuted is None
        delivered = reexecuted and spec.target.stream == "model-calls"
        intervention_meta["substitution_delivered_to_workload"] = delivered
        if not delivered:
            intervention_meta["substitution_note"] = (
                f"no command was re-executed ({not_reexecuted}); the substitution "
                "is applied to the output capsule's streams and the checks only"
                if not reexecuted
                else "the re-executed workload's tools ran live and never saw the "
                "substituted tool-call record; it is applied to the output "
                "capsule's streams and the checks only (ADR-0300, ADR-0306)"
            )

        result = ReplayResult(
            replay_id=replay_id,
            replay_of_run_id=run_id,
            mode="intervention",
            status=status,
            start_time=start,
            end_time=_now(),
            duration_ms=int((time.monotonic() - t0) * 1000),
            policy_flags_used=self._flags.active_flag_names(),
            env_warnings=[w.as_dict() for w in env_warnings],
            model_calls_mocked=len(mutated_model_calls),
            # ADR-0300: intervention installs no tool dispatcher (a
            # counterfactual's tools run live), so nothing is substituted.
            tool_calls_mocked=0,
            tool_calls_available=_servable_tool_count(mutated_tool_calls),
            tool_calls_recorded=len(mutated_tool_calls),
            exit_code=exit_code,
            intervention=intervention_meta,
        )
        if status == "failure":
            result.error = run_error or {
                "type": "NonZeroExit",
                "message": f"Replayed command exited with code {exit_code}",
            }
        write_replay_result(result, result_dir)
        write_intervention_capsule(
            result_dir=result_dir,
            replay_id=replay_id,
            source_run_id=run_id,
            source_manifest=manifest,
            spec=spec,
            matched_index=matched_index,
            model_calls=mutated_model_calls,
            tool_calls=mutated_tool_calls,
            check_outcomes=check_outcomes,
        )
        return result

    def _forensic(
        self,
        run_id: str,
        manifest: dict[str, Any],
        model_calls: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]],
        env_warnings: list[Any],
        schema_drift: list[dict[str, Any]] | None = None,
    ) -> ReplayResult:
        start = _now()
        t0 = time.monotonic()

        replay_id = new_ulid()
        result_dir = self._base_dir / replay_id
        result = ReplayResult(
            replay_id=replay_id,
            replay_of_run_id=run_id,
            mode="forensic",
            status="success",
            start_time=start,
            end_time=_now(),
            duration_ms=int((time.monotonic() - t0) * 1000),
            policy_flags_used=["--mode=forensic"],
            env_warnings=[w.as_dict() for w in env_warnings],
            model_calls_mocked=len(model_calls),
            tool_calls_mocked=0,  # ADR-0261: forensic executes nothing
            tool_calls_available=_servable_tool_count(tool_calls),
            tool_calls_recorded=len(tool_calls),
            schema_drift=schema_drift or None,
        )
        write_replay_result(result, result_dir)
        return result

    def _dry_run(
        self,
        run_id: str,
        manifest: dict[str, Any],
        evaluator: PolicyEvaluator,
        tool_calls: list[dict[str, Any]],
        env_warnings: list[Any],
    ) -> ReplayResult:
        start = _now()
        t0 = time.monotonic()
        replay_id = new_ulid()
        result_dir = self._base_dir / replay_id

        report = evaluator.dry_run_report(tool_calls)
        # Say what the real run would do with a capsule that records no
        # command: `mocked` refuses it (same message, same error), and
        # `intervention` emits the counterfactual streams without re-running.
        refusal: dict[str, Any] | None = None
        if self._flags.mode == "mocked":
            message = refusal_message(manifest, "mocked")
            if message is not None:
                refusal = {"type": CapsuleNotReplayableError.error_type, "message": message}
                report = f"REFUSED: {message}\n\n{report}"
        elif self._flags.mode == "intervention":
            reason = not_reexecutable_reason(manifest)
            if reason is not None:
                report = (
                    f"NOT RE-EXECUTED: {reason}; --mode intervention would emit "
                    f"the counterfactual streams only.\n\n{report}"
                )

        result = ReplayResult(
            replay_id=replay_id,
            replay_of_run_id=run_id,
            mode=self._flags.mode,
            status="dry_run",
            start_time=start,
            end_time=_now(),
            duration_ms=int((time.monotonic() - t0) * 1000),
            policy_flags_used=self._flags.active_flag_names(),
            env_warnings=[w.as_dict() for w in env_warnings],
            model_calls_mocked=0,
            tool_calls_mocked=0,  # ADR-0261: a dry run executes nothing
            tool_calls_available=_servable_tool_count(tool_calls),
            tool_calls_recorded=len(tool_calls),
            error=refusal,
        )
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "dry_run_report.txt").write_text(report)
        write_replay_result(result, result_dir)
        return result

    def _semantic(
        self,
        run_id: str,
        model_calls: list[dict[str, Any]],
        env_warnings: list[Any],
        schema_drift: list[dict[str, Any]] | None = None,
    ) -> ReplayResult:
        start = _now()
        t0 = time.monotonic()
        replay_id = new_ulid()
        result_dir = self._base_dir / replay_id

        # Extract response text from each model call and compute pairwise similarity.
        texts = []
        for mc in model_calls:
            # Records use flat OTel-GenAI keys (gen_ai.response.choices), the
            # same shape the mock dispatcher reads — not a nested "response".
            choices = mc.get("gen_ai.response.choices", [])
            if choices:
                content = choices[0].get("message", {}).get("content") or ""
                texts.append(str(content))
        if len(texts) >= 2:
            pairs = [
                difflib.SequenceMatcher(None, texts[i], texts[j]).ratio()
                for i in range(len(texts))
                for j in range(i + 1, len(texts))
            ]
            similarity_score = round(sum(pairs) / len(pairs), 4)
        else:
            similarity_score = 1.0

        result = ReplayResult(
            replay_id=replay_id,
            replay_of_run_id=run_id,
            mode="semantic",
            status="success",
            start_time=start,
            end_time=_now(),
            duration_ms=int((time.monotonic() - t0) * 1000),
            policy_flags_used=["--mode=semantic"],
            env_warnings=[w.as_dict() for w in env_warnings],
            model_calls_mocked=len(model_calls),
            similarity_score=similarity_score,
            matched_run_id=run_id,
            schema_drift=schema_drift or None,
        )
        write_replay_result(result, result_dir)
        return result

    def _exact(
        self,
        run_id: str,
        env_lock: dict[str, Any],
        model_calls: list[dict[str, Any]],
        env_warnings: list[Any],
        schema_drift: list[dict[str, Any]] | None = None,
        *,
        manifest: dict[str, Any] | None = None,
    ) -> ReplayResult:
        start = _now()
        t0 = time.monotonic()
        replay_id = new_ulid()
        result_dir = self._base_dir / replay_id

        reasons: list[str] = []
        # A byte-exact re-run needs something to re-run.
        not_reexecuted = None if manifest is None else not_reexecutable_reason(manifest)
        if not_reexecuted is not None:
            reasons.append(f"{not_reexecuted} — exact replay needs a re-runnable command")
        # "mode" is the canonical field name (environment.schema.json).
        # Old capsules (pre-v0.2) may omit it; treat as "best-effort".
        env_mode = env_lock.get("mode") or env_lock.get("lock_mode") or "best-effort"
        if env_mode != "deterministic":
            reasons.append(
                f"env.lock mode is '{env_mode}' — "
                "exact replay requires mode=deterministic"
            )

        hash_count = 0
        for mc in model_calls:
            # Records use flat OTel-GenAI keys; the request is the set of
            # gen_ai.request.* attributes, and the seed is gen_ai.request.seed.
            inputs = {k: v for k, v in mc.items() if k.startswith("gen_ai.request.")}
            if inputs:
                canonical = json.dumps(inputs, sort_keys=True, ensure_ascii=False)
                hashlib.sha256(canonical.encode()).hexdigest()
                hash_count += 1
            if mc.get("gen_ai.request.seed") in (None, ""):
                reasons.append(
                    f"model_call {mc.get('model_call_id', '?')[:10]} has no seed — "
                    "exact reproduction of remote LLM responses is not guaranteed"
                )
                break  # one warning is enough to flag the issue

        # ADR-0128: a stored tool result that no longer conforms to its
        # declared schema is a hard refusal in exact mode (fail-closed).
        for finding in schema_drift or []:
            tool_call_id = str(finding.get("tool_call_id") or "?")
            reasons.append(
                f"tool_call {tool_call_id[:10]} does not conform to its declared "
                "schema_ref — schema drift blocks exact replay (ADR-0128)"
            )

        eligible = len(reasons) == 0

        result = ReplayResult(
            replay_id=replay_id,
            replay_of_run_id=run_id,
            mode="exact",
            status="success",
            start_time=start,
            end_time=_now(),
            duration_ms=int((time.monotonic() - t0) * 1000),
            policy_flags_used=["--mode=exact"],
            env_warnings=[w.as_dict() for w in env_warnings],
            model_calls_mocked=len(model_calls),
            exact_eligible=eligible,
            exact_hash_count=hash_count,
            exact_reasons=reasons,
            schema_drift=schema_drift or None,
        )
        write_replay_result(result, result_dir)
        return result

    def _mocked(
        self,
        manifest: dict[str, Any],
        run_id: str,
        model_calls: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]],
        evaluator: PolicyEvaluator,
        env_warnings: list[Any],
        schema_drift: list[dict[str, Any]] | None = None,
    ) -> ReplayResult:
        start = _now()
        t0 = time.monotonic()
        replay_id = new_ulid()
        result_dir = self._base_dir / replay_id

        try:
            command = require_reexecutable_command(manifest, "mocked")
        except CapsuleNotReplayableError as exc:
            # Refused before anything is spawned, and recorded: an adapter
            # capsule's `@framework:name` label is not a program.
            refused = ReplayResult(
                replay_id=replay_id,
                replay_of_run_id=run_id,
                mode="mocked",
                status="aborted",
                start_time=start,
                end_time=_now(),
                duration_ms=int((time.monotonic() - t0) * 1000),
                policy_flags_used=self._flags.active_flag_names(),
                env_warnings=[w.as_dict() for w in env_warnings],
                tool_calls_recorded=len(tool_calls),
                schema_drift=schema_drift or None,
                error=exc.as_error(),
            )
            write_replay_result(refused, result_dir)
            return refused

        policy = self._flags.divergence_policy
        exit_code, run_error, events = self._run_replay_subprocess(
            command, model_calls, tool_calls, divergence_policy=policy
        )
        report = summarize(
            model_calls, tool_calls, events,
            divergence_policy=policy, substitute_tools=True,
        )
        status = "success" if exit_code == 0 else "failure"
        # ADR-0300: under the default fail-closed policy a divergence fails the
        # replay even if the workload caught the dispatcher's exception and
        # exited 0 -- the exit code alone would overstate fidelity.
        if report.diverged and policy == "fail":
            status = "failure"
            if run_error is None:
                run_error = {
                    "type": "ReplayDivergence",
                    "message": report.divergence_reason or "replay diverged",
                }

        result = ReplayResult(
            replay_id=replay_id,
            replay_of_run_id=run_id,
            mode="mocked",
            status=status,
            start_time=start,
            end_time=_now(),
            duration_ms=int((time.monotonic() - t0) * 1000),
            policy_flags_used=self._flags.active_flag_names(),
            env_warnings=[w.as_dict() for w in env_warnings],
            exit_code=exit_code,
            schema_drift=schema_drift or None,
            **_contract_fields(report),
        )
        if status == "failure":
            result.error = run_error or {
                "type": "NonZeroExit",
                "message": f"Replayed command exited with code {exit_code}",
            }
        write_replay_result(result, result_dir)
        return result

    def _run_mocked_subprocess(
        self,
        command: list[str],
        model_calls: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]] | None = None,
        *,
        divergence_policy: str = "warn",
    ) -> tuple[int, dict[str, Any] | None]:
        """Run the replayed command; return its exit code and why it stopped.

        The second element is ``None`` for an ordinary exit (the code says it
        all) and a populated error for a timeout or a launch failure, where the
        exit code alone would misdescribe what happened. The dispatchers' event
        log is discarded; ``_run_replay_subprocess`` returns it.
        """
        code, error, _events = self._run_replay_subprocess(
            command, model_calls, tool_calls, divergence_policy=divergence_policy
        )
        return code, error

    def _run_replay_subprocess(
        self,
        command: list[str],
        model_calls: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]] | None = None,
        *,
        divergence_policy: str = "warn",
    ) -> tuple[int, dict[str, Any] | None, list[dict[str, Any]]]:
        """As ``_run_mocked_subprocess``, plus the dispatchers' event log.

        ``tool_calls=None`` installs no tool dispatcher (tools run live);
        a list -- even an empty one -- installs ``MockToolDispatcher`` on
        ``mcp.ClientSession.call_tool`` (ADR-0300).
        """
        with tempfile.TemporaryDirectory(prefix="nf_replay_") as tmp:
            site_dir = Path(tmp) / "site"
            site_dir.mkdir()
            (site_dir / "sitecustomize.py").write_text(_MOCK_HOOK_LOADER)

            # The full model-call stream, as before ADR-0300: the dispatcher
            # selects the servable records itself (one implementation, in
            # `_contract.model_queues`).
            queue_path = Path(tmp) / "model_queue.json"
            queue_path.write_text(json.dumps(model_calls))
            events_path = Path(tmp) / "replay_events.jsonl"

            env = dict(os.environ)
            env["NOVAFABRIC_REPLAY_QUEUE_PATH"] = str(queue_path)
            env["NOVAFABRIC_REPLAY_EVENTS_PATH"] = str(events_path)
            env["NOVAFABRIC_REPLAY_DIVERGENCE_POLICY"] = divergence_policy
            env["NOVAFABRIC_REPLAY_MODE"] = "mocked"
            # ADR-0306 D7: the mutation classes the operator's ladder flags
            # permit -- an unmatched `record.tool` call may run live under
            # --permissive only for these. From the command line, never the capsule.
            flags = getattr(self, "_flags", None) or ReplayFlags()
            env[TOOL_LADDER_ENV] = ",".join(c for c in MUTATION_CLASSES if flags.permits(c))
            env.pop("NOVAFABRIC_REPLAY_TOOL_QUEUE_PATH", None)
            if tool_calls is not None:
                tool_queue_path = Path(tmp) / "tool_queue.json"
                tool_queue_path.write_text(json.dumps(tool_calls))
                env["NOVAFABRIC_REPLAY_TOOL_QUEUE_PATH"] = str(tool_queue_path)
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = f"{site_dir}:{existing}" if existing else str(site_dir)

            code, error = self._spawn(command, env)
            return code, error, read_events(events_path)

    @staticmethod
    def _spawn(
        command: list[str], env: dict[str, str]
    ) -> tuple[int, dict[str, Any] | None]:
        try:
            proc = subprocess.run(
                command, env=env, capture_output=True,
                timeout=REPLAY_SUBPROCESS_TIMEOUT_S,
            )
            return proc.returncode, None
        except subprocess.TimeoutExpired:
            # 124 is the conventional shell timeout code, but a command may
            # legitimately exit 124 itself — so the code alone cannot say
            # which happened. Reporting this as "exited with code 124" (as
            # this path used to, via the NonZeroExit branch) states something
            # that did not occur: the command did not exit, it was killed.
            return 124, {
                "type": "ReplayTimeout",
                "message": (
                    "the replayed command did not finish within "
                    f"{REPLAY_SUBPROCESS_TIMEOUT_S}s and was terminated; it "
                    "did not exit on its own"
                ),
            }
        except Exception as exc:
            # The command may never have launched. "exited with code 1" would
            # assert an exit that never happened.
            return 1, {
                "type": "ReplayLaunchError",
                "message": f"the replayed command could not be run: {exc}",
            }
