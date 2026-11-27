from __future__ import annotations

import difflib
import json
import os
import shlex
import subprocess
import tempfile
import textwrap
import time
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from novafabric._capsule_fs import read_required_capsule_member
from novafabric._paths import default_replays_dir
from novafabric.audit import AuditEventType, AuditLog, resolve_audit_log_path
from novafabric.capture._tool_codec import MUTATION_CLASSES
from novafabric.capture._ulid import new_ulid
from novafabric.capture.process_invocation import ADAPTER_RUNS_ENV
from novafabric.capture.record_roles import logical_model_calls
from novafabric.policy import (
    PolicyDeniedError,
    PolicyInput,
    PolicyResource,
    PolicySubject,
    deployment_environment_from_capsule,
    get_policy_engine,
)
from novafabric.replay._child_observer import OBSERVE_ENV, ROLE_ENV, SITE_ENV
from novafabric.replay._contract import (
    ReplayContractReport,
    describe_not_served,
    interceptable_tool_calls,
    is_servable_tool_record,
    not_served_dry_run_lines,
    not_served_tool_calls,
    read_events,
    summarize,
)
from novafabric.replay._dispatcher import REPLAY_DISPATCHER_UNAVAILABLE_EXIT, TOOL_LADDER_ENV
from novafabric.replay._echo_recovery import recover_tool_results_from_echo
from novafabric.replay._env_check import EnvironmentResolver
from novafabric.replay._errors import (
    CapsuleNotReplayableError,
    ModelSurfaceNotServableError,
    ReplayOverrideUnenforceableError,
    ToolSurfaceNotServedError,
)
from novafabric.replay._fidelity import compute_fidelity
from novafabric.replay._fidelity import not_run as fidelity_not_run
from novafabric.replay._flags import ReplayFlags
from novafabric.replay._hermetic import (
    HermeticUnavailableError,
    Sandbox,
    check_writable,
    select_sandbox,
)
from novafabric.replay._policy import INSTALL_REQUIRED_ENV, TOOL_POLICY_ENV, PolicyEvaluator
from novafabric.replay._replay_capsule import (
    REPLAY_CAPSULE_FILE,
    write_mocked_replay_capsule,
)
from novafabric.replay._replayability import (
    ReexecutionPlan,
    not_reexecutable_reason,
    refusal_message,
    require_reexecution_plan,
)
from novafabric.replay._result import ReplayResult, write_replay_result
from novafabric.replay._servability import (
    covered_not_servable_records,
    describe_by_system,
    endpoint_host,
    not_servable_by_system,
    not_servable_records,
    system_of,
)

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
            if (_os.environ.get("NOVAFABRIC_REPLAY_DIVERGENCE_POLICY") != "warn"
                    or _os.environ.get("{INSTALL_REQUIRED_ENV}") == "1"):
                _os._exit({REPLAY_DISPATCHER_UNAVAILABLE_EXIT})
        else:
            _nf_install()
    elif _os.environ.get("{OBSERVE_ENV}", ""):
        # A child whose environment lost the dispatcher's variables (ADR-0319,
        # #52 part 2): observe it with the stdlib-only bootstrap; serve nothing.
        try:
            import _nf_child_observer as _nf_obs
            _nf_obs.bootstrap_child(__file__)
        except Exception as _e:
            print(f"[novafabric] child process observer failed: {{_e}}", file=_sys.stderr)
""")

#: Name of the stdlib-only observer's copy in the replay site directory.
_CHILD_OBSERVER_MODULE = "_nf_child_observer"


def write_replay_site(site_dir: Path) -> None:
    """Write the replayed interpreters' ``sitecustomize.py`` and the child
    observer it loads (``_child_observer``'s own source, stdlib only) into
    *site_dir*. Inert in a process without the replay's environment variables."""
    (site_dir / "sitecustomize.py").write_text(_MOCK_HOOK_LOADER)
    source = resources.files("novafabric.replay").joinpath("_child_observer.py").read_text(
        encoding="utf-8"
    )
    (site_dir / f"{_CHILD_OBSERVER_MODULE}.py").write_text(source, encoding="utf-8")

#: How long a mocked/intervention replay subprocess may run before it is killed.
#: Named because the timeout is now reported as a distinct outcome rather than
#: being flattened into an exit code.
REPLAY_SUBPROCESS_TIMEOUT_S = 600



def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _invocation_report(plan: ReexecutionPlan) -> str:
    """What a re-executing replay will run, when it is not ``capsule.yaml:command``
    (ADR-0306 Q7, owner decision O6: shown in the dry run, never prompted)."""
    if plan.source != "process_invocation":
        return ""
    return (
        "Command (the recorded process invocation of an adapter capsule, ADR-0306 Q7):\n"
        f"  {shlex.join(plan.command)}\n"
        f"  cwd: {plan.cwd}\n"
        "  stdin: closed (/dev/null)\n\n"
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    # Absent = no recorded calls. Present but unusable (a link, FIFO, directory)
    # raises CapsuleRequiredFileError: a replay never runs on a partial record.
    records = []
    text = read_required_capsule_member(path, operation="nova replay")
    if text is not None:
        for line in text.splitlines():
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
        "model_calls_not_servable": report.model_calls_not_servable,
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
    # Present but unusable (a link, FIFO, directory) raises CapsuleRequiredFileError.
    text = read_required_capsule_member(capsule_dir / "capsule.yaml", operation="nova replay")
    if text is None:
        raise ValueError(f"capsule.yaml not found in {capsule_dir}")
    return yaml.safe_load(text) or {}


def _load_replay_policy(capsule_dir: Path) -> dict[str, Any]:
    # A replay policy that is present but unusable is an error, never the default.
    text = read_required_capsule_member(capsule_dir / "replay.yaml", operation="nova replay")
    if text is None:
        return {"schema_version": "0.1.0"}
    return yaml.safe_load(text) or {}


def _load_env_lock(capsule_dir: Path) -> dict[str, Any]:
    text = read_required_capsule_member(capsule_dir / "env.lock", operation="nova replay")
    if text is None:
        return {}
    return yaml.safe_load(text) or {}


class ReplayEngine:
    """Replay or inspect one capsule: the engine behind ``nova replay``.

    Args:
        capsule_dir: The capsule to replay.
        flags: Mode and safety settings (:class:`ReplayFlags`).
        base_dir: Where replay output goes; defaults to
            ``$NOVAFABRIC_HOME/replays`` (``default_replays_dir()``).
        actor: The identity the ``replay_mutating`` policy gate evaluates and the
            audit log records.
    """

    def __init__(
        self,
        capsule_dir: Path,
        flags: ReplayFlags,
        base_dir: Path | None = None,
        actor: str = "cli",
    ) -> None:
        self._capsule_dir = capsule_dir
        self._flags = flags
        self._base_dir = base_dir or default_replays_dir()
        self._actor = actor

    def run(self) -> ReplayResult:
        """Replay the capsule according to the flags; write ``replay_result.yaml``.

        A replay that diverges or is refused is reported in the returned result
        (``status`` ``failure`` or ``aborted``, with ``divergence_reason`` or
        ``error``), not raised.

        Raises:
            ValueError: the capsule has no ``capsule.yaml``.
            ReplayEnvironmentMismatchError: ``flags.required_environment`` is set
                and the capsule recorded another deployment environment, or none.
            PolicyDeniedError: a write-permitting flag is set and the
                ``replay_mutating`` policy gate denied *actor*.
        """
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
        # ADR-0319 classifies servability over the whole stream: a record's
        # role depends on the records around it.
        recorded_model_records = _read_jsonl(self._capsule_dir / "model-calls.jsonl")
        model_calls = logical_model_calls(recorded_model_records)
        tool_calls = _read_jsonl(self._capsule_dir / "tool-calls.jsonl")
        if self._flags.mode == "mocked":
            # ADR-0306 r3: a payloads-off tool result the next recorded model
            # request echoes under exactly the recorded digest is servable.
            tool_calls = recover_tool_results_from_echo(tool_calls, recorded_model_records)

        env_warnings = EnvironmentResolver().check(env_lock)
        run_id = manifest.get("run_id", "unknown")
        evaluator = PolicyEvaluator(replay_policy, self._flags)
        # ADR-0306 r3c: a call made inside a tool call that is certain to be
        # served is never made, so it refuses nothing up front (mocked only).
        coverage = evaluator.coverage(tool_calls)
        not_servable = not_servable_by_system(recorded_model_records, coverage)

        # ADR-0128: re-validate stored tool calls against their *current*
        # declared schemas. Drift is recorded on the replay result in every
        # mode; only `exact` gates on it (hard refusal via exact_reasons).
        from novafabric.capture.schema_validation import revalidate_tool_calls

        schema_drift = revalidate_tool_calls(tool_calls, self._capsule_dir)

        # Policy gate — for every replay a write-permitting ladder flag is set on
        # (--allow-mutating, --allow-external-side-effects, --allow-unknown-mutation;
        # ReplayFlags.permits_writes is the one source of truth). Read-only
        # replays are never blocked. A policy engine error propagates: refused.
        if self._flags.permits_writes:
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
            return self._dry_run(
                run_id, manifest, evaluator, tool_calls, env_warnings,
                recorded_model_records=recorded_model_records,
                not_servable=not_servable,
            )

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
            not_servable=not_servable,
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
            plan = require_reexecution_plan(manifest, "intervention")
            exit_code, run_error = self._run_mocked_subprocess(
                plan.command, mutated_model_calls, plan=plan
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
        *,
        recorded_model_records: list[Any] | None = None,
        not_servable: dict[str, dict[str, Any]] | None = None,
    ) -> ReplayResult:
        start = _now()
        t0 = time.monotonic()
        replay_id = new_ulid()
        result_dir = self._base_dir / replay_id
        not_servable = not_servable or {}

        report = evaluator.dry_run_report(tool_calls)
        # Say what the real run would do with a capsule that records no
        # command: `mocked` refuses it (same message, same error), and
        # `intervention` emits the counterfactual streams without re-running.
        refusal: dict[str, Any] | None = None
        if self._flags.mode == "mocked":
            # ADR-0319: each recorded model call replay cannot serve would run live.
            coverage = evaluator.coverage(tool_calls)
            unserved = not_servable_records(recorded_model_records or [], coverage)
            if unserved:
                report += "Model calls mocked replay cannot serve (ADR-0319):\n" + "".join(
                    f"  [LIVE] model call ({system_of(r)}, "
                    f"{endpoint_host(r) or 'host not recorded'})\n"
                    for r in unserved
                )
            # ADR-0306 r3c: unservable model calls made inside a served tool call.
            covered = covered_not_servable_records(recorded_model_records or [], coverage)
            if covered:
                report += (
                    "Model calls inside a served tool call (never made, ADR-0306 r3c):\n"
                    + "".join(
                        f"  [COVERED by {outer.get('tool_name')}] model call "
                        f"({system_of(r)}, {endpoint_host(r) or 'host not recorded'})\n"
                        for r, outer in covered
                    )
                )
            # ADR-0306 r3 amendment: each recorded observed tool call (a LangChain
            # tool not passed through wrap_tools) replay cannot serve.
            not_served = not_served_tool_calls(tool_calls, coverage)
            if not_served:
                report += "Tool calls mocked replay cannot serve (ADR-0306 r3):\n" + "".join(
                    not_served_dry_run_lines(tool_calls, coverage)
                )
            # ADR-0306 D8: what the replayed process will do with each
            # replay.yaml override -- the same table the real run reports.
            overrides = evaluator.override_report(tool_calls)
            if overrides:
                report += "Tool overrides (replay.yaml):\n" + "".join(
                    f"  {o['tool_name']}  allow={'true' if o['decision'] == 'allow' else 'false'}"
                    f"  honoured={'yes' if o['honoured'] else 'no'}  ({o['reason']})\n"
                    for o in overrides
                )
            message = refusal_message(manifest, "mocked")
            if message is None:
                report = _invocation_report(require_reexecution_plan(manifest, "mocked")) + report
            unenforceable = evaluator.unenforceable_overrides(tool_calls)
            surface_error = ModelSurfaceNotServableError(not_servable) if not_servable else None
            tool_error = ToolSurfaceNotServedError(not_served) if not_served else None
            if message is not None:
                refusal = {"type": CapsuleNotReplayableError.error_type, "message": message}
                report = f"REFUSED: {message}\n\n{report}"
            elif surface_error is not None and not self._flags.permissive:
                refusal = surface_error.as_error()
                report = f"REFUSED: {surface_error}\n\n{report}"
            elif tool_error is not None and not self._flags.permissive:
                refusal = tool_error.as_error()
                report = f"REFUSED: {tool_error}\n\n{report}"
            elif unenforceable:
                not_enforced = ReplayOverrideUnenforceableError(unenforceable)
                if self._flags.permissive:
                    report = (
                        f"WARNING ({not_enforced.kind}, --permissive): {not_enforced}\n\n{report}"
                    )
                else:
                    refusal = not_enforced.as_error()
                    report = f"REFUSED: {not_enforced}\n\n{report}"
            if surface_error is not None and self._flags.permissive and message is None:
                report = (
                    f"WARNING ({surface_error.kind}, --permissive): {surface_error}\n\n{report}"
                )
            if tool_error is not None and self._flags.permissive and message is None:
                report = f"WARNING ({tool_error.kind}, --permissive): {tool_error}\n\n{report}"
            if self._flags.hermetic:
                # ADR-0322 D7: the dry run runs the same preflight as the real run.
                try:
                    sandbox = select_sandbox(self._flags.hermetic_backend)
                    check_writable(
                        [path for _role, path in self._hermetic_writable(result_dir)],
                        runtime_dir=os.environ.get("XDG_RUNTIME_DIR"),
                    )
                except HermeticUnavailableError as exc:
                    if refusal is None:
                        refusal = exc.as_error()
                        report = f"REFUSED: {exc}\n\n{report}"
                else:
                    report += (
                        f"Hermetic (ADR-0322): {sandbox.label()} preflight passed; the "
                        "replayed process tree would run with no network beyond its own "
                        "loopback (none at all under landlock) and a read-only filesystem "
                        f"except the working directory ({Path.cwd()}) and the replay result "
                        "directory.\n"
                    )
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
            model_calls_not_servable=(
                sum(int(v.get("count", 0)) for v in not_servable.values())
                if self._flags.mode == "mocked" else None
            ),
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
            similarity_score: float | None = round(sum(pairs) / len(pairs), 4)
            no_score_reason = None
        else:
            # A score needs two responses to compare. 1.0 here was a perfect
            # score built from no data (review replay-lineage-registry-09).
            similarity_score = None
            no_score_reason = (
                f"fewer than 2 model responses with text ({len(texts)}); "
                "there is nothing to compare, so no similarity score"
            )

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
            matched_run_id=run_id if similarity_score is not None else None,
            divergence_reason=no_score_reason,
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
            # hash_count is the number of requests that carry inputs to hash; a
            # digest computed and thrown away verified nothing, so none is kept.
            if any(k.startswith("gen_ai.request.") for k in mc):
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
            # Ineligible is a verdict, not a success: it was a green tick and
            # exit 0 for a capsule that cannot be replayed exactly (review
            # replay-lineage-registry-09).
            status="success" if eligible else "failure",
            exit_code=None if eligible else 1,
            divergence_reason=None if eligible else "; ".join(reasons),
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
        *,
        not_servable: dict[str, dict[str, Any]] | None = None,
    ) -> ReplayResult:
        start = _now()
        t0 = time.monotonic()
        replay_id = new_ulid()
        result_dir = self._base_dir / replay_id
        not_servable = not_servable or {}

        try:
            plan = require_reexecution_plan(manifest, "mocked")
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
                fidelity=fidelity_not_run(
                    "the replay was refused before the command ran"
                ).as_dict(),
            )
            write_replay_result(refused, result_dir)
            return refused

        # ADR-0319: a recorded model call replay cannot serve would run live
        # under a "mocked" label -- a strict replay refuses to start; under
        # --permissive it runs and is reported (below).
        if not_servable and not self._flags.permissive:
            surface_error = ModelSurfaceNotServableError(not_servable)
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
                model_calls_not_servable=surface_error.count,
                tool_calls_recorded=len(tool_calls),
                schema_drift=schema_drift or None,
                error=surface_error.as_error(),
            )
            write_replay_result(refused, result_dir)
            return refused

        # ADR-0306 r3 amendment: recorded observed tool calls (LangChain tools
        # not passed through wrap_tools) whose results cannot be served -- a
        # strict replay refuses to start, as for an unservable model surface
        # (ADR-0319); under --permissive it runs, and each such call is refused
        # unless --allow-unknown-mutation lets it run live.
        not_served = not_served_tool_calls(tool_calls, evaluator.coverage(tool_calls))
        if not_served and not self._flags.permissive:
            tool_error = ToolSurfaceNotServedError(not_served)
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
                error=tool_error.as_error(),
                fidelity=fidelity_not_run(
                    "the replay was refused before the command ran"
                ).as_dict(),
            )
            write_replay_result(refused, result_dir)
            return refused

        # ADR-0306 D8 / owner Q5: an `allow: false` override on a transport
        # replay cannot intercept could not be enforced -- a strict replay
        # refuses to start rather than break that promise; --permissive starts
        # and reports it (below).
        unenforceable = evaluator.unenforceable_overrides(tool_calls)
        if unenforceable and not self._flags.permissive:
            unenforceable_error = ReplayOverrideUnenforceableError(unenforceable)
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
                error=unenforceable_error.as_error(),
                fidelity=fidelity_not_run(
                    "the replay was refused before the command ran"
                ).as_dict(),
            )
            write_replay_result(refused, result_dir)
            return refused

        # ADR-0322: --hermetic runs only inside a sandbox that the preflight
        # proved can be built here; otherwise it is refused, never run un-isolated.
        sandbox: Sandbox | None = None
        writable = self._hermetic_writable(
            result_dir, plan.cwd if plan.source == "process_invocation" else None
        )
        if self._flags.hermetic:
            try:
                sandbox = select_sandbox(self._flags.hermetic_backend)
                check_writable(
                    [path for _role, path in writable],
                    runtime_dir=os.environ.get("XDG_RUNTIME_DIR"),
                )
            except HermeticUnavailableError as exc:
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
                    fidelity=fidelity_not_run(
                        "the replay was refused before the command ran"
                    ).as_dict(),
                )
                write_replay_result(refused, result_dir)
                return refused

        policy = self._flags.divergence_policy
        exit_code, run_error, events, outputs = self._run_replay_subprocess(
            plan.command, model_calls, tool_calls, divergence_policy=policy,
            tool_policy=evaluator.tool_policy_table(),
            install_required=evaluator.has_deny_override(),
            sandbox=sandbox,
            writable=[path for _role, path in writable],
            plan=plan,
        )
        report = summarize(
            model_calls, tool_calls, events,
            divergence_policy=policy, substitute_tools=True,
            adapter_runs_expected=plan.adapter_runs_expected,
        )
        if unenforceable:  # --permissive: a warning on the result, not a failure
            message = ReplayOverrideUnenforceableError.describe(unenforceable)
            report.divergences[:0] = [
                {
                    "kind": ReplayOverrideUnenforceableError.kind,
                    "message": message,
                    "tool_name": u["tool_name"],
                    "transports": u["transports"],
                }
                for u in unenforceable
            ]
        report.model_calls_not_servable_by_system = not_servable
        if not_servable:  # --permissive: a warning on the result, not a failure
            report.divergences[:0] = [{
                "kind": ModelSurfaceNotServableError.kind,
                "message": (
                    "recorded model calls mocked replay cannot serve ran live if the "
                    f"workload made them ({describe_by_system(not_servable)})"
                ),
                "by_system": {k: dict(v) for k, v in not_servable.items()},
            }]
        report.tool_calls_not_served_by_surface = not_served
        if not_served:  # --permissive: a warning on the result, not a failure
            report.divergences[:0] = [{
                "kind": ToolSurfaceNotServedError.kind,
                "message": (
                    "recorded tool calls mocked replay cannot serve were refused, or run "
                    "live under --allow-unknown-mutation and counted in tool_calls_live, "
                    f"if the workload made them ({describe_not_served(not_served)})"
                ),
                "by_surface": {k: dict(v) for k, v in not_served.items()},
            }]
        report.tool_overrides = evaluator.override_report(tool_calls)
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

        # ADR-0321: the replay capsule `nova diff` compares with the original,
        # then the outcome-equivalence verdict over it.
        end = _now()
        stdout, stderr = outputs if outputs is not None else (None, None)
        stored = write_mocked_replay_capsule(
            result_dir=result_dir,
            replay_id=replay_id,
            source_run_id=run_id,
            source_manifest=manifest,
            model_calls=model_calls,
            tool_calls=tool_calls,
            events=events,
            exit_code=exit_code,
            status=status,
            stdout=stdout,
            stderr=stderr,
            created_at=start,
            finished_at=end,
        )
        fidelity = compute_fidelity(
            capsule_dir=self._capsule_dir,
            manifest=manifest,
            model_calls=model_calls,
            tool_calls=tool_calls,
            events=events,
            report=report,
            exit_code=exit_code,
            run_error=run_error,
            stdout=stdout,
            stderr=stderr,
            replay_capsule=result_dir if stored else None,
            # The replayed command ran in this process's working directory.
            cwd=Path.cwd(),
            home=Path.home(),
        )

        result = ReplayResult(
            replay_id=replay_id,
            replay_of_run_id=run_id,
            mode="mocked",
            status=status,
            start_time=start,
            end_time=end,
            duration_ms=int((time.monotonic() - t0) * 1000),
            policy_flags_used=self._flags.active_flag_names(),
            env_warnings=[w.as_dict() for w in env_warnings],
            exit_code=exit_code,
            schema_drift=schema_drift or None,
            fidelity=fidelity.as_dict(),
            replay_capsule_ref=REPLAY_CAPSULE_FILE,
            hermetic=sandbox.describe(writable) if sandbox is not None else None,
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
        plan: ReexecutionPlan | None = None,
    ) -> tuple[int, dict[str, Any] | None]:
        """Run the replayed command; return its exit code and why it stopped.

        The second element is ``None`` for an ordinary exit (the code says it
        all) and a populated error for a timeout or a launch failure, where the
        exit code alone would misdescribe what happened. The dispatchers' event
        log is discarded; ``_run_replay_subprocess`` returns it.
        """
        code, error, _events, _outputs = self._run_replay_subprocess(
            command, model_calls, tool_calls, divergence_policy=divergence_policy,
            plan=plan,
        )
        return code, error

    def _run_replay_subprocess(
        self,
        command: list[str],
        model_calls: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]] | None = None,
        *,
        divergence_policy: str = "warn",
        tool_policy: dict[str, Any] | None = None,
        install_required: bool = False,
        sandbox: Sandbox | None = None,
        writable: list[Path] | None = None,
        plan: ReexecutionPlan | None = None,
    ) -> tuple[
        int, dict[str, Any] | None, list[dict[str, Any]], tuple[bytes, bytes] | None
    ]:
        """As ``_run_mocked_subprocess``, plus the dispatchers' event log and the
        process's captured ``(stdout, stderr)`` (``None`` if it did not finish).

        ``tool_calls=None`` installs no tool dispatcher (tools run live);
        a list -- even an empty one -- installs ``MockToolDispatcher`` on
        ``mcp.ClientSession.call_tool`` (ADR-0300). ``tool_policy`` is the
        resolved ``replay.yaml`` override table the dispatcher enforces
        (ADR-0306 D8); ``install_required`` stops the process if the
        dispatchers cannot be installed even under ``--permissive``.
        ``sandbox`` (ADR-0322) runs the command inside bubblewrap, where only
        ``writable`` (the working directory and the replay result directory) can be
        written; the temp dir holding the queues and the event log is then created
        inside the last of them, so it needs no bind of its own.
        A *plan*
        from an adapter capsule's recorded process invocation (ADR-0306 Q7) runs
        in the recorded working directory with stdin closed, and tells the
        process how many adapter runs the capsule recorded.
        """
        writable = list(writable or [])
        temp_parent = writable[-1] if sandbox is not None and writable else None
        if temp_parent is not None:
            temp_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="nf_replay_", dir=temp_parent) as tmp:
            site_dir = Path(tmp) / "site"
            site_dir.mkdir()
            write_replay_site(site_dir)

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
            # ADR-0319 (#52 part 2): every Python process of the replayed tree
            # reports in to the same event log; the one started here is `root`.
            env[OBSERVE_ENV] = str(events_path)
            env[SITE_ENV] = str(site_dir)
            env[ROLE_ENV] = "root"
            # ADR-0306 D7: the mutation classes the operator's ladder flags
            # permit -- an unmatched `record.tool` call may run live under
            # --permissive only for these. From the command line, never the capsule.
            flags = getattr(self, "_flags", None) or ReplayFlags()
            env[TOOL_LADDER_ENV] = ",".join(c for c in MUTATION_CLASSES if flags.permits(c))
            env.pop("NOVAFABRIC_REPLAY_TOOL_QUEUE_PATH", None)
            env.pop(TOOL_POLICY_ENV, None)
            env.pop(INSTALL_REQUIRED_ENV, None)
            env.pop(ADAPTER_RUNS_ENV, None)
            if plan is not None and plan.adapter_runs_expected is not None:
                env[ADAPTER_RUNS_ENV] = str(plan.adapter_runs_expected)
            if tool_calls is not None:
                tool_queue_path = Path(tmp) / "tool_queue.json"
                tool_queue_path.write_text(json.dumps(tool_calls))
                env["NOVAFABRIC_REPLAY_TOOL_QUEUE_PATH"] = str(tool_queue_path)
                if tool_policy is not None:
                    policy_path = Path(tmp) / "tool_policy.json"
                    policy_path.write_text(json.dumps(tool_policy))
                    env[TOOL_POLICY_ENV] = str(policy_path)
                if install_required:
                    env[INSTALL_REQUIRED_ENV] = "1"
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = f"{site_dir}:{existing}" if existing else str(site_dir)

            spawn_cwd = (
                plan.cwd if plan is not None and plan.source == "process_invocation" else None
            )
            if sandbox is not None:
                env = sandbox.environment(env, Path(tmp))
                command = sandbox.wrap(
                    command, writable=writable or [Path(tmp)], cwd=spawn_cwd or Path.cwd()
                )
            if spawn_cwd is not None:
                code, error, outputs = self._spawn(
                    command, env, cwd=spawn_cwd, stdin_devnull=True
                )
            else:
                code, error, outputs = self._spawn(command, env)
            return code, error, read_events(events_path), outputs

    @staticmethod
    def _hermetic_writable(
        result_dir: Path, cwd: Path | None = None
    ) -> list[tuple[str, Path]]:
        """ADR-0322 (owner, 2026-11-20): the directories ``--hermetic`` makes
        writable -- the replayed process's working directory (its outputs; an
        adapter capsule's recorded one, ADR-0306 Q7) and the replay result
        directory; the rest of the host stays read-only."""
        return [
            ("working_directory", (cwd or Path.cwd()).resolve()),
            ("replay_result_dir", result_dir.resolve()),
        ]

    @staticmethod
    def _spawn(
        command: list[str],
        env: dict[str, str],
        *,
        cwd: Path | None = None,
        stdin_devnull: bool = False,
    ) -> tuple[int, dict[str, Any] | None, tuple[bytes, bytes] | None]:
        """Run *command*; return its exit code, why it stopped, and its output.

        The output -- ``(stdout, stderr)`` -- is captured as it always was (the
        replayed process never wrote to the terminal) and is ``None`` when the
        process did not run to completion (ADR-0321 compares it). *cwd* and
        *stdin_devnull* are passed only when set (an adapter capsule's recorded
        invocation, ADR-0306 Q7); a ``nova capture`` capsule's spawn is unchanged.
        """
        extra: dict[str, Any] = {}
        if cwd is not None:
            extra["cwd"] = cwd
        if stdin_devnull:
            extra["stdin"] = subprocess.DEVNULL
        try:
            proc = subprocess.run(
                command, env=env, capture_output=True,
                timeout=REPLAY_SUBPROCESS_TIMEOUT_S, **extra,
            )
            stdout = getattr(proc, "stdout", None)
            stderr = getattr(proc, "stderr", None)
            return proc.returncode, None, (
                stdout if isinstance(stdout, bytes) else b"",
                stderr if isinstance(stderr, bytes) else b"",
            )
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
            }, None
        except Exception as exc:
            # The command may never have launched. "exited with code 1" would
            # assert an exit that never happened.
            return 1, {
                "type": "ReplayLaunchError",
                "message": f"the replayed command could not be run: {exc}",
            }, None
