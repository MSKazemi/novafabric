"""``skip_if_image_rate_limited`` skips on a registry rate limit and on nothing else."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import _docker_image as helper
import pytest


def _run(returncodes_and_text):
    seq = iter(returncodes_and_text)

    def fake(argv, **kw):
        rc, err = next(seq)
        return subprocess.CompletedProcess(argv, rc, stdout=b"", stderr=err)

    return fake


@pytest.fixture(autouse=True)
def _fresh() -> None:
    helper._image_unavailable_reason.cache_clear()


def test_rate_limited_pull_is_reported() -> None:
    err = b"Error response from daemon: toomanyrequests: You have reached your unauthenticated pull rate limit."
    with patch.object(subprocess, "run", _run([(1, b"No such image"), (1, err)])):
        assert "rate limit" in (helper._image_unavailable_reason("x:1") or "")


def test_a_local_image_is_available() -> None:
    with patch.object(subprocess, "run", _run([(0, b"")])):
        assert helper._image_unavailable_reason("x:2") is None


def test_a_successful_pull_is_available() -> None:
    with patch.object(subprocess, "run", _run([(1, b"No such image"), (0, b"")])):
        assert helper._image_unavailable_reason("x:3") is None


def test_other_pull_failures_are_not_skipped() -> None:
    err = b"Error response from daemon: pull access denied for nope, repository does not exist"
    with patch.object(subprocess, "run", _run([(1, b"No such image"), (1, err)])):
        assert helper._image_unavailable_reason("nope:1") is None


def test_skip_function_skips_only_on_a_rate_limit() -> None:
    with patch.object(helper, "_image_unavailable_reason", return_value="docker registry rate limit"):
        with pytest.raises(pytest.skip.Exception):
            helper.skip_if_image_rate_limited("x:4")
    with patch.object(helper, "_image_unavailable_reason", return_value=None):
        helper.skip_if_image_rate_limited("x:5")  # no exception
