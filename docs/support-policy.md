# Support policy — versions, windows, and what "supported" means

> **Label glossary** (same as the [roadmap](../ROADMAP.md)): **works today** — in effect
> now; **planned** — designed, takes effect at a stated milestone, not in effect yet.

This page is the single source of truth for which NovaFabric versions receive fixes,
for how long, and what an upgrade promises. [`SECURITY.md`](../SECURITY.md#supported-versions)
links here; if the two ever disagree, this page wins and that is a bug
(guarded by `tests/docs/test_support_policy.py`).

## Today — pre-1.0 (works today)

NovaFabric is pre-1.0 and releases frequently. **Only the latest tagged release is
supported** — there is no maintained LTS line before v1.0. Security fixes land on
`main` and ship in the next release; earlier tags receive nothing. If you are on an
older version, the supported action is: upgrade to latest.

| Version | Supported |
|---|---|
| Latest tag | Yes |
| Anything earlier | No — upgrade to latest |

### What upgrading promises (works today)

- **N→N+1 migration compatibility** is a release gate, not an aspiration: every shipped
  database migration must be backward-compatible with the previous minor's code
  (expand-contract; see [release-process.md §0](release-process.md)). Skipping
  versions is *not* covered pre-1.0 — upgrade sequentially through minors, or
  re-migrate from capsules (the capsule store is the source of truth; indexes are
  rebuildable).
- **Old capsules stay readable forever.** Schema changes are additive; every capsule
  carries a `schema_version`. This holds across every upgrade path.

### Runtime support matrix (works today)

| Dependency | Supported |
|---|---|
| Python | ≥ 3.12 (matches `requires-python` in `pyproject.toml`; guarded by test) |
| PostgreSQL (server mode) | 16 (the version CI and the nightly scale tier run) |
| SQLite (local mode) | the version bundled with supported Pythons |
| OS | Linux x86_64/aarch64 (CI-verified); macOS best-effort |

Local mode never requires Postgres or a server.

## At v1.0 — channels, LTS, support windows (planned)

The support-window policy that replaces the table above is designed and takes effect at
the v1.0 capsule-format freeze (the freeze itself is gated on design-partner sign-off —
see the [roadmap](../ROADMAP.md)). In summary, as intent:

- **Two channels:** `current` (every minor, supported until the next minor) and `lts`
  (designated minors, supported **18 months**: security and data-corruption fixes only,
  no features, no dependency majors except CVE remediation).
- **Backports are real releases:** an LTS patch is built by the same pipeline and
  gates as any release.
- **CVE clocks extend to supported LTS versions** (same severity SLAs as
  [`SECURITY.md`](../SECURITY.md)); where a fix cannot be backported without a breaking
  change, the advisory says so and names the mitigation.
- **LTS→next-LTS upgrades are release-gated** on the newer LTS, the same way N→N+1 is
  gated today.

Nothing in this section is in effect before v1.0, and no LTS will be designated
retroactively.

## Deprecations and API sunsets

Interface (as opposed to version) lifecycle is separate and already mechanized:
RFC 9745/8594 deprecation headers, a published register, and a drift gate — see the
[API deprecation policy](decisions.md) (ADR-0188).
