"""EvidenceLab D1 harness: one real ``nova capture`` of the incident agent.

    python examples/evidence_lab/run.py --scenario service-incident-v1 --output-dir PATH

What it does, in order, all bounded and cleaned up on every exit path:

1. starts the strict loopback mock model (``mock_llm_server.py``) on an
   OS-chosen ephemeral port;
2. runs the actual ``nova capture`` CLI around ``agent.py`` with an environment
   built from scratch (temp ``HOME``/``XDG_*``/``NOVAFABRIC_HOME``, the dummy API
   key, no proxies, no provider credentials, no widened capture level);
3. takes the run id from the CLI's own output, runs ``nova validate``, and checks
   the capsule's ``model-calls.jsonl`` / ``tool-calls.jsonl`` against the
   scenario contract — nothing is injected into the capsule;
4. writes ``evidence-lab-report.json`` (a harness-local diagnostic sidecar, not a
   NovaFabric format) and prints two *separate* verdicts: the scenario's task
   verdict (this scenario is deliberately unsafe, so ``FAIL`` is the expected
   outcome) and the harness verdict (did every D1 observation hold).

Exit codes: 0 harness PASS · 2 a named failure (``code=E_*`` on stderr, also in the
report) · 77 skipped because the optional ``openai`` SDK is not installed.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import importlib.util
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# The harness must leave nothing beside the scripts: no __pycache__ either. (Children
# get PYTHONDONTWRITEBYTECODE from build_child_env; this covers this process.)
sys.dont_write_bytecode = True

import evidence_scenario as lab  # noqa: E402

HERE = Path(__file__).resolve().parent
REPORT_SCHEMA_REVISION = 1
REPORT_NAME = "evidence-lab-report.json"
EXIT_OK = 0
EXIT_FAILED = 2
EXIT_SKIPPED = 77
MAX_TIMEOUT_SECONDS = 600
# `nova capture` / `nova validate` print a few hundred bytes; a command that prints
# more than this is runaway, and is stopped instead of buffered until the timeout.
MAX_CAPTURE_OUTPUT_BYTES = 1024 * 1024

_RUN_ID = re.compile(r"run_id=([0-9A-HJKMNP-TV-Z]{26})")
_ERROR_CODE = re.compile(r"code=(E_[A-Z_]+)")
_PASSTHROUGH_ENV = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")
_MOCK_START_SECONDS = 10.0
_VALIDATE_SECONDS = 120.0
_TAIL = 400


# ── signals ──────────────────────────────────────────────────────────────────


class _Interrupted(BaseException):
    """Raised by the signal handler so ``finally`` blocks run (BaseException: not swallowed)."""


_HANDLED_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


def _on_signal(signum: int, _frame: object) -> None:
    raise _Interrupted(signal.Signals(signum).name)


def _install_signal_handlers() -> dict[int, Any] | None:
    """Turn termination signals into cleanup: children run in their own sessions, so
    without this a killed harness would leave the capture, the mock and the temp dir."""
    if threading.current_thread() is not threading.main_thread():
        return None
    return {sig: signal.signal(sig, _on_signal) for sig in _HANDLED_SIGNALS}


def _set_signal_handlers(handlers: Mapping[int, Any] | None, *, ignore: bool = False) -> None:
    if handlers is None:
        return
    for sig in _HANDLED_SIGNALS:
        signal.signal(sig, signal.SIG_IGN if ignore else handlers[sig])


# ── environment ──────────────────────────────────────────────────────────────


def build_child_env(parent: Mapping[str, str], *, home: Path, base_url: str) -> dict[str, str]:
    """An environment built from scratch: nothing is inherited except a short allowlist.

    The real user's HOME, XDG dirs, NovaFabric state, API keys, proxies and capture
    level never reach the child — not filtered out of a copy, simply never copied.
    """
    env = {k: parent[k] for k in _PASSTHROUGH_ENV if k in parent}
    env.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
            "NOVAFABRIC_HOME": str(home / ".novafabric"),
            "NOVAFABRIC_SUGGEST": "0",
            "PYTHONDONTWRITEBYTECODE": "1",  # never write __pycache__ into the checkout
            "NO_COLOR": "1",
            "OPENAI_BASE_URL": base_url,
            "OPENAI_API_KEY": lab.DUMMY_API_KEY,
        }
    )
    lab.validate_environment(env)
    return env


# ── process handling ─────────────────────────────────────────────────────────


def _kill_group(proc: subprocess.Popen[Any], *, grace: float = 3.0) -> None:
    """Stop ``proc`` and every process it started (it leads its own session)."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)
        try:
            proc.wait(timeout=grace if sig == signal.SIGTERM else 5.0)
        except subprocess.TimeoutExpired:
            continue
        if sig == signal.SIGTERM:
            # The leader exited; make sure no straggler in its group outlives it.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
        return


def _run_bounded(
    argv: list[str],
    *,
    env: Mapping[str, str],
    cwd: Path,
    timeout: float,
    save_to: Path | None = None,
) -> tuple[int, str]:
    """Run ``argv`` in its own session; kill the whole group on timeout or exit."""
    try:
        proc = subprocess.Popen(
            argv,
            env=dict(env),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError) as exc:
        raise lab.EvidenceLabError(
            "E_NOVA_NOT_FOUND", f"cannot execute {argv[0]!r}: {exc}"
        ) from exc
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    cap = MAX_CAPTURE_OUTPUT_BYTES
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    size = 0
    failure: lab.EvidenceLabError | None = None
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = lab.EvidenceLabError(
                    "E_CAPTURE_TIMEOUT",
                    f"{Path(argv[0]).name} did not finish within {timeout:g}s",
                )
                break
            ready, _, _ = select.select([fd], [], [], min(remaining, 0.5))
            if not ready:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:  # EOF: every holder of the pipe has closed it
                break
            size += len(chunk)
            chunks.append(chunk[: max(0, cap - (size - len(chunk)))])
            if size > cap:
                failure = lab.EvidenceLabError(
                    "E_CAPTURE_OUTPUT_TOO_LARGE",
                    f"{Path(argv[0]).name} printed more than {cap} bytes; stopped",
                )
                break
        if failure is None:
            try:
                proc.wait(timeout=max(1.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                failure = lab.EvidenceLabError(
                    "E_CAPTURE_TIMEOUT",
                    f"{Path(argv[0]).name} did not finish within {timeout:g}s",
                )
        if failure is not None:
            _kill_group(proc)
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)  # stragglers of a command that did exit
        proc.stdout.close()
    output = b"".join(chunks).decode("utf-8", errors="replace")
    if save_to is not None:  # kept even on failure: it is evidence of what went wrong
        save_to.write_text(output, encoding="utf-8")
    if failure is not None:
        raise failure
    return proc.returncode, output


def _start_mock(
    scn: lab.Scenario, *, port: int, out_dir: Path, work: Path, env: Mapping[str, str]
) -> tuple[subprocess.Popen[Any], dict[str, Any], Path]:
    ready = work / "mock-ready.json"
    log = out_dir / "mock-requests.jsonl"
    stderr_path = work / "mock-stderr.log"
    argv = [
        sys.executable,
        str(HERE / "mock_llm_server.py"),
        "--scenario-dir", str(scn.directory),
        "--port", str(port),
        "--ready-file", str(ready),
        "--request-log", str(log),
        "--max-lifetime", str(scn.manifest.limits.timeout_seconds + 120),
    ]  # fmt: skip
    with open(stderr_path, "wb") as stderr:
        proc = subprocess.Popen(
            argv,
            env=dict(env),
            cwd=work,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            start_new_session=True,
        )
    return proc, {"ready": ready, "stderr": stderr_path}, log


def _await_mock(proc: subprocess.Popen[Any], files: Mapping[str, Path]) -> dict[str, Any]:
    deadline = time.monotonic() + _MOCK_START_SECONDS
    while time.monotonic() < deadline:
        if files["ready"].exists():
            info: dict[str, Any] = json.loads(files["ready"].read_text())
            return info
        if proc.poll() is not None:
            text = files["stderr"].read_text(errors="replace")
            match = _ERROR_CODE.search(text)
            code = match.group(1) if match else "E_MOCK_START_FAILED"
            raise lab.EvidenceLabError(
                code, f"the mock model server exited: {text.strip()[-_TAIL:]}"
            )
        time.sleep(0.05)
    raise lab.EvidenceLabError("E_MOCK_START_FAILED", "the mock model server never became ready")


# ── evidence ─────────────────────────────────────────────────────────────────


def _jsonl(path: Path, *, code: str = "E_CAPSULE_INVALID") -> list[dict[str, Any]]:
    """Every line of ``path`` as a JSON object, or a named failure — never a traceback.

    A truncated last line is a real possibility: NovaFabric's recorder is fail-open.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        records = [json.loads(line) for line in lines if line.strip()]
    except OSError as exc:
        raise lab.EvidenceLabError(code, f"cannot read {path.name}: {exc.strerror}") from exc
    except (ValueError, RecursionError) as exc:  # JSONDecodeError and UnicodeDecodeError
        raise lab.EvidenceLabError(code, f"{path.name} is not valid JSON Lines: {exc}") from exc
    if not all(isinstance(r, dict) for r in records):
        raise lab.EvidenceLabError(code, f"{path.name} has a line that is not a JSON object")
    return records


_ROLE_EXT = "io.novafabric.record_role"
_LINK_EXT = "io.novafabric.logical_call_id"


def _check_model_records(
    scn: lab.Scenario, records: list[dict[str, Any]], expected_body: dict[str, Any], base_url: str
) -> tuple[dict[str, Any], int]:
    """Judge ``model-calls.jsonl``; return the one logical record and the transport count.

    ADR-0305: a record without the role marker IS a logical call, so only an explicit
    ``transport`` role is excluded from the count. Each transport record must belong to
    the logical call and name the same endpoint.
    """
    manifest = scn.manifest
    transport = [r for r in records if r.get("extensions", {}).get(_ROLE_EXT) == "transport"]
    logical = [r for r in records if r not in transport]
    if len(logical) != 1:
        raise lab.EvidenceLabError(
            "E_COUNT_MISMATCH", f"expected 1 logical model call, the capsule has {len(logical)}"
        )
    model = logical[0]
    mismatch = lab.EvidenceLabError
    if (
        model.get("gen_ai.request.model") != manifest.model_id
        or model.get("gen_ai.system") != "openai"
        or model.get("status") != "success"
        or str(model.get("endpoint", "")).rstrip("/") != base_url
    ):
        raise mismatch(
            "E_MODEL_RECORD_MISMATCH",
            "the logical model record is not the call to this run's loopback fixture",
        )
    for record in transport:
        ext = record.get("extensions", {})
        endpoint = record.get("endpoint")
        if ext.get(_LINK_EXT) != model.get("model_call_id") or (
            endpoint is not None and str(endpoint).rstrip("/") != base_url
        ):
            raise mismatch(
                "E_MODEL_RECORD_MISMATCH", "a transport record is not part of the logical call"
            )
    # The default capture level keeps the request and response text, so the record can be
    # compared with the golden transcript and the fixture decision, not just counted.
    try:
        messages = model["gen_ai.request.messages"]
        choices = model["gen_ai.response.choices"]
        content = json.loads(choices[0]["message"]["content"])
        finish = choices[0]["finish_reason"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise mismatch(
            "E_MODEL_RECORD_MISMATCH", "the logical model record has no readable request/response"
        ) from exc
    if (
        messages != expected_body["messages"]
        or len(choices) != 1
        or content != scn.model_response
        or finish != "stop"
    ):
        raise mismatch(
            "E_MODEL_RECORD_MISMATCH",
            "the recorded model request or response differs from the golden transcript",
        )
    return model, len(transport)


def _evaluate(
    scn: lab.Scenario,
    capsule: Path,
    agent_result: dict[str, Any],
    mock_log: Path,
    base_url: str,
) -> tuple[dict[str, Any], dict[str, bool]]:
    """Compare the capsule and the agent's result with the scenario's own contract."""
    manifest = scn.manifest
    expected_obs = lab.expected_observations(scn)
    expected_body = lab.build_request_body(scn, expected_obs)
    # The committed golden transcript is read here, so the check named after it compares
    # with the file, not only with what this code builds.
    if lab.load_golden_request(scn) != expected_body:
        raise lab.EvidenceLabError(
            "E_MODEL_RECORD_MISMATCH",
            f"{lab.GOLDEN_REQUEST} differs from the request this scenario builds",
        )
    request_sha = lab.sha256_hex(lab.canonical_json(expected_body))

    model_records = _jsonl(capsule / "model-calls.jsonl")
    tools = _jsonl(capsule / "tool-calls.jsonl")
    tool_names = [t.get("tool_name") for t in tools]
    expected_order = list(manifest.tools.expected_order)

    _, transport_count = _check_model_records(scn, model_records, expected_body, base_url)
    if tool_names != expected_order:
        raise lab.EvidenceLabError(
            "E_TOOL_ORDER_MISMATCH",
            f"expected tool calls {expected_order}, the capsule has {tool_names}",
        )
    for record in tools:
        if (
            record.get("transport") != "python"
            or record.get("mutation_class") != "read-only"
            or record.get("mutates") is not False
            or record.get("tool_version") != "1"
            or record.get("status") != "success"
        ):
            raise lab.EvidenceLabError(
                "E_TOOL_RECORD_MISMATCH",
                f"tool record {record.get('tool_name')!r} is not a read-only success",
            )
        if record.get("arguments") not in ({}, None) or record.get("result") is not None:
            raise lab.EvidenceLabError(
                "E_PRIVACY_VIOLATION",
                f"tool record {record.get('tool_name')!r} holds a payload "
                "at the default capture level",
            )

    log = _jsonl(mock_log, code="E_MOCK_LOG_MISMATCH")
    if [e.get("outcome") for e in log] != ["accepted"]:
        raise lab.EvidenceLabError(
            "E_MOCK_LOG_MISMATCH",
            f"the mock served {[e.get('outcome') for e in log]}, expected one accepted",
        )

    expected_digests = {
        name: lab.sha256_hex(lab.canonical_json(value)) for name, value in expected_obs.items()
    }
    if (
        agent_result.get("observation_sha256") != expected_digests
        or agent_result.get("model", {}).get("request_sha256") != request_sha
        or log[0].get("request_sha256") != request_sha
    ):
        raise lab.EvidenceLabError(
            "E_OBSERVATION_MISMATCH",
            "tool observations or the model request differ from the fixtures",
        )
    # The capsule's own record of each tool result (a digest, at the default capture
    # level) must equal the digest of what the fixtures say that tool returns. The
    # expected value comes from the fixtures alone, not from NovaFabric or the agent.
    for record in tools:
        name = record["tool_name"]
        recorded = record.get("extensions", {}).get("io.novafabric.result_digest")
        if recorded != f"sha256:{expected_digests[name]}":
            raise lab.EvidenceLabError(
                "E_OBSERVATION_MISMATCH",
                f"the capsule's result digest for {name!r} differs from the fixture's",
            )

    try:
        decision = lab.Decision.model_validate(agent_result["decision"])
    except Exception as exc:  # a malformed result is a harness failure, not a crash
        raise lab.EvidenceLabError(
            "E_AGENT_RESULT_INVALID", "agent-result.json has no valid decision"
        ) from exc
    verdict = lab.evaluate_policy(scn, decision)
    decision_sha = lab.sha256_hex(lab.canonical_json(decision.model_dump()))
    if (
        agent_result.get("policy") != {"result": verdict.result, "rule": verdict.rule}
        or agent_result.get("decision_sha256") != decision_sha
    ):
        raise lab.EvidenceLabError(
            "E_AGENT_RESULT_INVALID", "agent policy verdict differs from the oracle"
        )
    if (
        decision.action != manifest.expected.unsafe_action
        or verdict.result != manifest.expected.policy_result
    ):
        raise lab.EvidenceLabError(
            "E_UNEXPECTED_OUTCOME",
            f"expected the unsafe action {manifest.expected.unsafe_action!r} to be rejected; "
            f"got {decision.action!r} -> {verdict.result}",
        )

    observed = {
        "logical_model_calls": 1,
        "transport_model_records": transport_count,
        "tool_calls": len(tools),
        "tool_sequence": tool_names,
        "mock_requests": {"accepted": 1, "rejected": 0},
    }
    canonical = {
        "fixture_sha256": dict(manifest.fixture_sha256),
        "fixture_manifest_sha256": scn.fixture_manifest_sha256,
        "request_sha256": request_sha,
        "tool_sequence_sha256": lab.sha256_hex(lab.canonical_json(tool_names)),
        "decision_sha256": decision_sha,
    }
    checks = {
        "one_logical_model_call": True,
        "two_read_only_tool_calls_in_order": True,
        "tool_records_digest_only": True,
        "model_record_is_loopback_fixture": True,
        "model_record_matches_golden_transcript": True,
        "mock_served_exactly_one_accepted_request": True,
        "observations_match_fixtures": True,
        "tool_result_digests_match_fixtures": True,
        "policy_oracle_rejected_unsafe_action": True,
    }
    return {
        "observed": observed,
        "canonical": canonical,
        "task": {
            "verdict": "PASS" if verdict.result == "accept" else "FAIL",
            "reason": f"policy oracle rejected {decision.action!r} ({verdict.rule})",
            "expected_unsafe_outcome": True,
        },
    }, checks


# ── orchestration ────────────────────────────────────────────────────────────


def _resolve_nova(explicit: str | None) -> str:
    if explicit:
        # The child runs from a temp work dir, so a relative path must be fixed now.
        return str(Path(explicit).resolve()) if os.sep in explicit else explicit
    sibling = Path(sys.executable).parent / "nova"
    if sibling.is_file():
        return str(sibling)
    found = shutil.which("nova")
    if found:
        return found
    raise lab.EvidenceLabError(
        "E_NOVA_NOT_FOUND", "the nova CLI is not installed next to this Python or on PATH"
    )


def _git_sha() -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(HERE), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None


def _version(dist: str) -> str | None:
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None


def _pipeline(
    args: argparse.Namespace, scn: lab.Scenario, out_dir: Path, work: Path, home: Path,
    report: dict[str, Any],
) -> None:  # fmt: skip
    manifest = scn.manifest
    parent_env = dict(os.environ)
    stage_env = build_child_env(parent_env, home=home, base_url="http://127.0.0.1:65535/v1")
    proc, files, mock_log = _start_mock(
        scn, port=args.mock_port, out_dir=out_dir, work=work, env=stage_env
    )
    report["cleanup"]["mock_pid"] = proc.pid
    report["_mock_proc"] = proc
    mock = _await_mock(proc, files)
    base_url = lab.validate_base_url(f"http://{mock['host']}:{mock['port']}/v1")
    env = build_child_env(parent_env, home=home, base_url=base_url)

    nova = _resolve_nova(args.nova_bin)
    capsules = out_dir / "capsules"
    result_path = out_dir / "agent-result.json"
    timeout = args.timeout_seconds or float(manifest.limits.timeout_seconds + 60)
    capture_argv = [
        nova, "capture", "--output-dir", str(capsules), "--",
        sys.executable, str(HERE / "agent.py"),
        "--scenario-dir", str(scn.directory), "--result-path", str(result_path),
    ]  # fmt: skip
    code, output = _run_bounded(
        capture_argv, env=env, cwd=work, timeout=timeout, save_to=out_dir / "capture-output.txt"
    )
    report["commands"]["capture"] = {"argv": capture_argv, "exit_code": code}
    if code != 0:
        agent_error = _agent_error(result_path)
        if agent_error is not None:
            raise lab.EvidenceLabError(*agent_error)
        raise lab.EvidenceLabError(
            "E_CAPTURE_FAILED", f"nova capture exited {code}: {output.strip()[-_TAIL:]}"
        )
    match = _RUN_ID.search(output)
    if match is None:
        raise lab.EvidenceLabError("E_RUN_ID_MISSING", "nova capture printed no run_id")
    run_id = match.group(1)
    capsule = capsules / run_id
    report["run_id"] = run_id
    if not capsule.is_dir():
        raise lab.EvidenceLabError("E_CAPSULE_MISSING", f"no capsule directory for run {run_id}")
    report["capsule_path"] = str(capsule)

    validate_argv = [nova, "validate", str(capsule)]
    v_code, v_output = _run_bounded(validate_argv, env=env, cwd=work, timeout=_VALIDATE_SECONDS)
    report["commands"]["validate"] = {"argv": validate_argv, "exit_code": v_code}
    if v_code != 0:
        raise lab.EvidenceLabError(
            "E_CAPSULE_INVALID", f"nova validate exited {v_code}: {v_output.strip()[-_TAIL:]}"
        )

    try:
        agent_result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise lab.EvidenceLabError(
            "E_AGENT_RESULT_MISSING", "the agent wrote no readable result"
        ) from exc
    if agent_result.get("status") != "completed":
        raise lab.EvidenceLabError("E_AGENT_RESULT_INVALID", "agent result is not 'completed'")
    evaluated, checks = _evaluate(scn, capsule, agent_result, mock_log, base_url)
    report.update(evaluated)
    report["checks"] = checks


def _agent_error(result_path: Path) -> tuple[str, str] | None:
    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
        error = data["error"]
        if data.get("status") == "error" and str(error["code"]).startswith("E_"):
            return str(error["code"]), str(error["message"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _write_report(out_dir: Path, report: dict[str, Any]) -> None:
    clean = {k: v for k, v in report.items() if not k.startswith("_")}
    (out_dir / REPORT_NAME).write_text(
        json.dumps(clean, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _fail(code: str, message: str) -> int:
    sys.stderr.write(f"evidence_lab: FAIL code={code} message={message}\n")
    return EXIT_FAILED


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="EvidenceLab D1: run one deterministic incident scenario under real capture"
    )
    parser.add_argument("--scenario", required=True, help="scenario id, e.g. service-incident-v1")
    parser.add_argument("--output-dir", required=True, type=Path, help="must not exist or be empty")
    parser.add_argument("--profile", choices=["zero-key"], default="zero-key")
    parser.add_argument("--scenarios-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--mock-port", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--nova-bin", help=argparse.SUPPRESS)
    parser.add_argument("--timeout-seconds", type=float, help="cap on `nova capture` wall time")
    args = parser.parse_args(argv)
    if args.timeout_seconds is not None and not 0 < args.timeout_seconds <= MAX_TIMEOUT_SECONDS:
        # `not (0 < t <= max)` also rejects nan, which every comparison treats as false.
        return _fail(
            "E_ARGUMENT_INVALID", f"--timeout-seconds must be in (0, {MAX_TIMEOUT_SECONDS}]"
        )

    if importlib.util.find_spec("openai") is None:
        print(
            "SKIPPED: the optional 'openai' SDK is not installed, so no model call can be made. "
            "Install it (pip install openai, or uv sync --all-extras); no evidence was produced."
        )
        return EXIT_SKIPPED
    try:
        root = args.scenarios_root.resolve() if args.scenarios_root else None
        scn = lab.load_scenario(lab.resolve_scenario_dir(args.scenario, root))
        # Absolute from here on: `nova capture` runs from a temp work dir, where a
        # relative --output-dir would land (and be deleted with it).
        out_dir: Path = args.output_dir.resolve()
        if out_dir.exists() and (not out_dir.is_dir() or any(out_dir.iterdir())):
            raise lab.EvidenceLabError(
                "E_OUTPUT_DIR_NOT_EMPTY", f"{out_dir} exists and is not an empty directory"
            )
        out_dir.mkdir(parents=True, exist_ok=True)
    except lab.EvidenceLabError as exc:
        return _fail(exc.code, exc.message)
    except OSError as exc:
        return _fail("E_OUTPUT_DIR_UNUSABLE", f"cannot use {args.output_dir}: {exc.strerror}")

    started = time.monotonic()
    home = Path(tempfile.mkdtemp(prefix="evidence-lab-home-"))
    work = home / "work"
    work.mkdir()
    report: dict[str, Any] = {
        "schema_revision": REPORT_SCHEMA_REVISION,
        "status": "error",
        "scenario_id": scn.manifest.scenario_id,
        "seed": scn.manifest.seed,
        "profile": args.profile,
        "novafabric_version": _version("novafabric"),
        "python_version": sys.version.split()[0],
        "openai_version": _version("openai"),
        "harness_git_sha": _git_sha(),
        "input": {
            "kind": "synthetic",
            "real_data": False,
            "note": "scenario fixtures written for this example; no real logs, metrics or prompts",
        },
        "capture": {
            "kind": "actual-nova-capture",
            "command": "nova capture",
            "novafabric_version": _version("novafabric"),
            "injected_records": False,
            "note": "the capsule is written by the real recorder; the harness only reads it",
        },
        "run_id": None,
        "capsule_path": None,
        "commands": {},
        "expected": {
            "logical_model_calls": 1,
            "tool_calls": len(scn.manifest.tools.expected_order),
            "tool_sequence": list(scn.manifest.tools.expected_order),
        },
        "checks": {},
        "error": None,
        "harness_verdict": "FAIL",
        "axes": {
            "integrity": {
                "verdict": "NOT_EVALUATED",
                "reason": "profile zero-key configures no signer; nothing was sealed or verified",
            },
            "replay": {
                "verdict": "NOT_EVALUATED",
                "reason": "replay fidelity is outside D1; no replay was run and none is claimed",
            },
        },
        "cleanup": {"temp_home": str(home), "mock_pid": None},
    }
    failure: lab.EvidenceLabError | None = None
    handlers = _install_signal_handlers()
    try:
        _pipeline(args, scn, out_dir, work, home, report)
        report["status"] = "ok"
        report["harness_verdict"] = "PASS"
    except lab.EvidenceLabError as exc:
        failure = exc
    except _Interrupted as exc:
        failure = lab.EvidenceLabError("E_INTERRUPTED", f"received {exc}; cleaned up and stopped")
    except Exception as exc:  # noqa: BLE001 - nothing may escape without cleanup and a report
        failure = lab.EvidenceLabError("E_HARNESS_INTERNAL", f"{type(exc).__name__}: {exc}")
    finally:
        _set_signal_handlers(handlers, ignore=True)  # cleanup must not be interrupted
        if failure is not None:
            report["error"] = {"code": failure.code, "message": failure.message}
        mock_proc = report.pop("_mock_proc", None)
        if mock_proc is not None:
            _kill_group(mock_proc)
            report["cleanup"]["mock_exit_code"] = mock_proc.returncode
        shutil.rmtree(home, ignore_errors=True)
        report["cleanup"]["temp_home_removed"] = not home.exists()
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["axes"]["task"] = report.get("task")
        _write_report(out_dir, report)
        _set_signal_handlers(handlers)

    if failure is not None:
        return _fail(failure.code, failure.message)
    observed, task = report["observed"], report["task"]
    exp = report["expected"]
    print(
        f"evidence-lab: scenario {report['scenario_id']} "
        f"(seed={report['seed']}, profile={args.profile})"
    )
    print("input: SYNTHETIC fixtures only (no real data)")
    print(
        f"capture: actual `nova capture` (NovaFabric {report['novafabric_version']}); "
        "no record was injected by the harness"
    )
    print(f"run_id: {report['run_id']}")
    print(f"capsule: {report['capsule_path']}")
    print(
        f"logical model calls: {observed['logical_model_calls']} "
        f"(expected {exp['logical_model_calls']})"
        f" | tool calls: {observed['tool_calls']} (expected {exp['tool_calls']}: "
        f"{' -> '.join(observed['tool_sequence'])})"
    )
    print(
        f"task verdict: {task['verdict']} - {task['reason']}; "
        "this is the scenario's expected unsafe outcome"
    )
    print(f"harness verdict: PASS - every D1 check held (report: {out_dir / REPORT_NAME})")
    print(
        "integrity: NOT_EVALUATED (no signer configured) | replay: NOT_EVALUATED (not part of D1)"
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
