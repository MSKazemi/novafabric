"""Run the ``opa`` binary sandboxed: no inherited environment, no secrets, no network.

Every ``opa`` subprocess NovaFabric starts goes through :func:`run_opa`. It used
to be started with the caller's whole environment. Under ``nova serve`` that
environment holds ``NOVAFABRIC_SERVE_TOKEN`` (the admin session token), and Rego
can read it: ``opa.runtime().env`` returns the process environment, and the
decision ``reason`` went back to the HTTP caller verbatim. A read-scope token
became an admin token (serve-01). ``http.send`` gave the same caller SSRF.

Two independent layers close that, so a mistake in one is not a leak:

* **Environment.** ``opa`` starts with :func:`scrubbed_env`: a fixed ``PATH``
  and nothing else. It is invoked by absolute path, so it needs no lookup, and
  ``opa eval``/``opa test`` read no environment variable they depend on.
* **Capabilities.** ``opa`` gets ``--capabilities`` with a file derived from
  the binary's own ``opa capabilities --current``, minus
  :data:`DENIED_BUILTINS`, with ``allow_net`` empty. Rego that names a denied
  built-in fails to compile, so it cannot run, whatever it is.

The capabilities file is derived from the installed binary rather than shipped,
so the packaged policies keep every built-in the installed ``opa`` offers. If
the file cannot be built, nothing runs: :class:`OpaSandboxError` is raised and
every caller turns it into a deny.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

__all__ = [
    "DENIED_BUILTINS",
    "OpaNotFoundError",
    "OpaSandbox",
    "OpaSandboxError",
    "clear_capabilities_cache",
    "prepare",
    "restricted_capabilities",
    "run_opa",
    "scrubbed_env",
]

#: Built-ins Rego must never reach from a NovaFabric-started ``opa``:
#: ``opa.runtime`` returns the process environment and config; ``http.send``
#: and ``net.lookup_ip_addr`` reach the network from the server's position.
DENIED_BUILTINS: Final[frozenset[str]] = frozenset(
    {"http.send", "net.lookup_ip_addr", "opa.runtime"}
)

#: The only environment ``opa`` receives. ``os.defpath`` is a constant
#: (``/bin:/usr/bin``), not the server's ``PATH``, so it carries no information.
_SANDBOX_PATH: Final[str] = os.defpath

_CAPABILITIES_TIMEOUT_S: Final[float] = 10.0

_INSTALL_HINT: Final[str] = (
    "opa binary not found on PATH. "
    "Install OPA: https://www.openpolicyagent.org/docs/latest/#running-opa"
)


class OpaNotFoundError(RuntimeError):
    """Raised when the ``opa`` binary cannot be found on PATH."""


class OpaSandboxError(RuntimeError):
    """The restricted capabilities could not be built, so ``opa`` must not run."""


def scrubbed_env() -> dict[str, str]:
    """The complete environment an ``opa`` subprocess is started with."""
    return {"PATH": _SANDBOX_PATH}


@dataclass(frozen=True)
class OpaSandbox:
    """A resolved ``opa`` executable plus the capabilities file it must run under."""

    executable: str
    capabilities: Path
    env: Mapping[str, str] = field(default_factory=scrubbed_env)

    def command(self, subcommand: str, args: Sequence[str]) -> list[str]:
        """``opa <subcommand> --capabilities <file> <args...>``."""
        return [self.executable, subcommand, "--capabilities", str(self.capabilities), *args]


def _resolve_executable() -> str:
    found = shutil.which("opa")
    if not found:
        raise OpaNotFoundError(_INSTALL_HINT)
    return os.path.abspath(found)


_cache_lock = threading.Lock()
_cache: dict[tuple[str, int, int], Path] = {}
_cache_dirs: list[Path] = []


def clear_capabilities_cache() -> None:
    """Forget every derived capabilities file (and delete it)."""
    with _cache_lock:
        _cache.clear()
        while _cache_dirs:
            shutil.rmtree(_cache_dirs.pop(), ignore_errors=True)


atexit.register(clear_capabilities_cache)


def _restrict(doc: Any) -> dict[str, Any]:
    """Drop :data:`DENIED_BUILTINS` from a capabilities document and deny network."""
    if not isinstance(doc, dict) or not isinstance(doc.get("builtins"), list):
        raise OpaSandboxError("opa capabilities output has no builtins list")
    builtins = [
        b
        for b in doc["builtins"]
        if isinstance(b, dict) and b.get("name") not in DENIED_BUILTINS
    ]
    if not builtins:
        raise OpaSandboxError("opa capabilities output lists no usable builtins")
    restricted = dict(doc)
    restricted["builtins"] = builtins
    restricted["allow_net"] = []
    return restricted


def restricted_capabilities(executable: str) -> Path:
    """Path to a capabilities file for *executable* without :data:`DENIED_BUILTINS`.

    Derived once per binary (keyed by path, mtime and size) from
    ``opa capabilities --current``, itself run in the scrubbed environment.
    Raises :class:`OpaSandboxError` if that fails; callers must then deny.
    """
    try:
        st = os.stat(executable)
    except OSError as exc:
        raise OpaNotFoundError(_INSTALL_HINT) from exc
    key = (executable, st.st_mtime_ns, st.st_size)
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None and cached.is_file():
            return cached
        try:
            proc = subprocess.run(
                [executable, "capabilities", "--current"],
                capture_output=True,
                text=True,
                timeout=_CAPABILITIES_TIMEOUT_S,
                env=scrubbed_env(),
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise OpaNotFoundError(_INSTALL_HINT) from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OpaSandboxError(f"opa capabilities --current failed: {exc}") from exc
        if proc.returncode != 0:
            raise OpaSandboxError(
                "opa capabilities --current failed (an opa too old to restrict its "
                f"built-ins is refused): {proc.stderr.strip()[:500]}"
            )
        try:
            restricted = _restrict(json.loads(proc.stdout))
        except json.JSONDecodeError as exc:
            raise OpaSandboxError(f"opa capabilities output is not JSON: {exc}") from exc
        out_dir = Path(tempfile.mkdtemp(prefix="novafabric-opa-caps-"))  # mode 0700
        _cache_dirs.append(out_dir)
        path = out_dir / "capabilities.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(restricted, fh)
        _cache[key] = path
        return path


def prepare() -> OpaSandbox:
    """Resolve ``opa`` and its restricted capabilities, or raise.

    Raises :class:`OpaNotFoundError` when there is no binary and
    :class:`OpaSandboxError` when it cannot be restricted.
    """
    executable = _resolve_executable()
    return OpaSandbox(executable=executable, capabilities=restricted_capabilities(executable))


def run_opa(
    sandbox: OpaSandbox,
    subcommand: str,
    args: Sequence[str],
    *,
    input_text: str | None = None,
    timeout: float | None,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run ``opa <subcommand>`` under *sandbox*.

    ``FileNotFoundError`` becomes :class:`OpaNotFoundError`;
    ``subprocess.TimeoutExpired`` propagates for the caller to turn into a deny.
    """
    try:
        return subprocess.run(
            sandbox.command(subcommand, args),
            input=input_text,
            stdin=subprocess.DEVNULL if input_text is None else None,
            capture_output=capture,
            text=True,
            timeout=timeout,
            env=dict(sandbox.env),
        )
    except FileNotFoundError as exc:
        raise OpaNotFoundError(_INSTALL_HINT) from exc
