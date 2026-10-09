"""Which image a container runner actually ran, as the runtime reports it (ADR-0307).

A tag names an image; it does not identify one. ``python:3.12-slim`` today and
``python:3.12-slim`` next month are different images, so a capsule that records
only the tag cannot say what produced it. This module turns what the container
runtime itself reports into the ``capsule.yaml:host.runner.image`` block:

- ``image_id`` — the content-addressed local image ID (``docker image inspect``
  ``.Id``), ``sha256:<64 hex>``;
- ``repo_digests`` — registry manifest digests, ``<name>@sha256:<64 hex>``, the
  form you can pull and pin;
- ``resolved_by`` — which runtime surface said so;
- ``unresolved_reason`` — present instead of all three when nothing could be
  resolved. A digest is never inferred from the tag and never guessed.

Pure functions only: the runners do the I/O and hand the result here, so the
parsing is unit-tested without a daemon or a cluster.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "IMAGE_PROVENANCE_KEY",
    "ImageInspection",
    "image_block",
    "parse_docker_inspect",
    "parse_kubernetes_image_id",
    "resolve_docker_image",
]

#: The ``RunnerJobResult.runner_metadata`` key a runner puts the block under.
#: The orchestrator copies this one key into ``host.runner.image``; nothing
#: else in ``runner_metadata`` reaches the manifest.
IMAGE_PROVENANCE_KEY = "image_provenance"

_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REPO_DIGEST_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")

#: ``docker image inspect --format`` that prints the ID, then each repo digest,
#: whitespace-separated. Neither field can contain whitespace.
DOCKER_INSPECT_FORMAT = "{{.Id}}{{range .RepoDigests}} {{.}}{{end}}"

# Upper bound on how many repo digests one image block carries. An image
# tagged into many repositories is legitimate; an unbounded list is not.
_MAX_REPO_DIGESTS = 32


@dataclass(frozen=True)
class ImageInspection:
    """One read of the runtime's view of an image reference."""

    image_id: str | None = None
    repo_digests: tuple[str, ...] = field(default_factory=tuple)
    error: str | None = None

    @property
    def resolved(self) -> bool:
        return self.image_id is not None or bool(self.repo_digests)


def _repo_digests(values: Iterable[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for value in values:
        if _REPO_DIGEST_RE.match(value) and value not in seen:
            seen.append(value)
    return tuple(seen[:_MAX_REPO_DIGESTS])


def parse_docker_inspect(stdout: str) -> ImageInspection:
    """Parse the output of ``docker image inspect --format DOCKER_INSPECT_FORMAT``.

    Anything that is not a well-formed ``sha256:`` ID is refused rather than
    recorded: a Podman-style bare hex ID, an empty line, a template error.
    """
    words = stdout.split()
    if not words:
        return ImageInspection(error="docker image inspect printed nothing")
    image_id, rest = words[0], words[1:]
    if not _IMAGE_ID_RE.match(image_id):
        return ImageInspection(
            error=f"docker image inspect returned an unrecognised image id {image_id[:80]!r}"
        )
    return ImageInspection(image_id=image_id, repo_digests=_repo_digests(rest))


def resolve_docker_image(before: ImageInspection, after: ImageInspection) -> dict[str, Any]:
    """Combine the inspections taken before and after ``docker run``.

    ``docker run`` (default ``--pull=missing``) uses the local image when the tag
    already resolves, so the *before* read is what ran. When the tag did not
    resolve yet, ``docker run`` pulled it and the *after* read is what ran. If
    both resolved and disagree, the tag was re-pointed while the workload ran and
    neither read can be attributed to it: the digest is recorded as absent.
    """
    if before.resolved and after.resolved and before.image_id != after.image_id:
        return {
            "unresolved_reason": (
                f"the tag resolved to {before.image_id} before the run and to "
                f"{after.image_id} after it; the image that ran cannot be "
                "attributed to either"
            ),
        }
    chosen = before if before.resolved else after
    if not chosen.resolved:
        reason = after.error or before.error or "the runtime reported no image id"
        return {"unresolved_reason": reason}
    block: dict[str, Any] = {"resolved_by": "docker-image-inspect"}
    if chosen.image_id is not None:
        block["image_id"] = chosen.image_id
    if chosen.repo_digests:
        block["repo_digests"] = list(chosen.repo_digests)
    return block


_DOCKER_PULLABLE = "docker-pullable://"
_DOCKER_SCHEME = "docker://"


def parse_kubernetes_image_id(raw: str) -> dict[str, Any]:
    """Map a pod's ``status.containerStatuses[].imageID`` to the image block.

    containerd and CRI-O report ``<name>@sha256:<hex>`` (a repo digest); the old
    dockershim reported ``docker-pullable://<name>@sha256:<hex>``, or
    ``docker://sha256:<hex>`` (a local image ID) for an image that was never
    pulled from a registry. Any other shape is refused, not guessed at.
    """
    value = raw.strip()
    if not value:
        return {
            "unresolved_reason": (
                "the pod status carried no imageID for the workload container"
            ),
        }
    if value.startswith(_DOCKER_PULLABLE):
        value = value[len(_DOCKER_PULLABLE):]
    elif value.startswith(_DOCKER_SCHEME):
        value = value[len(_DOCKER_SCHEME):]
    if _IMAGE_ID_RE.match(value):
        return {"resolved_by": "kubernetes-pod-status", "image_id": value}
    if _REPO_DIGEST_RE.match(value):
        return {"resolved_by": "kubernetes-pod-status", "repo_digests": [value]}
    return {
        "unresolved_reason": (
            f"the pod status imageID {raw.strip()[:120]!r} is not a sha256 digest"
        ),
    }


def image_block(reference: str, resolution: dict[str, Any]) -> dict[str, Any]:
    """The ``host.runner.image`` block: the reference the operator gave, plus the
    resolution (either the digest fields or ``unresolved_reason``)."""
    return {"reference": reference, **resolution}
