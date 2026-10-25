from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

from . import _opa_sandbox
from ._models import PolicyDecision, PolicyInput
from ._opa_sandbox import OpaNotFoundError, OpaSandbox, OpaSandboxError, run_opa

__all__ = ["DEFAULT_TIMEOUT_S", "OpaEngine", "OpaNotFoundError", "OpaSandboxError"]

#: A Rego package path: dotted identifiers only. The package of caller-supplied
#: Rego becomes part of the ``opa eval`` query, so anything else is refused
#: rather than spliced into a query string.
_PACKAGE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")

#: Default wall-clock bound on one ``opa`` subprocess.
DEFAULT_TIMEOUT_S = 30.0


def _parse_package_from_rego(source: str) -> str | None:
    """Return the dotted package name from a Rego source string, or None."""
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("package "):
            return stripped[len("package "):].strip()
    return None

_ACTION_TO_PACKAGE: dict[str, str] = {
    "promote": "novafabric/defaults/promote_gate",
    "promote_direct": "novafabric/defaults/promote_direct",
    "replay_mutating": "novafabric/defaults/replay_mutating",
    "evidence_export": "novafabric/defaults/evidence_export",
    "dataset_license": "novafabric/defaults/dataset_license",
}


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
    """Policy engine that delegates evaluation to the ``opa eval`` subprocess.

    Every ``opa`` it starts is sandboxed (:mod:`._opa_sandbox`): a scrubbed
    environment and capabilities without ``opa.runtime``, ``http.send`` or
    ``net.lookup_ip_addr``. That holds for the packaged policies and for
    caller-supplied ``policy_source`` alike. *Who* may supply Rego is the
    caller's decision; ``nova serve`` allows it only to ``admin``.
    """

    def __init__(
        self, bundle_path: Path | None = None, *, timeout_s: float = DEFAULT_TIMEOUT_S
    ) -> None:
        self._bundle = bundle_path or _get_bundle_default()
        self._timeout_s = timeout_s

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
        return self._evaluate_query(
            input_.model_dump_json(),
            query,
            data_path=str(self._bundle),
            policy_path=query,
            explain=explain,
        )

    def _evaluate_custom(
        self, input_: PolicyInput, rego_source: str, explain: bool
    ) -> PolicyDecision:
        """Evaluate caller-supplied Rego source against *input_* (sandboxed)."""
        pkg = _parse_package_from_rego(rego_source) or "novafabric.authz"
        if not _PACKAGE_RE.fullmatch(pkg):
            return PolicyDecision(
                allow=False,
                reason="invalid Rego package name (dotted identifiers only)",
                decision_id=str(uuid.uuid4()),
            )
        query = f"data.{pkg}.allow"
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "custom.rego").write_text(rego_source, encoding="utf-8")
            return self._evaluate_query(
                input_.model_dump_json(),
                query,
                data_path=tmpdir,
                policy_path=f"custom:{query}",
                explain=explain,
            )

    def _evaluate_query(
        self,
        input_json: str,
        query: str,
        *,
        data_path: str,
        policy_path: str,
        explain: bool,
    ) -> PolicyDecision:
        decision_id = str(uuid.uuid4())
        try:
            sandbox = _opa_sandbox.prepare()
        except OpaSandboxError as exc:
            return PolicyDecision(
                allow=False,
                reason=f"opa sandbox unavailable, refusing to evaluate: {exc}",
                decision_id=decision_id,
                policy_path=policy_path,
            )
        try:
            result = run_opa(
                sandbox,
                "eval",
                ["--data", data_path, "--input", "/dev/stdin", "--format", "json", query],
                input_text=input_json,
                timeout=self._timeout_s,
            )
        except subprocess.TimeoutExpired:
            return PolicyDecision(
                allow=False,
                reason=f"opa eval timed out after {self._timeout_s:g}s",
                decision_id=decision_id,
                policy_path=policy_path,
            )

        if result.returncode != 0:
            return PolicyDecision(
                allow=False,
                reason=f"opa eval error: {_error_text(result)}",
                decision_id=decision_id,
                policy_path=policy_path,
            )

        try:
            allow, reason = _parse_decision(result.stdout)
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            return PolicyDecision(
                allow=False,
                reason=f"opa response parse error: {exc}",
                decision_id=decision_id,
                policy_path=policy_path,
            )

        trace_text: str | None = None
        if explain:
            trace_text = self._explain(input_json, query, data_path=data_path, sandbox=sandbox)

        return PolicyDecision(
            allow=allow,
            reason=reason,
            decision_id=decision_id,
            policy_path=policy_path,
            trace_text=trace_text,
        )

    def _explain(
        self,
        input_json: str,
        query: str,
        data_path: str | None = None,
        sandbox: OpaSandbox | None = None,
    ) -> str:
        """Return human-readable OPA trace (--explain full --format pretty)."""
        try:
            box = sandbox or _opa_sandbox.prepare()
            result = run_opa(
                box,
                "eval",
                [
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
                input_text=input_json,
                timeout=self._timeout_s,
            )
        except (OpaNotFoundError, OpaSandboxError, subprocess.TimeoutExpired):
            return "(trace unavailable)"
        return (result.stdout + result.stderr).strip() or "(empty trace)"


def _error_text(result: subprocess.CompletedProcess[str]) -> str:
    """What ``opa`` said when it failed: stderr, else stdout.

    With ``--format json`` a compile error (an undefined built-in such as a
    sandbox-denied ``opa.runtime``) is written to *stdout* as ``{"errors": …}``
    and stderr is empty, which used to leave the reason blank.
    """
    stderr = result.stderr if isinstance(result.stderr, str) else ""
    stdout = result.stdout if isinstance(result.stdout, str) else ""
    return stderr.strip() or stdout.strip()


def _parse_decision(stdout: str) -> tuple[bool, str]:
    """``(allow, reason)`` from ``opa eval --format json`` output.

    The query yields a bool (``data.pkg.allow``) or an object (a whole package,
    or an ``allow`` rule whose value is ``{"allow": ..., "reason": ...}``).
    """
    data = json.loads(stdout)
    raw = data["result"][0]["expressions"][0]["value"]
    if isinstance(raw, bool):
        return raw, ""
    if isinstance(raw, dict):
        allow = raw.get("allow", False)
        reason = str(raw.get("reason", ""))
        if isinstance(allow, bool):
            return allow, reason
        return False, _non_boolean(allow) + (f" ({reason})" if reason else "")
    # Python truthiness is not a policy decision: allow := "false" is truthy, and
    # an empty set or 0 falsy for the wrong reason. A type error denies (review
    # misc-3-09).
    return False, _non_boolean(raw)


def _non_boolean(value: object) -> str:
    return f"non-boolean allow ({type(value).__name__}): a policy decision must be true or false"
