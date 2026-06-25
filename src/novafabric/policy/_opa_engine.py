from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

from ._models import PolicyDecision, PolicyInput


def _parse_package_from_rego(source: str) -> str | None:
    """Return the dotted package name from a Rego source string, or None."""
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("package "):
            return stripped[len("package "):].strip()
    return None

_ACTION_TO_PACKAGE: dict[str, str] = {
    "promote": "novafabric/defaults/promote_gate",
    "replay_mutating": "novafabric/defaults/replay_mutating",
    "evidence_export": "novafabric/defaults/evidence_export",
    "dataset_license": "novafabric/defaults/dataset_license",
}


class OpaNotFoundError(RuntimeError):
    """Raised when the ``opa`` binary cannot be found on PATH."""


def _find_bundle_path() -> Path:
    """Locate the built-in Rego bundle directory.

    Search order:
    1. ``NOVAFABRIC_POLICY_BUNDLE_PATH`` environment variable.
    2. Sibling ``policies/`` directory relative to the ``novafabric`` package root
       (resolves correctly both in-tree under ``src/`` and after installation into
       ``site-packages``).
    """
    env = os.environ.get("NOVAFABRIC_POLICY_BUNDLE_PATH")
    if env:
        return Path(env)
    # __file__ is src/novafabric/policy/_opa_engine.py
    # parent      = src/novafabric/policy/
    # parent.parent = src/novafabric/         ← package root
    # / "policies" = src/novafabric/policies/ ← bundle lives here
    return Path(__file__).parent.parent / "policies"


_BUNDLE_DEFAULT: Path | None = None


def _get_bundle_default() -> Path:
    global _BUNDLE_DEFAULT
    if _BUNDLE_DEFAULT is None:
        _BUNDLE_DEFAULT = _find_bundle_path()
    return _BUNDLE_DEFAULT


class OpaEngine:
    """Policy engine that delegates evaluation to the ``opa eval`` subprocess."""

    def __init__(self, bundle_path: Path | None = None) -> None:
        self._bundle = bundle_path or _get_bundle_default()

    def evaluate(
        self,
        input_: PolicyInput,
        explain: bool = False,
        policy_source: str | None = None,
    ) -> PolicyDecision:
        if policy_source and policy_source.strip():
            return self._evaluate_custom(input_, policy_source.strip(), explain)

        action = input_.action
        if action not in _ACTION_TO_PACKAGE:
            return PolicyDecision(
                allow=False,
                reason=f"no policy registered for action '{action}'",
                decision_id=str(uuid.uuid4()),
            )

        package_path = _ACTION_TO_PACKAGE[action]
        # OPA query: data.<package/path/with/dots>
        query = "data." + package_path.replace("/", ".")
        input_json = input_.model_dump_json()

        try:
            result = subprocess.run(
                [
                    "opa",
                    "eval",
                    "--data",
                    str(self._bundle),
                    "--input",
                    "/dev/stdin",
                    "--format",
                    "json",
                    query,
                ],
                input=input_json,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except FileNotFoundError as exc:
            raise OpaNotFoundError(
                "opa binary not found on PATH. "
                "Install OPA: https://www.openpolicyagent.org/docs/latest/#running-opa"
            ) from exc
        except subprocess.TimeoutExpired:
            return PolicyDecision(
                allow=False,
                reason="opa eval timed out after 30s",
                decision_id=str(uuid.uuid4()),
            )

        decision_id = str(uuid.uuid4())

        if result.returncode != 0:
            return PolicyDecision(
                allow=False,
                reason=f"opa eval error: {result.stderr}",
                decision_id=decision_id,
                policy_path=query,
            )

        try:
            data = json.loads(result.stdout)
            value: dict[str, object] = data["result"][0]["expressions"][0]["value"]
            allow = bool(value.get("allow", False))
            reason = str(value.get("reason", ""))
        except (json.JSONDecodeError, KeyError, IndexError) as exc:
            return PolicyDecision(
                allow=False,
                reason=f"opa response parse error: {exc}",
                decision_id=decision_id,
                policy_path=query,
            )

        trace_text: str | None = None
        if explain:
            trace_text = self._explain(input_json, query, data_path=str(self._bundle))

        return PolicyDecision(
            allow=allow,
            reason=reason,
            decision_id=decision_id,
            policy_path=query,
            trace_text=trace_text,
        )

    def _evaluate_custom(
        self, input_: PolicyInput, rego_source: str, explain: bool
    ) -> PolicyDecision:
        """Evaluate user-supplied Rego source against *input_*."""
        decision_id = str(uuid.uuid4())
        pkg = _parse_package_from_rego(rego_source) or "novafabric.authz"
        query = f"data.{pkg}.allow"
        input_json = input_.model_dump_json()

        with tempfile.TemporaryDirectory() as tmpdir:
            policy_file = Path(tmpdir) / "custom.rego"
            policy_file.write_text(rego_source)

            try:
                result = subprocess.run(
                    [
                        "opa",
                        "eval",
                        "--data",
                        tmpdir,
                        "--input",
                        "/dev/stdin",
                        "--format",
                        "json",
                        query,
                    ],
                    input=input_json,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except FileNotFoundError as exc:
                raise OpaNotFoundError(
                    "opa binary not found on PATH. "
                    "Install OPA: https://www.openpolicyagent.org/docs/latest/#running-opa"
                ) from exc
            except subprocess.TimeoutExpired:
                return PolicyDecision(
                    allow=False,
                    reason="opa eval timed out after 30s",
                    decision_id=decision_id,
                    policy_path=f"custom:{query}",
                )

            if result.returncode != 0:
                return PolicyDecision(
                    allow=False,
                    reason=f"opa eval error: {result.stderr}",
                    decision_id=decision_id,
                    policy_path=f"custom:{query}",
                )

            try:
                data = json.loads(result.stdout)
                raw = data["result"][0]["expressions"][0]["value"]
                # Query returns a bool (data.pkg.allow) or dict (full package object)
                if isinstance(raw, bool):
                    allow = raw
                    reason = ""
                elif isinstance(raw, dict):
                    allow = bool(raw.get("allow", False))
                    reason = str(raw.get("reason", ""))
                else:
                    allow = bool(raw)
                    reason = ""
            except (json.JSONDecodeError, KeyError, IndexError) as exc:
                return PolicyDecision(
                    allow=False,
                    reason=f"opa response parse error: {exc}",
                    decision_id=decision_id,
                    policy_path=f"custom:{query}",
                )

            trace_text: str | None = None
            if explain:
                trace_text = self._explain(input_json, query, data_path=tmpdir)

            return PolicyDecision(
                allow=allow,
                reason=reason,
                decision_id=decision_id,
                policy_path=f"custom:{query}",
                trace_text=trace_text,
            )

    def _explain(self, input_json: str, query: str, data_path: str | None = None) -> str:
        """Return human-readable OPA trace (--explain full --format pretty)."""
        try:
            result = subprocess.run(
                [
                    "opa",
                    "eval",
                    "--data",
                    data_path or str(self._bundle),
                    "--input",
                    "/dev/stdin",
                    "--explain",
                    "full",
                    "--format",
                    "pretty",
                    query,
                ],
                input=input_json,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return "(trace unavailable)"
        return (result.stdout + result.stderr).strip() or "(empty trace)"
