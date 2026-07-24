# NovaSeal API Stability Guarantee

**Status:** Works today (ADR-0041, NovaSeal v0.12.15+)

This document defines the NovaSeal API stability contract, breaking-change
definition, and versioning policy.

---

## 1. Stability Tiers

| Surface | Tier | Policy |
|---|---|---|
| `NovaSeal.seal(manifest)` → `SealBundle` | **Stable** | No breaking changes without `NOVASEAL_MAJOR` increment |
| `NovaSeal.verify(capsule_id, seal_dir)` → `VerificationResult` | **Stable** | No breaking changes without `NOVASEAL_MAJOR` increment |
| `SealBundle.dsse_envelope` (bytes) | **Stable** | Format: DSSE per Sigstore spec; no changes without `NOVASEAL_MAJOR` |
| `SealBundle.tsr` (bytes) | **Stable** | Format: RFC 3161 DER; no changes without `NOVASEAL_MAJOR` |
| `SealBundle.log_entry` (dict) | **Stable** | Schema additions are backwards-compatible; removals require `NOVASEAL_MAJOR` |
| `KeyConfig`, `SigningProfile` dataclasses | **Stable** | Additive field additions OK; removals require `NOVASEAL_MAJOR` |
| `load_signing_profile()` return type | **Stable** | |
| `VerificationResult` fields | **Stable** | Additive only until `NOVASEAL_MAJOR` |
| Cap-004: `ToolPermissionEvent` schema | **Stable** | JSON Schema at `schemas/tool-permission-event.schema.json` |
| Cap-001: `RedactionManifest` schema | **Experimental** | OQ-01 unresolved; may change before legal-hold mode is removed |
| Cap-002: `AnnexIVDocument` schema | **Stable** | JSON Schema at `schemas/annex-iv-document.schema.json` |
| Internal modules (`_engine.py`, `merkle.py`) | **Internal** | No stability guarantee |
| CLI commands (`nova verify`, `nova seal`) | **Stable** | Flag renames require deprecation notice ≥1 minor version |

---

## 2. Versioning Policy

NovaSeal follows semantic versioning embedded within the NovaFabric package
version (`MAJOR.MINOR.PATCH`).

An additional `NOVASEAL_MAJOR` counter tracks the cryptographic API version.
It is a **policy convention** (currently `1`) that governs the breaking-change
rules below; it is not (yet) exposed as a code constant. The shipped version of
record is the NovaFabric package version (`novafabric.__version__`); a future
release may surface `NOVASEAL_MAJOR` as an explicit symbol in
`src/novafabric/trust/novaseal/__init__.py`.

### When to increment `NOVASEAL_MAJOR`

`NOVASEAL_MAJOR` MUST be incremented when:

1. **DSSE envelope format changes** — e.g. new required header, payload encoding change.
2. **Signing algorithm change** — e.g. Ed25519 → Ed448 or post-quantum algorithm.
3. **Merkle log format change** — node hashing scheme, tree structure.
4. **`SealBundle` field removal or type change.**
5. **`VerificationResult` field removal or type change** that could cause a verifier to
   accept a previously-invalid seal or reject a previously-valid seal.
6. **`ToolPermissionEvent` schema version change** (removing or renaming required fields).
7. **`RedactionManifest` schema version change** (structural change, not additive).

`NOVASEAL_MAJOR` is NOT incremented for:
- Adding optional fields to `SealBundle`, `VerificationResult`, or `ToolPermissionEvent`.
- Adding new `VerificationResult` check flags (additive).
- Bug fixes that do not change the wire format.
- Performance improvements.

### Migration path for `NOVASEAL_MAJOR` increments

When `NOVASEAL_MAJOR` is incremented:
1. Announce the change in `CHANGELOG.md` under a `### Breaking Changes` header.
2. Provide a migration guide in `docs/migrations/novaseal-vN-to-vM.md`.
3. The old format must remain verifiable for at least two NovaFabric minor releases
   (e.g. if `NOVASEAL_MAJOR` increments at v0.15, old seals must verify until v0.17).
4. Add a `seal_format_version` field to `SealBundle` and `VerificationResult` to allow
   format detection.

---

## 3. Breaking Change Definition

A breaking change is any change that causes:

- A **previously-valid** sealed capsule to fail verification.
- A **previously-invalid** sealed capsule to pass verification.
- A **downstream verifier** to require code changes to handle `SealBundle` output.
- A **key management procedure** to be invalidated without a migration path.
- A **schema validator** (jsonschema against `tool-permission-event.schema.json`
  or `annex-iv-document.schema.json`) to report new errors on previously-valid docs.

Changes that are NOT breaking:
- Adding optional fields to Pydantic models (Pydantic v2 `model_config = {"extra": "ignore"}`).
- Adding new `VerificationResult` check fields with `default=False`.
- Adding new CLI flags (existing usage unchanged).
- Internal refactoring with no wire-format impact.

---

## 4. Deprecation Process

Before removing or renaming any stable surface:

1. Add a `DeprecationWarning` (Python standard) in the affected function/class.
2. Document in `CHANGELOG.md` under `### Deprecated`.
3. Maintain the deprecated surface for at least one NovaFabric minor release cycle.
4. Remove only in a `NOVASEAL_MAJOR` increment release.

---

## 5. Experimental Surfaces

Surfaces marked **Experimental** may change in any release without `NOVASEAL_MAJOR`
increment.  They are documented with `# experimental` or `**experimental**` in
docstrings and this file.

Currently experimental:
- `RedactionManifest` and `redaction-manifest.schema.json` — OQ-01 (GDPR Art.17)
  is unresolved.  The schema may change when legal-hold mode is lifted.
- `PIIDetectionGate.scan()` return type — pending OQ-01 resolution.

---

## 6. References

- ADR-0041: NovaSeal cryptographic core adoption
- ADR-0058: Maker-checker dual-approval with NovaSeal
- ADR-0059: Linked-envelope chain maker-checker
- [Semantic Versioning 2.0](https://semver.org/)
- [DSSE specification](https://github.com/secure-systems-lab/dsse)
