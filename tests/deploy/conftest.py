# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""Fixtures for tests/deploy that need a Docker daemon."""

from __future__ import annotations

import shutil
from collections.abc import Iterator

import pytest
from _docker_image import skip_if_image_rate_limited

from . import _oci_registry as oci


@pytest.fixture(scope="module")
def oci_registries() -> Iterator[oci.Registries]:
    """Two disposable local registries + a buildx builder that can push to them.

    ``a`` stands in for GHCR (and hosts the candidate repository), ``b`` for Docker Hub.
    Skipped, never failed, when there is no daemon or Docker Hub rate-limits the pulls.
    """
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not available")
    if oci.docker("info", check=False, timeout=60).returncode != 0:
        pytest.skip("docker daemon not reachable")
    skip_if_image_rate_limited(oci.REGISTRY_IMAGE)
    skip_if_image_rate_limited("moby/buildkit:buildx-stable-1")
    regs = oci.start_registries()
    try:
        yield regs
    finally:
        regs.teardown()
