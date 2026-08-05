"""Integration test for the `age` docker-compose profile (experimental).

Unlike `tests/deploy/test_compose_profiles.py` (structural YAML checks, no
Docker needed) and `tests/lineage/test_age_backend.py` (testcontainers), this
test stands up the actual compose service defined in
`deploy/docker/docker-compose.yml` and proves `AGELineageStore` can talk to it
end to end — the real proof the wiring works, not just that the YAML parses.

**Opt-in only — set `NOVAFABRIC_RUN_DOCKER_COMPOSE_TESTS=1` to run this file.**
It is skipped by default, including under `make test-fast`/`make test`. This is
a stricter gate than `tests/lineage/test_age_backend.py`'s bare
Docker-availability check, and deliberately so: that testcontainers-backed test
is genuinely lower-risk — testcontainers creates an ephemeral, uniquely-named
container that Ryuk reaps automatically, entirely outside any persistent
compose project. This test instead operates on the *live* compose project's
own named container (`novafabric-age`) and named volume (`age-data`), which is
a fundamentally different risk profile: a routine dev-loop command must never
silently create/destroy containers that could collide with an operator's own
long-running deployment of this same compose file. Hence the explicit env-var
gate on top of the Docker check.

Two more safety measures, both added after a real incident while first
writing this test (see git history / task report for the postmortem):

1. **Isolated compose project** (`-p novafabric-age-test-project`, see
   `_compose()`). Without an explicit `-p`, compose derives the project name
   from the compose file's directory (here, literally `docker`) — the exact
   same namespace an operator's own `docker compose -f
   deploy/docker/docker-compose.yml up` invocation would use. That means the
   `age-data` named volume would land in the *same* volume namespace as that
   operator's `pg-data`/`kuzu-data`. Passing an explicit, distinct project
   name makes that specific cross-contamination (volume namespace collision)
   structurally impossible rather than merely avoided by careful command
   choice.

   **What project isolation does NOT cover, and why that's accepted, not an
   oversight:** `container_name: novafabric-age` in the compose file is a
   literal, project-independent Docker name — by design, only one `age`
   container can exist at a time on a given Docker host, regardless of which
   compose project asked for it. So if an operator already has `make age-up`
   running (main/default project) while this test runs (isolated
   `novafabric-age-test-project`), `_remove_age_container_only()`'s
   `docker stop`/`docker rm novafabric-age` — which, deliberately, addresses
   the container by its literal name rather than through either compose
   project — WILL stop and remove *that operator's* running container, not
   just this test's own. Project isolation fixed the volume-namespace
   incident (point 2 below); it does not and cannot fix this, because the
   collision is on the container name itself, a Docker-level identifier that
   compose projects don't partition. There is no data loss from this either
   way: `age-data` is never deleted (no `-v` anywhere in this file), and
   `make age-up` recreates cleanly against the preserved volume afterward.
   Given that, and that only one `age` instance can meaningfully exist per
   host anyway (there is exactly one AGE lineage graph a developer would be
   working with), this collision is accepted as an intentional
   single-instance constraint rather than something worth engineering around
   here — running this opt-in test while `make age-up` is live elsewhere on
   the same host will interrupt that other session, and that's expected.

2. **Never `docker compose down`.** Teardown uses `docker stop`/`docker rm`
   against the literal `novafabric-age` container name. A first draft of this
   test used `docker compose --profile age down -v`, which is project-scoped,
   not service-scoped: `docker compose --profile age config --services`
   resolves to the *union* `{age, postgres, nova}`, because `postgres`/`nova`
   carry no `profiles:` key and are therefore always "active" under any
   profile filter. That first draft's `down -v` deleted an already-running,
   unrelated dev stack's `novafabric-postgres`/`novafabric-serve` containers
   and their `pg-data`/`kuzu-data` named volumes as a side effect — on a
   *different* project than this test's isolated one, since it ran before the
   `-p` fix existed. Both fixes (isolated project + name-scoped stop/rm) are
   layered defenses against the same class of mistake; keep both, don't
   collapse to the "the project is isolated so plain `down` is now safe"
   shortcut — that would still nuke this test's own `age` container in a way
   that isn't obviously distinguishable from nuking someone else's.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from novafabric.lineage._types import LineageEdge
from novafabric.lineage.backends.sqlite import SqliteLineageStore

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "deploy" / "docker" / "docker-compose.yml"
DSN = "postgresql://nova:nova@localhost:5433/nova_lineage"
CONTAINER_NAME = "novafabric-age"

# Isolated from the "docker" project an operator's own `docker compose -f
# deploy/docker/docker-compose.yml ...` would default to — see module
# docstring point 1. Only affects volume/network naming; `container_name` is
# a literal override regardless of project.
COMPOSE_PROJECT = "novafabric-age-test-project"

OPT_IN_ENV_VAR = "NOVAFABRIC_RUN_DOCKER_COMPOSE_TESTS"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(
            ["docker", "info"], check=True, capture_output=True, timeout=10
        )
    except Exception:
        return False
    return True


def _compose(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            COMPOSE_PROJECT,
            "-f",
            str(COMPOSE_FILE),
            "--profile",
            "age",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )


def _remove_age_container_only() -> None:
    """Stop and remove *only* the `novafabric-age` container, by literal name.

    Deliberately NOT `docker compose down` (even scoped to the isolated test
    project) — see the module docstring for the full reasoning. Never passes
    `-v`, so the named `age-data` volume is intentionally preserved between
    runs (matching this project's `dev-down`/`prod-down`/`make age-down`
    convention of not deleting data volumes by default).
    """
    subprocess.run(
        ["docker", "stop", CONTAINER_NAME], capture_output=True, timeout=30
    )
    subprocess.run(
        ["docker", "rm", "-f", CONTAINER_NAME], capture_output=True, timeout=30
    )


def _wait_healthy(timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    last_status = None
    while time.time() < deadline:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", CONTAINER_NAME],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            last_status = result.stdout.strip()
            if last_status == "healthy":
                return
        time.sleep(2)
    raise TimeoutError(
        f"{CONTAINER_NAME} did not become healthy within {timeout}s "
        f"(last status: {last_status!r})"
    )


@pytest.fixture(scope="module")
def age_compose_dsn():
    if os.environ.get(OPT_IN_ENV_VAR) != "1":
        pytest.skip(
            f"set {OPT_IN_ENV_VAR}=1 to run the age docker-compose integration "
            "test — opt-in only, since it creates/destroys a real "
            "novafabric-age container (not part of the default test run)"
        )
    if not _docker_available():
        pytest.skip("Docker is not available — skipping age compose integration test")

    try:
        import psycopg  # noqa: F401
    except ImportError:
        pytest.skip("psycopg not installed — install the [server] extra")

    # Clean slate: remove any leftover `age` container from a prior failed run
    # before starting fresh (scoped to the single container — see
    # `_remove_age_container_only`'s docstring for why).
    _remove_age_container_only()

    # Every path out of this block — success, `up` failure, health-check
    # timeout, or any other exception (including a subprocess timeout out of
    # `_compose()` itself) — must still remove the container. This is a
    # generator fixture: anything raised before the `yield` never registers a
    # finalizer, so the cleanup has to be a `finally` wrapped around the
    # entire setup sequence, not just the steps after `up` succeeds.
    try:
        up = _compose("up", "-d", "age")
        if up.returncode != 0:
            pytest.skip(f"could not start the age compose service: {up.stderr}")

        try:
            _wait_healthy()
        except TimeoutError as exc:
            logs = subprocess.run(
                ["docker", "logs", CONTAINER_NAME], capture_output=True, text=True, timeout=10
            )
            pytest.fail(f"{exc}\n--- container logs ---\n{logs.stdout}\n{logs.stderr}")

        yield DSN
    finally:
        _remove_age_container_only()


def _run(rid: str) -> dict:
    return {"kind": "run", "run_id": rid}


def _asset(ref: str) -> dict:
    return {"kind": "asset", "asset_ref": ref, "registry": "local"}


def _graph_edges() -> list[LineageEdge]:
    return [
        LineageEdge(
            edge_type="consumed",
            source=_run("01RUNA"),
            target=_asset("model:foo@1.0.0"),
            confidence="high",
            capsule_run_id="01RUNA",
        ),
        LineageEdge(
            edge_type="replayed_from",
            source=_run("01RUNB"),
            target=_run("01RUNA"),
            confidence="high",
            capsule_run_id="01RUNB",
        ),
        LineageEdge(
            edge_type="replayed_from",
            source=_run("01RUNC"),
            target=_run("01RUNB"),
            confidence="high",
            capsule_run_id="01RUNC",
        ),
    ]


def _refs(rows: list[dict]) -> set[str]:
    return {r["ref"] for r in rows}


def _load(store) -> None:
    for edge in _graph_edges():
        store.insert(edge)


@pytest.fixture
def age_store(age_compose_dsn):
    from novafabric.lineage.backends.age import AGELineageStore

    store = AGELineageStore(age_compose_dsn)
    # Isolate this test from any prior run against the same live container.
    store._cypher("MATCH (n) DETACH DELETE n", {})
    _load(store)
    yield store
    store.close()


@pytest.fixture
def sqlite_store(tmp_path: Path):
    store = SqliteLineageStore(db_path=tmp_path / "lineage.db")
    _load(store)
    return store


class TestAgeComposeIntegration:
    """Real proof the `age` compose service is reachable and behaves correctly."""

    def test_provenance_matches_sqlite(self, age_store, sqlite_store) -> None:
        assert _refs(age_store.provenance("01RUNA", depth=5)) == _refs(
            sqlite_store.provenance("01RUNA", depth=5)
        )

    def test_blast_radius_matches_sqlite(self, age_store, sqlite_store) -> None:
        assert _refs(age_store.blast_radius("01RUNA", max_depth=5)) == _refs(
            sqlite_store.blast_radius("01RUNA", max_depth=5)
        )

    def test_replay_chain_matches_sqlite(self, age_store, sqlite_store) -> None:
        age = [r["ref"] for r in age_store.replay_chain("01RUNC")]
        assert age == [r["ref"] for r in sqlite_store.replay_chain("01RUNC")]
        assert age == ["01RUNB", "01RUNA"]

    def test_unknown_run_returns_empty(self, age_store) -> None:
        assert age_store.provenance("nope", depth=5) == []
        assert age_store.blast_radius("nope", max_depth=5) == []
