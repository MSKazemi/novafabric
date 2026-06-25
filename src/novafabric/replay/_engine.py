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

from novafabric.audit import AUDIT_LOG_PATH, AuditEventType, AuditLog
from novafabric.capture._ulid import new_ulid
from novafabric.policy import (
    PolicyDeniedError,
    PolicyInput,
    PolicyResource,
    PolicySubject,
    get_policy_engine,
)
from novafabric.replay._env_check import EnvironmentResolver
from novafabric.replay._flags import ReplayFlags
from novafabric.replay._policy import PolicyEvaluator
from novafabric.replay._result import ReplayResult, write_replay_result

_MOCK_HOOK_LOADER = textwrap.dedent("""\
    import os as _os, sys as _sys, json as _json
    _queue_path = _os.environ.get("NOVAFABRIC_REPLAY_QUEUE_PATH", "")
    if _queue_path:
        try:
            from pathlib import Path as _P
            _queue = _json.loads(_P(_queue_path).read_text())
            from novafabric.replay._dispatcher import MockModelDispatcher as _MMD
            _dispatcher = _MMD(model_calls=_queue)
            _dispatcher.install()
        except Exception as _e:
            print(f"[novafabric] mock dispatcher install failed: {_e}", file=_sys.stderr)
""")


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
        replay_policy = _load_replay_policy(self._capsule_dir)
        env_lock = _load_env_lock(self._capsule_dir)
        model_calls = _read_jsonl(self._capsule_dir / "model-calls.jsonl")
        tool_calls = _read_jsonl(self._capsule_dir / "tool-calls.jsonl")

        env_warnings = EnvironmentResolver().check(env_lock)
        run_id = manifest.get("run_id", "unknown")
        evaluator = PolicyEvaluator(replay_policy, self._flags)

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
                ),
            )
            decision = engine.evaluate(inp)
            AuditLog(AUDIT_LOG_PATH).append(
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
            return self._dry_run(run_id, evaluator, tool_calls, env_warnings)

        if self._flags.mode == "forensic":
            return self._forensic(run_id, manifest, model_calls, tool_calls, env_warnings)

        if self._flags.mode == "semantic":
            return self._semantic(run_id, model_calls, env_warnings)

        if self._flags.mode == "exact":
            return self._exact(run_id, env_lock, model_calls, env_warnings)

        if self._flags.mode == "intervention":
            return self._intervention(
                manifest, run_id, model_calls, tool_calls, env_warnings
            )

        return self._mocked(
            manifest, run_id, model_calls, tool_calls, evaluator, env_warnings
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

        command: list[str] = manifest.get("command", [])
        exit_code: int | None = None
        if command:
            exit_code = self._run_mocked_subprocess(command, mutated_model_calls)
            status = "success" if exit_code == 0 else "failure"
        else:
            # no re-executable command — emit the mutated streams only
            status = "success"

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
            tool_calls_mocked=len(mutated_tool_calls),
            exit_code=exit_code,
            intervention=intervention_meta,
        )
        if status == "failure":
            result.error = {
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
            tool_calls_mocked=len(tool_calls),
        )
        write_replay_result(result, result_dir)
        return result

    def _dry_run(
        self,
        run_id: str,
        evaluator: PolicyEvaluator,
        tool_calls: list[dict[str, Any]],
        env_warnings: list[Any],
    ) -> ReplayResult:
        start = _now()
        t0 = time.monotonic()
        replay_id = new_ulid()
        result_dir = self._base_dir / replay_id

        report = evaluator.dry_run_report(tool_calls)

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
            tool_calls_mocked=len(tool_calls),
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
        )
        write_replay_result(result, result_dir)
        return result

    def _exact(
        self,
        run_id: str,
        env_lock: dict[str, Any],
        model_calls: list[dict[str, Any]],
        env_warnings: list[Any],
    ) -> ReplayResult:
        start = _now()
        t0 = time.monotonic()
        replay_id = new_ulid()
        result_dir = self._base_dir / replay_id

        reasons: list[str] = []
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
    ) -> ReplayResult:
        start = _now()
        t0 = time.monotonic()
        replay_id = new_ulid()
        result_dir = self._base_dir / replay_id

        command: list[str] = manifest.get("command", [])
        if not command:
            return ReplayResult(
                replay_id=replay_id,
                replay_of_run_id=run_id,
                mode="mocked",
                status="aborted",
                start_time=start,
                end_time=_now(),
                duration_ms=int((time.monotonic() - t0) * 1000),
                policy_flags_used=self._flags.active_flag_names(),
                env_warnings=[w.as_dict() for w in env_warnings],
                error={"type": "MissingCommand", "message": "capsule.yaml has no command field"},
            )

        exit_code = self._run_mocked_subprocess(command, model_calls)
        status = "success" if exit_code == 0 else "failure"

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
            model_calls_mocked=len(model_calls),
            tool_calls_mocked=len(tool_calls),
            exit_code=exit_code,
        )
        if status == "failure":
            result.error = {
                "type": "NonZeroExit",
                "message": f"Replayed command exited with code {exit_code}",
            }
        write_replay_result(result, result_dir)
        return result

    def _run_mocked_subprocess(
        self, command: list[str], model_calls: list[dict[str, Any]]
    ) -> int:
        with tempfile.TemporaryDirectory(prefix="nf_replay_") as tmp:
            site_dir = Path(tmp) / "site"
            site_dir.mkdir()
            (site_dir / "sitecustomize.py").write_text(_MOCK_HOOK_LOADER)

            queue_path = Path(tmp) / "model_queue.json"
            queue_path.write_text(json.dumps(model_calls))

            env = dict(os.environ)
            env["NOVAFABRIC_REPLAY_QUEUE_PATH"] = str(queue_path)
            env["NOVAFABRIC_REPLAY_MODE"] = "mocked"
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = f"{site_dir}:{existing}" if existing else str(site_dir)

            try:
                proc = subprocess.run(
                    command, env=env, capture_output=True, timeout=600
                )
                return proc.returncode
            except subprocess.TimeoutExpired:
                return 124
            except Exception:
                return 1
