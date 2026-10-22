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

import re
from collections.abc import Iterable, Mapping

#: Variables NovaFabric itself sets and every runner must carry: they are what
#: makes capture fire inside the job (capsule dir, span id, emit mode).
FORWARDABLE_PREFIX = "NOVAFABRIC_"

#: ``NOVAFABRIC_*`` names that hold a credential (server and SCIM tokens, client API
#: keys, database DSNs) are server- or client-side configuration that capture inside a
#: job never reads. The prefix alone forwarded them to every off-host runner, and SLURM
#: also put them in the ``--wrap`` script on ``sbatch``'s command line. A workload that
#: really needs one gets it through ``extra_env``.
_SECRET_NAME = re.compile(
    r"TOKEN|SECRET|PASSWORD|PASSWD|DSN|API_?KEY|PRIVATE_KEY|CREDENTIAL|PEPPER|DB_URL"
    r"|DATABASE_URL|_KEY$"
)

#: POSIX locale categories. Matching every ``LC_*`` name forwarded arbitrary
#: user variables that merely start with ``LC_``.
LOCALE_KEYS = frozenset(
    {"LC_ALL", "LC_CTYPE", "LC_NUMERIC", "LC_TIME", "LC_COLLATE", "LC_MONETARY",
     "LC_MESSAGES", "LC_PAPER", "LC_NAME", "LC_ADDRESS", "LC_TELEPHONE",
     "LC_MEASUREMENT", "LC_IDENTIFICATION"}
)

__all__ = [
    "FORWARDABLE_PREFIX",
    "LOCALE_KEYS",
    "SUBMIT_LOGIN_KEYS",
    "is_secret_name",
    "forwardable_env",
    "is_forwardable",
    "scheduler_submit_env",
    "shell_safe_env",
]

#: Login variables a batch-scheduler client needs to submit as the user. None of
#: them is a credential; they are what a fresh login shell would also have.
SUBMIT_LOGIN_KEYS = frozenset(
    {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TZ", "TMPDIR", "TERM",
     "KRB5CCNAME"}
)


def is_secret_name(key: str) -> bool:
    """True for a ``NOVAFABRIC_*`` name that holds a credential (see ``_SECRET_NAME``)."""
    return key.startswith(FORWARDABLE_PREFIX) and bool(
        _SECRET_NAME.search(key[len(FORWARDABLE_PREFIX):])
    )


def is_forwardable(key: str, *, also_allow: frozenset[str] = frozenset()) -> bool:
    """Return whether ``key`` may cross a runner's trust boundary.

    ``also_allow`` carries the per-runner exceptions — SLURM needs ``PATH`` so
    the compute node can find the interpreter; a container gets its ``PATH``
    from the image and must not inherit the submitter's. An explicitly allowed key
    is forwarded even when :func:`is_secret_name` would refuse it.
    """
    if key in also_allow:
        return True
    return key.startswith(FORWARDABLE_PREFIX) and not is_secret_name(key)


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


def scheduler_submit_env(
    environ: Mapping[str, str],
    *,
    client_prefixes: Iterable[str],
    extra_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The environment to run a batch scheduler's submit command (``sbatch``, ``bsub``) with.

    Schedulers propagate the *submitting process's* environment into the job by
    default — SLURM's ``--export=ALL``, LSF's copy of the submission environment —
    so filtering the job script (``forwardable_env``) is not enough: the submit
    process itself must not hold the user's secrets (ADR-0311, completing ADR-0270).
    It keeps only the scheduler client's own variables (``client_prefixes``, e.g.
    ``SLURM_``/``SBATCH_``), login basics (:data:`SUBMIT_LOGIN_KEYS`,
    :data:`LOCALE_KEYS`), forwardable ``NOVAFABRIC_*`` (not :func:`is_secret_name`),
    and the operator's explicit ``extra_env``. Called without ``extra_env`` it is
    also the environment for the scheduler's query and cancel commands.

    Args:
        environ: the orchestrator process environment (``os.environ``).
        client_prefixes: variable prefixes the scheduler client reads.
        extra_env: variables the operator chose to send into the job.

    Returns:
        A new dict; never mutates ``environ``.
    """
    prefixes = tuple(client_prefixes)
    env = {
        k: v
        for k, v in environ.items()
        if k in SUBMIT_LOGIN_KEYS
        or k in LOCALE_KEYS
        or k.startswith(prefixes)
        or is_forwardable(k)
    }
    env.update(extra_env or {})
    return env


_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def shell_safe_env(extra_env: Mapping[str, str]) -> dict[str, str]:
    """Return *extra_env* if every name is a shell identifier, else raise ``ValueError``.

    Batch runners write ``export NAME=value`` lines into a job script; a name is not
    quoted there, so anything but ``[A-Za-z_][A-Za-z0-9_]*`` would be shell syntax.
    """
    bad = sorted(k for k in extra_env if not _ENV_NAME.fullmatch(k))
    if bad:
        raise ValueError(f"extra_env names must be shell identifiers; refused: {bad}")
    return dict(extra_env)
