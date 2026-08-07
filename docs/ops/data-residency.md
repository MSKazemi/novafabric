# Data residency — jurisdiction-aware capsule placement

**Status: experimental** (ADR-0247, first slice). Placement enforcement over
the object capsule store; the cryptographic *proof* layer (jurisdiction site
seals) shipped earlier (v0.83.0) and composes with it.

## What it does today

A `JurisdictionRoutingAdapter` wraps your per-region WORM backends and routes
each tenant's capsules to the backend registered for that tenant's
jurisdiction:

```yaml
# jurisdictions.yaml
tenants:
  acme: EU
  globex: US
allow_cross_read:
  EU: [CH]        # directional: CH readers may read EU data; nothing else may
```

```python
from novafabric.object_capsule_store.jurisdiction_router import (
    JurisdictionRoutingAdapter, load_jurisdiction_config,
)

tenants, policy = load_jurisdiction_config(Path("jurisdictions.yaml"))
store = JurisdictionRoutingAdapter(
    routes={"EU": eu_backend, "US": us_backend},
    tenant_jurisdictions=tenants,
    default=default_backend,
    residency_policy=policy,
)
```

Three rules, enforced not documented:

- **Fail closed at write time.** A tenant mapped to a jurisdiction with no
  registered backend gets a refused write with a typed error — a
  mis-configured region never silently lands elsewhere.
- **Unmapped tenants keep today's behavior** (the default pool). Existing
  single-store deployments observe zero change.
- **Cross-border reads deny by default.** `get_object_checked(key, reader)`
  runs the ADR-0077 residency gate: same-jurisdiction always passes,
  cross-jurisdiction needs a directional grant, denial carries the reason.

## What stays planned

Per-capsule jurisdiction labels (capture-time `--jurisdiction` override of
the tenant default), the `nova lineage --jurisdiction` filter,
`nova storage relocate` with sealed relocation evidence, and wiring the
router into `make_adapter` env configuration — all **planned**, tracked in
ADR-0247.
