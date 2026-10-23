"""A fake ``opa`` executable that records how it was started (serve-01 tests).

It plays the attacker's best case: whatever ``eval`` or ``test`` is asked, it
answers with the environment it was started with, which is exactly what
``opa.runtime().env`` returns under a real ``opa`` that still allows that
built-in. If the server's secrets are not in what the fake saw, no policy could
have read them either.

Each call is appended to a JSON-lines log with the argv, the exec environment
and, when ``--capabilities`` is passed, the parsed capabilities document. The
log path is baked into the script, because the environment it would otherwise
come from is the very thing under test.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_SCRIPT = '''#!{python}
import json, os, sys
LOG = {log!r}
CAPS_EXIT = {caps_exit!r}
argv = sys.argv[1:]
def exec_env():
    # The environment as exec'd, before the interpreter edits its own copy
    # (PEP 538 locale coercion adds LC_CTYPE to os.environ).
    try:
        raw = open("/proc/self/environ", "rb").read()
    except OSError:
        return dict(os.environ)
    pairs = (kv.split(b"=", 1) for kv in raw.split(b"\\0") if b"=" in kv)
    return {{k.decode(): v.decode() for k, v in pairs}}
ENV = exec_env()
rec = {{"argv": argv, "env": ENV}}
if "--capabilities" in argv:
    p = argv[argv.index("--capabilities") + 1]
    try:
        rec["capabilities"] = json.load(open(p))
    except Exception as exc:
        rec["capabilities_error"] = repr(exc)
with open(LOG, "a") as f:
    f.write(json.dumps(rec) + "\\n")
if argv[:2] == ["capabilities", "--current"]:
    if CAPS_EXIT:
        sys.stderr.write("unknown command \\"capabilities\\"\\n")
        sys.exit(CAPS_EXIT)
    print(json.dumps({{
        "builtins": [{{"name": n}} for n in
                     ("abs", "http.send", "net.lookup_ip_addr", "opa.runtime", "json.marshal")],
        "features": ["rego_v1"],
    }}))
    sys.exit(0)
if argv and argv[0] == "eval":
    reason = json.dumps(ENV)
    if "--format" in argv and argv[argv.index("--format") + 1] == "pretty":
        print(reason)
    else:
        print(json.dumps({{"result": [{{"expressions": [
            {{"value": {{"allow": True, "reason": reason}}}}]}}]}}))
    sys.exit(0)
if argv and argv[0] == "test":
    print("PASS: 1/1 " + json.dumps(ENV))
    sys.exit(0)
sys.exit(3)
'''

#: Built-ins the sandbox must remove.
DENIED = ("http.send", "net.lookup_ip_addr", "opa.runtime")


class FakeOpa:
    """Install a fake ``opa`` under *root*/fakebin; read back what it saw."""

    def __init__(self, root: Path, *, capabilities_exit: int = 0) -> None:
        self.bin_dir = root / "fakebin"
        self.bin_dir.mkdir(parents=True)
        self.log = root / "opa-calls.jsonl"
        self.exe = self.bin_dir / "opa"
        self.exe.write_text(
            _SCRIPT.format(
                python=sys.executable, log=str(self.log), caps_exit=capabilities_exit
            )
        )
        self.exe.chmod(0o755)

    def calls(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]

    def evals(self) -> list[dict[str, Any]]:
        """The calls that evaluate Rego (``eval``/``test``), not the capabilities probe."""
        return [c for c in self.calls() if c["argv"][:1] in (["eval"], ["test"])]


def clear_capabilities_cache() -> None:
    from novafabric.policy import _opa_sandbox

    _opa_sandbox.clear_capabilities_cache()


def assert_no_server_env(env: dict[str, str], *secrets: str) -> None:
    """``opa`` was started with ``PATH`` and nothing else, and no secret value."""
    dumped = json.dumps(env)
    for secret in secrets:
        assert secret not in dumped
    assert set(env) <= {"PATH"}, f"opa saw more than PATH: {sorted(env)}"


def assert_restricted_capabilities(call: dict[str, Any]) -> None:
    """The call passed ``--capabilities`` without :data:`DENIED` and with no network."""
    assert "--capabilities" in call["argv"], call["argv"]
    caps = call.get("capabilities")
    assert caps is not None, call.get("capabilities_error")
    names = {b["name"] for b in caps["builtins"]}
    for denied in DENIED:
        assert denied not in names, f"{denied} still allowed"
    assert "abs" in names and "json.marshal" in names, "harmless builtins were dropped"
    assert caps.get("allow_net") == [], "network must be denied outright"
    assert caps.get("features") == ["rego_v1"], "opa's own features must be preserved"
