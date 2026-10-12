"""Shared precondition for the OCI-container eval suites (ADR-0308).

NovaFabric publishes no eval images, so a container suite has no usable default:
the operator must name an image they built or host and pin it by digest. This check
runs before Docker is touched so the failure names the exact variables to set,
instead of surfacing as ``invalid OCI digest ''``.
"""
from __future__ import annotations

from novafabric.runners._types import ContainerEvalError


def require_pinned_image(
    *, suite_id: str, image_var: str, digest_var: str, image: str, digest: str
) -> None:
    """Raise :class:`ContainerEvalError` unless both *image* and *digest* are set.

    Format validation of the digest stays in ``DockerRunner.run_eval_container``.
    """
    missing = [var for var, value in ((image_var, image), (digest_var, digest)) if not value]
    if not missing:
        return
    raise ContainerEvalError(
        f"{suite_id}: {' and '.join(missing)} must be set. NovaFabric publishes no eval "
        f"image, so build or host one and set {image_var} to its reference and "
        f"{digest_var} to its digest (sha256:<64-hex>); see docs/cli-reference.md."
    )
