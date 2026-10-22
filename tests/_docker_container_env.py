"""What a container would receive from a recorded ``docker run`` call.

The Docker runner passes each variable as ``-e NAME`` with the value in the
``docker`` client's own environment (``subprocess.run(..., env=...)``), so the
value is never on a command line. ``docker`` resolves ``-e NAME`` from that
environment, and ``-e NAME=VALUE`` literally. Tests that assert what reaches
the container resolve the flags the same way, instead of grepping argv for
``NAME=VALUE``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def container_env(argv: Sequence[str], client_env: Mapping[str, str] | None) -> dict[str, str]:
    """The ``NAME -> VALUE`` pairs docker would set from *argv*'s ``-e`` flags."""
    env: dict[str, str] = {}
    args = [str(a) for a in argv]
    for flag, value in zip(args, args[1:]):
        if flag != "-e":
            continue
        name, sep, literal = value.partition("=")
        if sep:
            env[name] = literal
        elif client_env is not None and name in client_env:
            env[name] = client_env[name]
    return env


def docker_run_container_env(run: object) -> dict[str, str]:
    """``container_env`` of the single ``docker run`` call recorded by a mock."""
    calls = [c for c in run.call_args_list if c.args[0][1:2] == ["run"]]  # type: ignore[attr-defined]
    assert len(calls) == 1, calls
    return container_env(calls[0].args[0], calls[0].kwargs.get("env"))
