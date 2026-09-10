"""Which environment variables a runner may carry across a trust boundary.

The orchestrator builds ``RunnerJobSpec.env`` from ``dict(os.environ)``
(``capture/orchestrator.py``) because the **local** runner needs the user's
real environment — the workload is the user's own agent, running as the user,
on the user's machine, and it needs its provider credentials to work at all.

Every *other* runner hands that dict to something outside that boundary:
a container's ``-e`` flags, a Kubernetes ``Job`` object persisted in etcd, a
batch script written to a shared filesystem. Forwarding the submitting
shell's whole environment there discloses every secret it happens to hold to
anyone who can read the job object, the process table, or the spool
directory.

So the rule is a **default-deny allowlist**, and it lives here rather than in
each runner. That is the whole point of this module: the same property was
previously open-coded in the Docker and SLURM runners and simply *absent*
from the Kubernetes one (defect B2, disclosed 2026-08-28, fixed by ADR-0270).
A safety property delegated to N call sites holds at N-1 of them.

Anything a workload genuinely needs beyond ``NOVAFABRIC_*`` is passed
**explicitly** by the operator through the runner's ``extra_env`` option — an
opt-in they can see and audit, rather than an inheritance they cannot.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

#: Variables NovaFabric itself sets and every runner must carry: they are what
#: makes capture fire inside the job (capsule dir, span id, emit mode).
FORWARDABLE_PREFIX = "NOVAFABRIC_"

__all__ = ["FORWARDABLE_PREFIX", "forwardable_env", "is_forwardable"]


def is_forwardable(key: str, *, also_allow: frozenset[str] = frozenset()) -> bool:
    """Return whether ``key`` may cross a runner's trust boundary.

    ``also_allow`` carries the per-runner exceptions — SLURM needs ``PATH`` so
    the compute node can find the interpreter; a container gets its ``PATH``
    from the image and must not inherit the submitter's.
    """
    return key.startswith(FORWARDABLE_PREFIX) or key in also_allow


def forwardable_env(
    env: Mapping[str, str],
    *,
    also_allow: Iterable[str] = (),
) -> dict[str, str]:
    """Filter ``env`` down to the variables safe to send off-host.

    Args:
        env: the orchestrator-provided environment (effectively ``os.environ``).
        also_allow: extra keys this particular runner needs, e.g. ``{"PATH"}``
            for SLURM, or the operator's explicit ``extra_env`` keys.

    Returns:
        A new dict containing only the allowed entries. Never mutates ``env``.
    """
    allowed = frozenset(also_allow)
    return {k: v for k, v in env.items() if is_forwardable(k, also_allow=allowed)}
