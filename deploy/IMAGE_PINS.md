# Pinned third-party container images

Human-readable index of the third-party container images this project has
deliberately pinned to a specific tag. `tests/deploy/test_image_pins.py` is the
actual enforcement mechanism — it parses the files in the "Referenced from"
column and fails if any of them drifts from this table or falls back to
`:latest`. Update both together.

| Image | Pinned tag | Referenced from | Rationale |
|---|---|---|---|
| `janusgraph/janusgraph` | `1.1.0` | `deploy/docker/docker-compose.yml`, `deploy/helm/janusgraph/values.yaml`, `deploy/helm/janusgraph/Chart.yaml` (`appVersion`), `deploy/helm/janusgraph/README.md`, `src/novafabric/lineage/backends/janusgraph.py` (docstrings), `tests/lineage/test_janusgraph.py` (docstring), `tests/lineage/test_janusgraph_backend.py` | Only version with live compose-stack evidence behind it — the `prod` profile already runs `1.1.0`. Confirmed published on Docker Hub 2026-07-30. |
| `edoburu/pgbouncer` | `v1.25.2-p0` | `deploy/docker/docker-compose.yml`, `tests/integration/docker-compose.eval.yaml` | Latest stable release verified on Docker Hub 2026-07-30 (`v1.25.2-p0`/`v1.25.1-p0` published ~2026-06-10). MIT-licensed (Tier A); already the project's chosen replacement for the abandoned `bitnami/pgbouncer` image (unpullable manifests — see `CHANGELOG.md`). |
| `apache/age` | `release_PG16_1.6.0` | `tests/lineage/test_age_backend.py`, `deploy/docker/docker-compose.yml` (`age` profile, experimental) | Matches this project's Postgres 16 baseline (`postgres:16-alpine` in `docker-compose.yml`). AGE tags follow `release_PG<major>_<version>`, not semver — there is no bare `1.6.0` tag. Confirmed published on Docker Hub 2026-07-30. |

## Notes

- This table only tracks *repo-wide infrastructure pins* — a fixed tag that
  ships in a committed compose/Helm file or a testcontainers test. It is not a
  full inventory of every container image referenced anywhere in the repo
  (e.g. `postgres:16-alpine`, `clickhouse/clickhouse-server`, `nats`,
  `apache/kafka` already carry their own explicit version pins and aren't part
  of this drift-prevention pass).
- `src/novafabric/lineage/profiles/janusgraph_minimal.py` (`generate_janusgraph_minimal_profile`)
  is a *generator*, not a pin — it prints a docker-compose YAML for a
  user-chosen deployment, so its three image tags are independently-defaulted
  **function parameters**, not fixed repo state:
  - `janusgraph_tag` defaults to `"1.1.0"`, matching the table above, because
    that is this project's own converged JanusGraph choice and there's no
    reason for the generated profile to suggest anything else by default.
  - `cassandra_tag` and `novafabric_tag` default to `"latest"` deliberately —
    Cassandra and novafabric/novafabric are not tracked pins of *this* repo
    (Cassandra is a third-party dependency this project doesn't version-pin
    anywhere else; `novafabric/novafabric` is this project's own
    self-published image, whose "current" version depends on when a user
    runs the generator, not on a fixed value baked in at edit time).
  - This is intentionally **not** a regression of the bug being fixed here:
    the bug was that *one* tag silently applied to *three unrelated images*.
    The fix is that each image now has its own independent default — the
    fact that two of those defaults happen to be `"latest"` (an appropriate
    default for a *user-invoked generator*, unlike a committed pin) is a
    separate, deliberate choice, not drift.
  - `tests/deploy/test_image_pins.py` never scans `src/**`, so these two
    generator defaults are correctly out of its scope — they are not
    something the drift test could or should flag as `:latest` drift.
