"""EvidenceLab D1 agent: one deterministic incident investigation.

Fixed flow, single synchronous process, no agent framework:

1. ``read_metrics`` and ``search_logs`` — the two declared read-only tool
   boundaries (``@record.tool``); under ``nova capture`` each call is one real
   record in ``tool-calls.jsonl``.
2. One non-streaming ``openai.OpenAI(...).chat.completions.create`` request to the
   strict loopback mock — the one genuine, captured model SDK call.
3. A deterministic policy reviewer applies the fixed policy to the model's
   decision. The task verdict comes from that oracle, never from the model's own
   explanation.

Run it through the harness (``run.py``). Run directly, it needs
``OPENAI_BASE_URL`` (loopback) and the dummy ``OPENAI_API_KEY``; a real key, a
non-loopback URL, a proxy or a widened capture level is refused before any SDK
call.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import evidence_scenario as lab

from novafabric.capture import record

HERE = Path(__file__).resolve().parent
DEFAULT_SCENARIO_DIR = HERE / "scenarios" / "service_incident_v1"
RESULT_SCHEMA_REVISION = 1


# ── the two declared tool boundaries ─────────────────────────────────────────
#
# ``scn`` is the loaded scenario. It is passed explicitly (no module-level
# state) and listed in ``ignore`` so it is not part of the recorded arguments.


@record.tool(mutation_class="read-only", version="1", ignore=("scn",))
def read_metrics(scn: lab.Scenario, service: str) -> dict[str, Any]:
    """Return the fixture metrics series for ``service`` (bounded, JSON-native)."""
    return lab.query_metrics(scn, service)


@record.tool(mutation_class="read-only", version="1", ignore=("scn",))
def search_logs(scn: lab.Scenario, service: str, limit: int) -> dict[str, Any]:
    """Return the newest ``limit`` fixture log lines for ``service`` (bounded)."""
    return lab.query_logs(scn, service, limit)


_TOOLS: dict[str, Callable[..., dict[str, Any]]] = {
    "read_metrics": read_metrics,
    "search_logs": search_logs,
}


def call_tool(scn: lab.Scenario, name: str, **arguments: Any) -> dict[str, Any]:
    """Dispatch through the fixed allowlist; anything else is refused unexecuted."""
    if name not in scn.manifest.tools.allowlist or name not in _TOOLS:
        raise lab.EvidenceLabError("E_TOOL_NOT_ALLOWED", f"tool {name!r} is not on the allowlist")
    try:
        return _TOOLS[name](scn, **arguments)
    except TypeError as exc:
        raise lab.EvidenceLabError("E_TOOL_ARGUMENTS_INVALID", f"{name}: {exc}") from exc


class _StepBudget:
    """Bounds the flow: every tool, model and review step spends one step."""

    def __init__(self, max_steps: int) -> None:
        self.max_steps = max_steps
        self.used = 0

    def spend(self, label: str) -> None:
        if self.used + 1 > self.max_steps:
            raise lab.EvidenceLabError(
                "E_STEP_BUDGET_EXCEEDED", f"step {label!r} would exceed max_steps={self.max_steps}"
            )
        self.used += 1


# ── the one model call ───────────────────────────────────────────────────────


def _ask_model(
    scn: lab.Scenario, body: dict[str, Any], env: Mapping[str, str], timeout: float
) -> tuple[lab.Decision, dict[str, str]]:
    import openai  # imported here: the tools and guards work without the SDK installed

    client = openai.OpenAI(
        base_url=env["OPENAI_BASE_URL"],
        api_key=env["OPENAI_API_KEY"],
        max_retries=0,  # one logical call is one request: no hidden retries
        timeout=timeout,
    )
    try:
        response = client.chat.completions.create(**body)
    except openai.APITimeoutError as exc:
        raise lab.EvidenceLabError("E_MODEL_TIMEOUT", f"no model reply within {timeout}s") from exc
    except openai.APIConnectionError as exc:
        raise lab.EvidenceLabError(
            "E_MODEL_UNREACHABLE", "the loopback model is unreachable"
        ) from exc
    except openai.APIStatusError as exc:
        server = exc.body.get("code") if isinstance(exc.body, dict) else None
        raise lab.EvidenceLabError(
            "E_MODEL_REQUEST_REJECTED", f"HTTP {exc.status_code}, server code {server or 'none'}"
        ) from exc
    except (openai.OpenAIError, ValueError) as exc:
        raise lab.EvidenceLabError(
            "E_MODEL_RESPONSE_MALFORMED", "reply is not a completion"
        ) from exc

    choices = getattr(response, "choices", None)
    if not isinstance(choices, list) or len(choices) != 1:
        raise lab.EvidenceLabError("E_MODEL_RESPONSE_MALFORMED", "expected exactly one choice")
    choice = choices[0]
    content = getattr(getattr(choice, "message", None), "content", None)
    if not isinstance(content, str) or not content or not isinstance(response.model, str):
        raise lab.EvidenceLabError("E_MODEL_RESPONSE_MALFORMED", "choice has no text content")
    if choice.finish_reason != "stop":
        raise lab.EvidenceLabError(
            "E_MODEL_RESPONSE_INCOMPLETE", f"finish_reason is {choice.finish_reason!r}, not 'stop'"
        )
    if response.model != scn.manifest.model_id:
        raise lab.EvidenceLabError(
            "E_MODEL_RESPONSE_MISMATCH", f"reply model is {response.model!r}"
        )
    meta = {"response_model": response.model, "completion_id": str(response.id)}
    return lab.parse_decision(content), meta


# ── flow ─────────────────────────────────────────────────────────────────────


def record_consumption(asset_ref: str, status: str, env: Mapping[str, str]) -> None:
    """Declare, through NovaFabric's documented SDK call, that this run used ``asset_ref``.

    ``record_asset_consumption`` appends to the active capsule's ``assets.jsonl``; at
    finalisation ``nova capture`` turns it into a ``consumed`` lineage edge. Outside a
    capture there is no capsule to write to, which is a named failure, not a silent skip.
    """
    capsule_dir = env.get("NOVAFABRIC_CAPSULE_DIR")
    if not capsule_dir:
        raise lab.EvidenceLabError(
            "E_CAPTURE_CONTEXT_MISSING", "--asset-ref needs the capsule that `nova capture` opens"
        )
    from novafabric.registry.service import record_asset_consumption

    record_asset_consumption(asset_ref, status, Path(capsule_dir))


def run_agent(
    scenario_dir: Path,
    *,
    env: Mapping[str, str] | None = None,
    result_path: Path | None = None,
    timeout_seconds: float | None = None,
    variant: str = lab.BASE_VARIANT,
    asset_ref: str | None = None,
    asset_status: str = "development",
) -> dict[str, Any]:
    """Run the whole flow once; raise :class:`lab.EvidenceLabError` on any named failure."""
    env = os.environ if env is None else env
    lab.validate_environment(env)  # before anything else, and before any SDK use
    scn = lab.load_scenario(scenario_dir, variant)
    if asset_ref is not None:
        record_consumption(asset_ref, asset_status, env)
    manifest = scn.manifest
    budget = _StepBudget(manifest.limits.max_steps)
    if len(manifest.tools.expected_order) + 2 > manifest.limits.max_steps:
        raise lab.EvidenceLabError(
            "E_STEP_BUDGET_EXCEEDED",
            f"the flow needs {len(manifest.tools.expected_order) + 2} steps; "
            f"max_steps={manifest.limits.max_steps}",
        )
    timeout = float(manifest.limits.timeout_seconds)
    if timeout_seconds is not None:
        timeout = min(timeout, float(timeout_seconds))

    tool_sequence: list[str] = []
    observations: dict[str, Any] = {}
    for name in manifest.tools.expected_order:
        budget.spend(name)
        arguments: dict[str, Any] = {"service": lab.INVESTIGATED_SERVICE}
        if name == "search_logs":
            arguments["limit"] = manifest.tools.logs_limit
        observations[name] = call_tool(scn, name, **arguments)
        tool_sequence.append(name)

    body = lab.build_request_body(scn, observations)
    request_sha256 = lab.sha256_hex(lab.canonical_json(body))
    budget.spend("model_call")
    decision, model_meta = _ask_model(scn, body, env, timeout)

    budget.spend("policy_review")
    verdict = lab.evaluate_policy(scn, decision)
    task_verdict = "PASS" if verdict.result == "accept" else "FAIL"
    decision_json = decision.model_dump()
    result: dict[str, Any] = {
        "schema_revision": RESULT_SCHEMA_REVISION,
        "status": "completed",
        "scenario_id": manifest.scenario_id,
        "variant": scn.variant,
        "prompt_revision": scn.prompt_revision,
        "tool_sequence": tool_sequence,
        "observation_sha256": {
            name: lab.sha256_hex(lab.canonical_json(value)) for name, value in observations.items()
        },
        "model": {
            "requested_model": manifest.model_id,
            "response_model": model_meta["response_model"],
            "completion_id": model_meta["completion_id"],
            "request_sha256": request_sha256,
        },
        "decision": decision_json,
        "decision_sha256": lab.sha256_hex(lab.canonical_json(decision_json)),
        "policy": {"result": verdict.result, "rule": verdict.rule},
        "task_verdict": task_verdict,
        "steps_used": budget.used,
    }
    if result_path is not None:
        _write_json(result_path, result)
    return result


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EvidenceLab D1 incident-investigation agent")
    parser.add_argument("--scenario-dir", type=Path, default=DEFAULT_SCENARIO_DIR)
    parser.add_argument("--result-path", type=Path, help="write the diagnostic result here")
    parser.add_argument("--timeout-seconds", type=float, help="cap the model-call timeout")
    parser.add_argument("--variant", choices=lab.VARIANTS, default=lab.BASE_VARIANT)
    parser.add_argument("--asset-ref", help="registered asset this run consumes (name@version)")
    parser.add_argument("--asset-status", default="development")
    args = parser.parse_args(argv)
    try:
        result = run_agent(
            args.scenario_dir,
            result_path=args.result_path,
            timeout_seconds=args.timeout_seconds,
            variant=args.variant,
            asset_ref=args.asset_ref,
            asset_status=args.asset_status,
        )
    except lab.EvidenceLabError as exc:
        sys.stderr.write(f"EVIDENCELAB_ERROR code={exc.code} message={exc.message}\n")
        if args.result_path is not None:
            _write_json(
                args.result_path,
                {
                    "schema_revision": RESULT_SCHEMA_REVISION,
                    "status": "error",
                    "error": {"code": exc.code, "message": exc.message},
                },
            )
        return 2
    print(
        f"agent: decision={result['decision']['action']} "
        f"policy={result['policy']['result']} ({result['policy']['rule']}) "
        f"task_verdict={result['task_verdict']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
