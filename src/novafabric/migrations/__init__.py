"""Registry-track alembic migration helpers (ADR-0211 D2/D5, experimental).

Two parallel alembic universes exist in NovaFabric:

- the **registry track** — the repo-root ``alembic/{sqlite,postgres}/versions``
  trees managing the registry/server database (the DB the server lifespan
  opens and ``nova backup create --profile pg`` dumps);
- the **MetadataStore track** — ``novafabric/metadata_store/migrations``
  managing the cluster-scale MetadataStore tier.

This package hosts the *registry* track's programmatic entry points and, in
built wheels, the packaged copy of the root migration trees (mapped to
``novafabric/migrations/registry/`` at build time — see
``[tool.hatch.build.targets.wheel.force-include]`` in ``pyproject.toml``).
Without that packaging, an installed deployment could never resolve the script
head and every schema-skew comparison would honestly degrade to ``unknown``
(ADR-0211 D2: "the guard would be theater").
"""
