"""Every workflow job runs on an explicit runner image, never a moving ``-latest`` label (#207).

``ubuntu-latest`` moved from 24.04 to 26.04 on 2026-11-07. The new image ships Postgres
client tools 17+, and a pg_restore newer than the ``postgres:16`` service sends
``SET transaction_timeout``, which 16 rejects — the backup/restore tests went red with no
change in this repo. A pinned image changes only when someone edits it, and the pairing
below makes that edit carry the service image with it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((REPO / ".github" / "workflows").glob("*.y*ml"))

# Postgres client major preinstalled on each pinned runner image. Add a row when a job
# moves to a new image, after checking the image's documented PostgreSQL version.
RUNNER_PG_CLIENT_MAJOR = {"ubuntu-24.04": 16}

_PG_IMAGE = re.compile(r"^postgres:(\d+)(?:[.-].*)?$")


def _jobs() -> list[tuple[str, str, dict[str, Any]]]:
    out = []
    for path in WORKFLOWS:
        doc = yaml.safe_load(path.read_text()) or {}
        for name, job in (doc.get("jobs") or {}).items():
            out.append((path.name, name, job or {}))
    return out


JOBS = _jobs()


def test_workflows_were_found() -> None:
    assert len(JOBS) > 20  # guard against a moved directory making every check vacuous


@pytest.mark.parametrize(("wf", "job", "spec"), JOBS, ids=[f"{w}:{j}" for w, j, _ in JOBS])
def test_runner_image_is_pinned(wf: str, job: str, spec: dict[str, Any]) -> None:
    if "uses" in spec:  # a reusable-workflow call has no runner of its own
        return
    runs_on = spec.get("runs-on")
    assert isinstance(runs_on, str), f"{wf}:{job} runs-on must be one literal image, got {runs_on!r}"
    assert not runs_on.endswith("-latest"), (
        f"{wf}:{job} uses the moving label {runs_on!r}; pin an explicit image (#207)"
    )


@pytest.mark.parametrize(("wf", "job", "spec"), JOBS, ids=[f"{w}:{j}" for w, j, _ in JOBS])
def test_postgres_service_matches_runner_client_tools(
    wf: str, job: str, spec: dict[str, Any]
) -> None:
    for svc in (spec.get("services") or {}).values():
        match = _PG_IMAGE.match(str((svc or {}).get("image", "")))
        if not match:
            continue
        server = int(match.group(1))
        runner = spec.get("runs-on")
        assert runner in RUNNER_PG_CLIENT_MAJOR, (
            f"{wf}:{job} runs postgres:{server} on {runner!r}, whose client tools are not "
            "recorded in RUNNER_PG_CLIENT_MAJOR"
        )
        client = RUNNER_PG_CLIENT_MAJOR[runner]
        assert client == server, (
            f"{wf}:{job}: {runner} ships pg_restore {client} but the service is postgres:{server}; "
            "keep the runner's client tools and the service on one major — move them together"
        )
