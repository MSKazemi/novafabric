"""The published runtime image must not run as root, and its uid must match
the Helm chart's.

Found 2026-09-04: the chart pinned ``runAsUser: 1000`` / ``runAsNonRoot: true``
while ``deploy/docker/Dockerfile`` defined no user at all — the image ran as
root under plain ``docker run``, and under the chart it ran as a uid that did
not exist in ``/etc/passwd`` writing data dirs owned by root. Two artifacts,
each self-consistent, disagreeing about who may write ``/data`` — pin them to
each other so they can only change together.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "deploy" / "docker" / "Dockerfile"
CHART_VALUES = REPO / "deploy" / "helm" / "novafabric" / "values.yaml"


def test_runtime_image_sets_a_nonroot_user() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    users = re.findall(r"^USER\s+(\S+)", text, re.M)
    assert users, "deploy/docker/Dockerfile has no USER directive — image runs as root"
    assert users[-1] != "root", "final USER directive switches back to root"


def test_user_is_set_after_every_write_to_data() -> None:
    """The chown of /data must happen before USER drops privileges."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    user_pos = text.rindex("\nUSER ")
    chown_pos = text.index("chown -R nova:nova /data")
    assert chown_pos < user_pos, "/data ownership must be fixed before USER drops root"


def test_image_uid_matches_helm_chart_uid() -> None:
    """uid/gid 1000 appears in both artifacts; they must be the SAME value."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    uid_match = re.search(r"useradd --uid (\d+) --gid (\d+)", dockerfile)
    assert uid_match, "Dockerfile no longer creates its user with explicit uid/gid"
    image_uid, image_gid = int(uid_match.group(1)), int(uid_match.group(2))

    values = yaml.safe_load(CHART_VALUES.read_text(encoding="utf-8"))
    pod_ctx = values["podSecurityContext"]
    assert pod_ctx["runAsNonRoot"] is True
    assert pod_ctx["runAsUser"] == image_uid, (
        f"chart runs as uid {pod_ctx['runAsUser']} but the image creates uid "
        f"{image_uid} — the two artifacts have drifted apart again"
    )
    assert pod_ctx["runAsGroup"] == image_gid
    assert pod_ctx["fsGroup"] == image_gid
