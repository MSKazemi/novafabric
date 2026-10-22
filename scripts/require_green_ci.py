#!/usr/bin/env python3
# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Block a release until CI has passed on the exact commit being released (ADR-0316).

v0.106.0 was tagged, built, signed and published (PyPI, GitHub Release) while CI's
``unit`` job was red on that commit: the four tag-triggered workflows never
looked at CI. Each now ``needs:`` a job that runs this script, which waits for
the ``ci.yml`` run on ``$GITHUB_SHA`` to finish and **fails closed** unless it
concluded ``success``:

* no CI run for the commit (yet)  -> wait out a short grace, then fail
* CI still queued / running       -> keep waiting, up to ``--timeout``
* CI finished, not ``success``    -> fail, naming the run
* CI finished ``success``         -> pass

Stdlib only; talks to the REST API with the job's ``GITHUB_TOKEN`` (needs
``actions: read``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

API = "https://api.github.com"

Fetch = Callable[[str], dict[str, Any]]


@dataclass(frozen=True)
class Verdict:
    """``state`` is ``pass``, ``fail`` or ``wait``."""

    state: str
    message: str


def verdict(runs: list[dict[str, Any]], *, waited: float, grace: float) -> Verdict:
    """Decide from the ``ci.yml`` runs recorded for one commit."""
    if not runs:
        if waited < grace:
            return Verdict("wait", "no CI run for this commit yet")
        return Verdict(
            "fail",
            "no CI run exists for this commit. Release only a commit that was pushed "
            "to main and went through CI.",
        )
    # The newest attempt is the one that counts: a re-run supersedes a failure.
    latest = max(runs, key=lambda r: (r.get("created_at") or "", r.get("id") or 0))
    url = latest.get("html_url", "")
    if latest.get("status") != "completed":
        return Verdict("wait", f"CI is {latest.get('status')}: {url}")
    if latest.get("conclusion") == "success":
        return Verdict("pass", f"CI passed on this commit: {url}")
    return Verdict(
        "fail",
        f"CI concluded {latest.get('conclusion')!r} on this commit: {url}\n"
        "Fix it (or re-run it if it was cancelled or flaked), then re-run this job.",
    )


def _github_fetch(token: str) -> Fetch:
    def fetch(path: str) -> dict[str, Any]:
        request = urllib.request.Request(  # noqa: S310 - fixed https API host
            f"{API}{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return dict(json.load(response))

    return fetch


def wait_for_green(
    fetch: Fetch,
    repo: str,
    sha: str,
    *,
    workflow: str,
    timeout: float,
    grace: float,
    interval: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Verdict:
    start = clock()
    while True:
        data = fetch(f"/repos/{repo}/actions/workflows/{workflow}/runs?head_sha={sha}&per_page=50")
        waited = clock() - start
        result = verdict(list(data.get("workflow_runs", [])), waited=waited, grace=grace)
        if result.state != "wait":
            return result
        if waited >= timeout:
            return Verdict("fail", f"gave up after {timeout:.0f}s — {result.message}")
        print(f"require-green-ci: {result.message}; checking again in {interval:.0f}s", flush=True)
        sleep(interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail unless CI passed on this commit.")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--sha", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--workflow", default="ci.yml")
    parser.add_argument("--timeout", type=float, default=100 * 60)
    parser.add_argument("--grace", type=float, default=5 * 60)
    parser.add_argument("--interval", type=float, default=30)
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN", "")
    if not (args.repo and args.sha and token):
        print("require-green-ci: need --repo, --sha and GITHUB_TOKEN", file=sys.stderr)
        return 2

    result = wait_for_green(
        _github_fetch(token),
        args.repo,
        args.sha,
        workflow=args.workflow,
        timeout=args.timeout,
        grace=args.grace,
        interval=args.interval,
    )
    if result.state == "pass":
        print(f"require-green-ci: {result.message}")
        return 0
    print(f"::error title=Release blocked::{result.message}".replace("\n", "%0A"))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
