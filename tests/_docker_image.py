"""Skip a live-Docker test when the registry will not give us its base image.

GitHub-hosted runners pull anonymously from Docker Hub, which rate-limits per IP
(``toomanyrequests: You have reached your unauthenticated pull rate limit``). On
2026-10-09 that failed five live tests in the ``unit`` job although nothing in the
code had changed. A rate-limited pull means "the image is unavailable", which is the
same situation as "no Docker": the test cannot run here. Anything else — a typo in the
image name, an auth failure, a docker run that fails — is NOT skipped.
"""

from __future__ import annotations

import functools
import subprocess

import pytest

_RATE_LIMIT_SIGNATURES = ("toomanyrequests", "rate limit")


@functools.lru_cache(maxsize=None)
def _image_unavailable_reason(image: str) -> str | None:
    """None if *image* is local or pullable; else the rate-limit message."""
    try:
        have = subprocess.run(
            ["docker", "image", "inspect", image], capture_output=True, timeout=30
        )
        if have.returncode == 0:
            return None
        pull = subprocess.run(
            ["docker", "pull", "--quiet", image], capture_output=True, timeout=300
        )
    except (OSError, subprocess.TimeoutExpired):
        return None  # let the test itself report what is wrong
    if pull.returncode == 0:
        return None
    text = (pull.stderr + pull.stdout).decode(errors="replace").lower()
    if any(sig in text for sig in _RATE_LIMIT_SIGNATURES):
        return f"docker registry rate limit: cannot pull {image}"
    return None


def skip_if_image_rate_limited(image: str) -> None:
    """Call from a fixture or test after the daemon is known to be reachable."""
    reason = _image_unavailable_reason(image)
    if reason is not None:
        pytest.skip(reason)
