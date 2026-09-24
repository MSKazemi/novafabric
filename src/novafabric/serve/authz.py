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
"""Authorization for ``nova serve`` — ADR-0228, first slice.

``nova serve`` authenticated and did not authorize. Possession of the one shared
bearer token granted every endpoint, including ``DELETE /api/runs/{run_id}``,
``POST /api/compliance/pii/erase`` and ``POST /api/admin/roles`` — the last of
which writes the ``role_assignments`` table that ``nova server start`` reads, so
a dashboard credential could mint a server-mode role. This module is the missing
authorization half.

Four decisions from the ADR shape the code, and each is load-bearing:

* **D1/D1a — the scope vocabulary is not new.** ``read``/``operate``/``admin``
  are ADR-0027's Layer A/B/C capability tiers, already applied per-endpoint in
  ``docs/dashboard.md``; ``audit`` is the ``auditor`` role ``server/rbac.Role``
  already defines. Inventing a fourth vocabulary is what guarantees drift.
* **D2 — one declarative table, not 184 decorators.** ADR-0183 froze
  ``serve/app.py``'s inline-route count precisely to stop that module growing.
  A table keyed by ``(method, path_template)`` is reviewable in one diff,
  greppable, and — the reason it wins — *testable for completeness*.
* **D3 — an unclassified route denies.** This is the opposite of the ergonomic
  choice and it is deliberate: defaulting to ``read`` means a forgotten
  classification silently discloses evidence, defaulting to deny means it
  visibly breaks a feature. In an evidence tool a loud failure beats a quiet
  disclosure. :func:`required_scope` returns ``None`` for an unknown route and
  every caller treats ``None`` as deny.
* **D4 — the laptop is unchanged.** The server token holds ``admin``, and an
  issued-token record written before this slice (no ``scope`` field) also holds
  ``admin``. So every credential that exists today is byte-identical in
  behaviour, and authorization only bites once an operator deliberately mints a
  narrower token.

**Deferred, and named so nobody reads silence as coverage:** D6's org/project
override needs a project axis ``serve`` does not have yet (it arrives with
ADR-0229); the ``rbac_store`` role-to-scope mapping is not consulted here, so a
row in ``role_assignments`` grants nothing in ``serve``; and OQ-1 (a
``read``/``read-content`` split), OQ-2 (scope on WebSocket subscriptions) and
OQ-3 (``/metrics`` as its own scope) are all open.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Callable
from typing import Any, Final

logger = logging.getLogger(__name__)

__all__ = [
    "ROUTE_SCOPES",
    "Scope",
    "build_authz_dependency",
    "grantable_scopes",
    "identity_source_for",
    "parse_scope",
    "required_scope",
    "satisfies",
]


class Scope(str, enum.Enum):
    """Capability tiers for the ``serve`` surface.

    ``public`` is deliberately a member rather than a separate structure: one
    table with one value per route is one place to forget something, and two
    would be two. It is nevertheless **not grantable** — it is a property of a
    route ("needs no credential"), never of a credential. The same shape
    ``server/rbac.Role`` uses for ``auditor``: in the enum, out of the chain.
    """

    public = "public"
    read = "read"
    operate = "operate"
    admin = "admin"
    audit = "audit"


#: The cumulative chain, weakest first: ``admin`` ⊇ ``operate`` ⊇ ``read``.
#: ``audit`` is absent on purpose — see :func:`satisfies`.
_CHAIN: Final[tuple[Scope, ...]] = (Scope.read, Scope.operate, Scope.admin)

#: Scopes a credential may actually hold. ``public`` is not one of them.
_GRANTABLE: Final[frozenset[Scope]] = frozenset(
    {Scope.read, Scope.operate, Scope.admin, Scope.audit}
)


def grantable_scopes() -> frozenset[Scope]:
    """The scopes a credential may hold — everything except ``public``."""
    return _GRANTABLE


def parse_scope(value: str | None, *, default: Scope = Scope.admin) -> Scope:
    """Coerce a stored or user-supplied scope string to a grantable :class:`Scope`.

    An unrecognised value returns *default* rather than raising, because this
    runs on the request path against data on disk: a hand-edited or
    future-versioned ``tokens.jsonl`` record must not take the server down. The
    default is ``admin`` only where the caller is reading a *legacy* record, for
    which admin is the truthful reading of what that credential could already
    do (D4). Anywhere a narrower default is correct, pass it.
    """
    if not value:
        return default
    try:
        scope = Scope(value)
    except ValueError:
        logger.warning("serve authz: unrecognised scope %r; using %s", value, default.value)
        return default
    if scope not in _GRANTABLE:
        logger.warning("serve authz: scope %r is not grantable; using %s", value, default.value)
        return default
    return scope


def satisfies(held: Scope, required: Scope) -> bool:
    """Return True if a credential holding *held* may call a route needing *required*.

    The rules, in the order they are checked:

    * a ``public`` route is satisfied by anything, including nothing;
    * ``admin`` is a super-scope and satisfies every requirement — including
      ``audit``. That is not a new rule invented here: ``server/rbac._satisfies``
      already short-circuits on ``Role.admin`` before its ``auditor`` branch, and
      having the two surfaces disagree about what admin means would be worse
      than either answer;
    * ``audit`` satisfies ``audit`` and ``read``, and **nothing else, at any
      level** — the persona must see more evidence than an ordinary reader while
      being structurally incapable of changing anything;
    * otherwise the cumulative chain decides.

    ``held`` is never ``public``: :func:`parse_scope` cannot produce it and no
    credential is issued with it. Passing it anyway is treated as holding
    nothing, which is the safe reading.
    """
    if required is Scope.public:
        return True
    if held is Scope.admin:
        return True
    if held is Scope.audit:
        return required in (Scope.audit, Scope.read)
    if required is Scope.audit:
        # Only audit and admin reach the audit trail; both are handled above.
        return False
    if held not in _CHAIN or required not in _CHAIN:
        return False
    return _CHAIN.index(held) >= _CHAIN.index(required)


# ---------------------------------------------------------------------------
# The route table (D2)
# ---------------------------------------------------------------------------
#
# Every HTTP route this app can mount, keyed by ``(method, path_template)`` —
# the template exactly as FastAPI reports it on ``request.scope["route"].path``,
# router prefixes included. 210 entries at the time of writing: 141 ``read``,
# 29 ``admin``, 28 ``operate``, 7 ``public``, 5 ``audit``.
#
# How a route earned its classification, so the next person can apply the same
# rule instead of guessing:
#
# * ``public`` — the six routes that carry no auth dependency today. Adding
#   auth to any of them would be a behaviour change this slice does not make.
#   (``GET /api/runs/stream`` looks unauthenticated and is not: SSE cannot use
#   a streaming ``Depends``, so it validates the token inline. It is ``read``.)
# * ``admin`` — irreversible, privilege-affecting, key-handling, or it spawns
#   user code: deletion, crypto-shredding, seal bypass, signing, ratchet
#   rotation, role and token management, DB upgrade, and the three real replay
#   modes. Everything under ``/api/admin/`` is admin by prefix, including the
#   harmless-looking ``GET /api/admin/new-run-id``: a uniform prefix rule is one
#   a reviewer can check by eye, and the cost of being wrong in this direction
#   is a broken feature rather than a disclosure.
# * ``audit`` — the response reveals *who did what*: the audit trail itself and
#   the recorded policy decisions. That is the whole rule; it keeps the
#   orthogonal scope tight and means an ``audit`` holder always sees strictly
#   more than a ``read`` holder.
# * ``operate`` — a mutation that is bounded and reversible: register, promote,
#   score, hold, incident, alias, ingest.
# * ``read`` — everything else, **including a POST whose only effect is to
#   compute and return**. Verb is not capability: ``POST /api/query``,
#   ``POST /api/kg/blast-radius`` ("Read-only" in its own docstring),
#   ``POST /api/mcp/scan``, ``POST /api/policy/check`` and the whole
#   ``/api/compliance/export/*`` family (each builds inside a
#   ``TemporaryDirectory`` and returns the bytes — checked, not assumed) change
#   nothing. Classifying them ``operate`` would have locked an auditor out of
#   the exports that are the point of the auditor persona.
#
# A route absent from this table is denied to everyone (D3). That is not a
# failure mode to design around — ``tests/serve/test_authz_route_table.py``
# fails CI on the first unclassified route, so the deny path should never be
# reached in a released build.
ROUTE_SCOPES: Final[dict[tuple[str, str], Scope]] = {
    # The "dashboard static is missing" placeholder, registered only when
    # nobody mounts a static site. It carries no capsule content and answers
    # without a credential, exactly like the landing page it stands in for.
    ("GET", "/"): Scope.public,
    ("GET", "/api/adapters"): Scope.read,
    ("GET", "/api/admin/api-keys"): Scope.admin,
    ("POST", "/api/admin/flush-jwks-cache"): Scope.admin,
    ("GET", "/api/admin/new-run-id"): Scope.admin,
    ("POST", "/api/admin/rebuild-metadata-db"): Scope.admin,
    ("POST", "/api/admin/reindex-runs"): Scope.admin,
    ("GET", "/api/admin/roles"): Scope.admin,
    ("POST", "/api/admin/roles"): Scope.admin,
    ("DELETE", "/api/admin/roles/{subject}/{role}"): Scope.admin,
    ("GET", "/api/admin/tokens"): Scope.admin,
    ("POST", "/api/admin/tokens"): Scope.admin,
    ("DELETE", "/api/admin/tokens/{fingerprint}"): Scope.admin,
    ("POST", "/api/aibom/generate"): Scope.operate,
    ("GET", "/api/aibom/status"): Scope.read,
    ("GET", "/api/alerts/recent"): Scope.read,
    ("GET", "/api/analytics/summary"): Scope.read,
    ("GET", "/api/assets"): Scope.read,
    ("POST", "/api/assets"): Scope.operate,
    ("POST", "/api/assets/register-from-yaml"): Scope.operate,
    ("GET", "/api/assets/{asset_id}"): Scope.read,
    ("GET", "/api/assets/{asset_id}/approvals"): Scope.read,
    ("POST", "/api/assets/{asset_id}/approve"): Scope.operate,
    ("POST", "/api/assets/{asset_id}/eval"): Scope.operate,
    ("GET", "/api/assets/{asset_id}/eval-history"): Scope.read,
    ("POST", "/api/assets/{asset_id}/promote"): Scope.operate,
    ("GET", "/api/assets/{name}/diff"): Scope.read,
    ("POST", "/api/assets/{name}/rollback"): Scope.operate,
    ("DELETE", "/api/assets/{name}/{version}"): Scope.admin,
    ("GET", "/api/assets/{name}/{version}"): Scope.read,
    ("POST", "/api/assets/{name}/{version}/eval"): Scope.operate,
    ("POST", "/api/assets/{name}/{version}/promote"): Scope.operate,
    ("GET", "/api/assure/{run_id}"): Scope.read,
    ("GET", "/api/audit"): Scope.audit,
    ("POST", "/api/capsule-migrate"): Scope.admin,
    ("GET", "/api/compliance/annex-iv"): Scope.read,
    ("POST", "/api/compliance/audit/bundle"): Scope.read,
    ("GET", "/api/compliance/audit/coverage"): Scope.read,
    ("GET", "/api/compliance/audit/map"): Scope.read,
    ("POST", "/api/compliance/audit/report"): Scope.read,
    ("POST", "/api/compliance/audit/verify"): Scope.read,
    ("POST", "/api/compliance/erasure/request"): Scope.admin,
    ("GET", "/api/compliance/erasure/status"): Scope.read,
    ("POST", "/api/compliance/euaiact/export"): Scope.read,
    ("GET", "/api/compliance/euaiact/status"): Scope.read,
    ("POST", "/api/compliance/examiner/{format}"): Scope.read,
    ("POST", "/api/compliance/export/aibom"): Scope.read,
    ("POST", "/api/compliance/export/c2pa"): Scope.read,
    ("POST", "/api/compliance/export/hipaa-proof"): Scope.read,
    ("GET", "/api/compliance/export/kinds"): Scope.read,
    ("POST", "/api/compliance/export/nist-rmf"): Scope.read,
    ("POST", "/api/compliance/export/rocrate"): Scope.read,
    ("POST", "/api/compliance/export/ropa"): Scope.read,
    ("POST", "/api/compliance/export/{kind}"): Scope.read,
    ("GET", "/api/compliance/nis2"): Scope.read,
    ("POST", "/api/compliance/pii/erase"): Scope.admin,
    ("GET", "/api/compliance/subject-proof"): Scope.read,
    ("POST", "/api/cost/attribute"): Scope.read,
    ("POST", "/api/cost/fairness"): Scope.read,
    ("GET", "/api/cost/pricing"): Scope.read,
    ("GET", "/api/cost/report"): Scope.read,
    ("POST", "/api/cost/usage-breakdown"): Scope.read,
    ("POST", "/api/db/upgrade"): Scope.admin,
    ("GET", "/api/diff"): Scope.read,
    ("GET", "/api/docs"): Scope.public,
    ("GET", "/api/doctor"): Scope.read,
    ("POST", "/api/eval/compare"): Scope.read,
    ("POST", "/api/eval/run"): Scope.operate,
    ("GET", "/api/eval/suites"): Scope.read,
    ("GET", "/api/evidence"): Scope.read,
    ("GET", "/api/evidence/completeness/{run_id}"): Scope.read,
    ("GET", "/api/evidence/{bundle_id}"): Scope.read,
    ("GET", "/api/evidence/{bundle_id}/download"): Scope.read,
    ("POST", "/api/evidence/{bundle_id}/verify"): Scope.read,
    ("POST", "/api/evidence/{run_id}"): Scope.operate,
    ("POST", "/api/evidence/{run_id}/bind"): Scope.operate,
    ("GET", "/api/governance/classify"): Scope.read,
    ("POST", "/api/governance/classify-manual"): Scope.operate,
    ("GET", "/api/governance/vocabularies"): Scope.read,
    ("GET", "/api/health"): Scope.public,
    ("GET", "/api/holds"): Scope.read,
    ("POST", "/api/holds"): Scope.operate,
    ("POST", "/api/holds/{hold_id}/release"): Scope.operate,
    ("GET", "/api/incidents"): Scope.read,
    ("POST", "/api/incidents"): Scope.operate,
    ("GET", "/api/incidents/{incident_id}"): Scope.read,
    ("GET", "/api/incidents/{incident_id}/export"): Scope.read,
    ("POST", "/api/incidents/{incident_id}/transition"): Scope.operate,
    ("GET", "/api/infra/backups"): Scope.read,
    ("GET", "/api/infra/collector"): Scope.read,
    ("POST", "/api/ingest-capsule"): Scope.operate,
    ("GET", "/api/kg/agents/{agent_id}/edges"): Scope.read,
    ("GET", "/api/kg/aliases"): Scope.read,
    ("POST", "/api/kg/aliases"): Scope.operate,
    ("POST", "/api/kg/attack-path"): Scope.read,
    ("POST", "/api/kg/blast-radius"): Scope.read,
    ("POST", "/api/kg/detect"): Scope.read,
    ("GET", "/api/kg/entity-queue"): Scope.read,
    ("GET", "/api/kg/entity-queue/stats"): Scope.read,
    ("POST", "/api/kg/entity-queue/{item_id}/approve"): Scope.operate,
    ("POST", "/api/kg/entity-queue/{item_id}/reject"): Scope.operate,
    ("POST", "/api/kg/ingest"): Scope.operate,
    ("POST", "/api/kg/ingest-all"): Scope.operate,
    ("POST", "/api/kg/init"): Scope.admin,
    ("GET", "/api/kg/status"): Scope.read,
    ("GET", "/api/kg/topology"): Scope.read,
    ("GET", "/api/lineage-store/profile"): Scope.read,
    ("GET", "/api/lineage/blast-radius/{ref:path}"): Scope.read,
    ("GET", "/api/lineage/edges"): Scope.read,
    ("POST", "/api/lineage/export-prov"): Scope.read,
    ("POST", "/api/lineage/import"): Scope.operate,
    ("GET", "/api/lineage/provenance/{ref:path}"): Scope.read,
    ("GET", "/api/lineage/replay-chain/{run_id}"): Scope.read,
    ("GET", "/api/lineage/time-travel/{ref:path}"): Scope.read,
    ("GET", "/api/lineage/{run_id}/emit-openlineage"): Scope.read,
    ("POST", "/api/mcp/risk-report"): Scope.read,
    ("POST", "/api/mcp/scan"): Scope.read,
    ("GET", "/api/openapi.json"): Scope.public,
    ("GET", "/api/ops/daemon-status"): Scope.read,
    ("POST", "/api/otlp/v1/traces"): Scope.operate,
    ("GET", "/api/policy/capture-level"): Scope.read,
    ("POST", "/api/policy/capture-level"): Scope.read,
    ("POST", "/api/policy/check"): Scope.read,
    ("GET", "/api/policy/explain"): Scope.read,
    ("GET", "/api/policy/list"): Scope.read,
    ("GET", "/api/policy/recent-decisions"): Scope.audit,
    ("POST", "/api/policy/sign"): Scope.admin,
    ("POST", "/api/policy/test"): Scope.read,
    ("POST", "/api/query"): Scope.read,
    ("GET", "/api/report"): Scope.read,
    ("GET", "/api/reports/alert-digest"): Scope.read,
    ("GET", "/api/reports/api-key-inventory"): Scope.admin,
    ("GET", "/api/reports/capsule-compare"): Scope.read,
    ("GET", "/api/reports/catalog"): Scope.read,
    ("GET", "/api/reports/compliance-posture"): Scope.read,
    ("GET", "/api/reports/cost-burn"): Scope.read,
    ("GET", "/api/reports/dashboard-audit"): Scope.audit,
    ("GET", "/api/reports/eval-regression"): Scope.read,
    ("GET", "/api/reports/evidence-inventory"): Scope.read,
    ("GET", "/api/reports/executive-summary"): Scope.read,
    ("GET", "/api/reports/policy-audit"): Scope.audit,
    ("GET", "/api/reports/release-comparison"): Scope.read,
    ("GET", "/api/reports/run-history"): Scope.read,
    ("GET", "/api/reports/seal-verification"): Scope.read,
    ("GET", "/api/reports/throughput"): Scope.read,
    ("GET", "/api/reports/{report_id}/export"): Scope.read,
    ("GET", "/api/runs"): Scope.read,
    ("GET", "/api/runs/cost-summary"): Scope.read,
    ("GET", "/api/runs/search"): Scope.read,
    ("GET", "/api/runs/stream"): Scope.read,
    ("GET", "/api/runs/suggest-register"): Scope.read,
    ("DELETE", "/api/runs/{run_id}"): Scope.admin,
    ("GET", "/api/runs/{run_id}"): Scope.read,
    ("GET", "/api/runs/{run_id}/children"): Scope.read,
    ("GET", "/api/runs/{run_id}/diagnose"): Scope.read,
    ("GET", "/api/runs/{run_id}/energy"): Scope.read,
    ("POST", "/api/runs/{run_id}/export-system-card"): Scope.admin,
    ("GET", "/api/runs/{run_id}/file/{filepath:path}"): Scope.read,
    ("GET", "/api/runs/{run_id}/forensics-timeline"): Scope.read,
    ("GET", "/api/runs/{run_id}/ledger"): Scope.read,
    ("POST", "/api/runs/{run_id}/redact"): Scope.admin,
    ("GET", "/api/runs/{run_id}/redaction-proof"): Scope.read,
    ("GET", "/api/runs/{run_id}/redaction-xray"): Scope.read,
    ("POST", "/api/runs/{run_id}/replay/dry-run"): Scope.operate,
    ("POST", "/api/runs/{run_id}/replay/exact"): Scope.admin,
    ("POST", "/api/runs/{run_id}/replay/forensic"): Scope.admin,
    ("POST", "/api/runs/{run_id}/replay/semantic"): Scope.admin,
    ("GET", "/api/runs/{run_id}/run-lineage"): Scope.read,
    ("GET", "/api/runs/{run_id}/safety-case"): Scope.read,
    ("GET", "/api/runs/{run_id}/scan-secrets"): Scope.read,
    ("POST", "/api/runs/{run_id}/scores"): Scope.operate,
    ("GET", "/api/runs/{run_id}/tool-permission-events"): Scope.read,
    ("GET", "/api/runs/{run_id}/tree"): Scope.read,
    ("GET", "/api/runs/{run_id}/trust-radar"): Scope.read,
    ("POST", "/api/runs/{run_id}/validate"): Scope.read,
    ("POST", "/api/runs/{run_id}/validate-distributed"): Scope.read,
    ("POST", "/api/runs/{run_id}/verify"): Scope.read,
    ("GET", "/api/schema/list"): Scope.read,
    ("GET", "/api/seal/log/verify"): Scope.read,
    ("GET", "/api/seal/policy"): Scope.read,
    ("POST", "/api/seal/ratchet/init"): Scope.admin,
    ("POST", "/api/seal/ratchet/rotate"): Scope.admin,
    ("GET", "/api/seal/ratchet/status"): Scope.read,
    ("POST", "/api/seal/sigstore/sign"): Scope.admin,
    ("POST", "/api/seal/sigstore/verify"): Scope.read,
    ("POST", "/api/seal/{capsule_id}/bypass"): Scope.admin,
    ("GET", "/api/seal/{capsule_id}/proposals"): Scope.read,
    ("POST", "/api/seal/{capsule_id}/verify"): Scope.read,
    ("GET", "/api/stats"): Scope.read,
    ("GET", "/api/storage/inspect/{run_id}"): Scope.read,
    ("GET", "/api/storage/manifest-chain"): Scope.read,
    ("GET", "/api/storage/stats"): Scope.read,
    ("GET", "/api/storage/validate"): Scope.read,
    ("POST", "/api/topology/seed"): Scope.operate,
    ("GET", "/api/topology/snapshot"): Scope.read,
    ("GET", "/api/tv5/live"): Scope.read,
    ("GET", "/api/tv5/snapshot/{window_id}"): Scope.read,
    ("GET", "/api/tv5/windows"): Scope.read,
    ("POST", "/api/validate-spec"): Scope.read,
    ("GET", "/docs/oauth2-redirect"): Scope.public,
    ("GET", "/livez"): Scope.public,
    ("GET", "/metrics"): Scope.read,
    ("GET", "/metrics/stream"): Scope.read,
    ("GET", "/readyz"): Scope.public,
    ("GET", "/topology/cluster-edges"): Scope.read,
    ("GET", "/topology/cluster-list"): Scope.read,
    ("GET", "/topology/clusters"): Scope.read,
    ("GET", "/v1/kg/audit"): Scope.audit,
    ("GET", "/v1/kg/query"): Scope.read,
    ("GET", "/v1/kg/status"): Scope.read,
}


def required_scope(method: str, path_template: str) -> Scope | None:
    """The scope a route needs, or ``None`` when it is not classified.

    ``None`` means **deny** (D3), never "allow" and never "read". Returning a
    sentinel rather than raising keeps the decision at the call site, where the
    denial can be audited with the route that caused it.
    """
    return ROUTE_SCOPES.get((method.upper(), path_template))


# ---------------------------------------------------------------------------
# Enforcement (D2 hook, D3 deny, D7 audit)
# ---------------------------------------------------------------------------

#: Scope held by the shared ``.serve-token`` and by any issued-token record
#: written before this slice existed. Both could already do everything, so
#: ``admin`` is the truthful reading, and it is what keeps D4's "the laptop is
#: unchanged" promise literally true rather than approximately.
LEGACY_SCOPE: Final[Scope] = Scope.admin


def resolve_scope(candidate: str, *, server_token: str) -> tuple[str, Scope] | None:
    """Map a presented credential to ``(subject, scope)``, or ``None`` if unknown.

    ``None`` means "this is not a credential I recognise" and the caller must
    **defer**, not deny: the route's own ``verify_token`` will answer 401. A 403
    from here for an unauthenticated request would be a worse answer to the
    question the client actually asked (AC6), and it would also leak that the
    route exists.
    """
    from novafabric.serve import token_store
    from novafabric.serve.auth import token_matches

    if server_token and token_matches(candidate, server_token):
        return "local", LEGACY_SCOPE
    record = token_store.find_active(candidate)
    if record is None:
        return None
    subject = str(record.get("fingerprint", "")) or "issued"
    return subject, parse_scope(record.get("scope"), default=LEGACY_SCOPE)


def identity_source_for(subject: str) -> str:
    """How well the actor behind *subject* is known (ADR-0231 D3).

    ``local`` is the one shared ``.serve-token`` that every operator pastes into
    a browser, so it identifies a *session*, never a person — the honest answer
    is ``shared-token``. An issued token is minted deliberately, per-holder, with
    its own fingerprint and scope, so it is ``credential``.

    The distinction is the whole point: an audit log that says ``shared-token``
    is honestly weak evidence, while the same fingerprint under a field named
    ``user`` is dishonestly strong evidence.
    """
    return "shared-token" if subject == "local" else "credential"


def _audit_denial(
    *,
    subject: str,
    method: str,
    path: str,
    required: Scope | None,
    held: Scope,
    identity_source: str = "shared-token",
) -> None:
    """Record one 403 (D7). Never raises — a failed audit must not mask the denial.

    Only the *denial* is logged. A 401 writes nothing, because a 401 is usually
    a stale browser tab and logging the credential that produced it would
    recreate the defect v0.98.0 closed by taking the bearer token off the
    logger. Nothing written here contains the presented secret: ``subject`` is
    either ``"local"`` or a token *fingerprint*.
    """
    try:
        from novafabric.serve import audit

        audit.append(
            action="authz.denied",
            args={"method": method, "route": path},
            cli_equivalent="",
            actor_token_fp=subject,
            result="denied",
            error="insufficient_scope",
            # ADR-0231 D4: first-class, not buried in `args`. A SIEM rule that
            # has to reach into a free-form payload to find the scopes is a rule
            # that breaks the next time the payload shape changes.
            required_scope=required.value if required else "unclassified",
            held_scope=held.value,
            resource=f"{method} {path}",
            identity_source=identity_source,
            actor_id=subject if identity_source != "shared-token" else None,
        )
    except Exception:  # noqa: BLE001 — an audit failure must never grant access
        logger.exception("serve authz: failed to record a 403; the denial still stands")


def build_authz_dependency(server_token: str) -> Callable[..., Any]:
    """Build the single app-level dependency that enforces :data:`ROUTE_SCOPES`.

    One dependency, mounted once on the app, is what D2 buys over 184
    decorators: every route inherits it — including routers included after
    ``create_app`` returns, such as TV-5 — so a new endpoint cannot be added
    *without* enforcement, only without a *classification*, and D3 turns that
    into a loud failure.

    It runs before each route's own ``Depends(verify_token)`` and is careful not
    to pre-empt it: an absent or unrecognised credential returns silently so the
    route answers 401.
    """
    from fastapi import Header, HTTPException, Query, status
    from starlette.requests import HTTPConnection

    from novafabric.serve.auth import extract_bearer

    async def enforce_scope(
        connection: HTTPConnection,
        t: str | None = Query(default=None, alias="token"),
        authorization: str | None = Header(default=None),
    ) -> None:
        # ``HTTPConnection`` rather than ``Request`` because an app-level
        # dependency is attached to **every** route, WebSockets included, and
        # FastAPI hands a WebSocket route a ``WebSocket`` — asking for a
        # ``Request`` there fails with a missing-argument TypeError at connect
        # time. ``HTTPConnection`` is the common base of both and is what makes
        # one dependency legal on both kinds of route.
        scope = connection.scope
        if scope.get("type") != "http":
            # ADR-0228 OQ-2: scope on a long-lived subscription is an open
            # question — mid-stream revocation has no agreed semantics yet — so
            # WebSockets keep exactly the guard they had: `/api/tv5/ws` and
            # `/topology/stream` re-check host and token inline before accept.
            # Standing aside here is the deferral, not an oversight.
            return

        route = scope.get("route")
        path = getattr(route, "path", None)
        method = str(scope.get("method", "")).upper()

        candidate = extract_bearer(authorization) or t
        resolved = resolve_scope(candidate, server_token=server_token) if candidate else None

        if path is None:
            # No matched route means no classification is possible. Nothing
            # should reach a dependency without one, so this is a "cannot
            # happen" — which is exactly the branch that must fail closed
            # rather than fall through to allow.
            if resolved is None:
                return
            subject, held = resolved
            _audit_denial(
                subject=subject,
                method=method,
                path="<unmatched>",
                required=None,
                held=held,
                identity_source=identity_source_for(subject),
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="unclassified route"
            )

        required = required_scope(method, path)

        if required is Scope.public:
            return

        if resolved is None:
            # Unauthenticated, or a credential we do not recognise. Defer: the
            # route's own auth dependency owns the 401.
            return

        subject, held = resolved
        if required is not None and satisfies(held, required):
            return

        _audit_denial(
            subject=subject,
            method=method,
            path=path,
            required=required,
            held=held,
            identity_source=identity_source_for(subject),
        )
        if required is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"route {method} {path} has no authorization classification and is "
                    "denied (ADR-0228 D3); add it to serve.authz.ROUTE_SCOPES"
                ),
            )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"scope '{required.value}' required for {method} {path}; "
                f"credential holds '{held.value}'"
            ),
        )

    # ``from __future__ import annotations`` (PEP 563) stores every annotation
    # as a string, and FastAPI resolves those against the *defining module's*
    # globals. ``HTTPConnection`` is imported inside this factory, so it is not in
    # ``authz``'s globals and FastAPI cannot resolve the name — it falls back to
    # treating ``connection`` as a required query parameter and every route answers
    # 422. Binding the real class here is the fix; the fastapi import stays lazy
    # because ``fastapi`` is the ``[serve]`` extra, not a core dependency, and
    # ``token_store`` imports this module for scope validation.
    enforce_scope.__annotations__["connection"] = HTTPConnection

    return enforce_scope
