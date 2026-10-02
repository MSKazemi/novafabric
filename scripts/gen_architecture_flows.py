#!/usr/bin/env python3
"""Generate the animated server-side flow diagrams for docs/architecture/.

Usage:
    python scripts/gen_architecture_flows.py           # (re)write the outputs
    python scripts/gen_architecture_flows.py --check   # exit 1 if any output is stale

One data model, two renderings, so they cannot drift apart:

* ``docs/assets/architecture/<flow>.svg`` — a self-contained animated SVG per
  flow (CSS keyframes + SMIL, no script). Each step lights its boxes, arrows and
  legend line in turn while a packet travels the arrows. Under
  ``prefers-reduced-motion`` the animation stops and the numbered badges plus the
  always-visible step legend carry the same information.
* the ``FLOWS`` data block inside ``docs/architecture/explainer.html`` — the
  explainer renders the same geometry inline and lets a reader step through it.

Deterministic: same data in, byte-identical files out. Edit this file, never the
generated outputs — ``tests/docs/test_architecture_diagrams.py`` fails on drift.
Every step names the code it was checked against; re-verify those references
when the code moves.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from html import escape
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ASSETS = REPO / "docs" / "assets" / "architecture"
EXPLAINER = REPO / "docs" / "architecture" / "explainer.html"
BEGIN = "/* BEGIN generated flows: scripts/gen_architecture_flows.py */"
END = "/* END generated flows */"

WIDTH = 980
SLOT_SECONDS = 3.4  # how long each step stays lit
TITLE_PX, TEXT_PX, MONO_PX = 8.0, 6.4, 6.7  # rough per-character advance widths


@dataclass(frozen=True)
class Node:
    id: str
    x: int
    y: int
    w: int
    h: int
    title: str
    lines: tuple[str, ...]
    color: str
    style: str = "card"  # card | store | warn | sealed
    mono: bool = False


@dataclass(frozen=True)
class Edge:
    id: str
    d: str
    color: str = "muted"
    style: str = "wire"  # wire | observe | blocked | deny
    label: str = ""
    lx: int = 0
    ly: int = 0


@dataclass(frozen=True)
class Step:
    nodes: tuple[str, ...]
    edges: tuple[str, ...]
    caption: str
    detail: str
    ref: str
    mat: str
    badge: str  # node whose top-left corner carries the step number
    color: str


@dataclass(frozen=True)
class Flow:
    id: str
    page: str
    title: str
    subtitle: str
    summary: str
    diagram_h: int
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]
    steps: tuple[Step, ...] = field(default_factory=tuple)

    @property
    def legend_y(self) -> int:
        return self.diagram_h + 14

    @property
    def height(self) -> int:
        return self.legend_y + 40 + 21 * len(self.steps) + 14


# ---------------------------------------------------------------------------
# Flow 1 — OTLP ingest (genai_ingest.py, logs_ingest.py; ADR-0098, ADR-0293)
# ---------------------------------------------------------------------------

OTLP = Flow(
    id="otlp-ingest",
    page="otlp-ingest.md",
    title="OTLP ingest: traces become a new capsule, logs go to a sidecar",
    subtitle="nova serve · POST /api/otlp/v1/traces and /api/otlp/v1/logs (both experimental)",
    summary=(
        "Two OTLP routes on nova serve. Trace exports that carry OTel GenAI spans are "
        "written as a brand-new run capsule. Log exports are appended to a sidecar "
        "log store outside every capsule; a sealed capsule is observed, never written."
    ),
    diagram_h=478,
    nodes=(
        Node("exp", 22, 150, 150, 120, "OTel exporter",
             ("SDK or Collector", "OTLP/HTTP", "JSON or protobuf"), "cap"),
        Node("gate", 206, 150, 160, 120, "nova serve",
             ("token required", "scope: operate", "(ROUTE_SCOPES)"), "seal"),
        Node("tr", 402, 80, 210, 104, "Trace ingest",
             ("keeps gen_ai.* spans", "others: counted as skipped", "none: no capsule"), "rep"),
        Node("newcap", 648, 80, 310, 104, "New capsule  capsules/<new ULID>/",
             ("one per export; never an existing run", "secret-scanned (ADR-0009)",
              "capture_mode: otel-import"), "ver"),
        Node("lg", 402, 236, 210, 116, "Logs ingest",
             ("≤ 16 MiB, ≤ 10,000 records", "metadata only by default",
              "body: type · bytes · sha256"), "amb"),
        Node("side", 648, 236, 310, 116, "Sidecar log store (append-only)",
             ("runs/<run_id>.jsonl", "traces/<trace_id>.jsonl", "unlinked/<YYYY-MM-DD>.jsonl"),
             "amb", style="store", mono=True),
        Node("sealed", 648, 398, 310, 72, "Existing capsule, sealed",
             ("capsules/<run_id>/ with .seal/", "read-only: is_sealed() → capsule_state"),
             "seal", style="sealed"),
        Node("resp", 206, 398, 406, 72, "Response (logs)",
             ("capsule_amended: false · storage: otlp-log-sidecar",
              "past 64 MiB per stream: partialSuccess.rejectedLogRecords"), "ver"),
    ),
    edges=(
        Edge("t1", "M172 186 H204", "rep"),
        Edge("t2", "M366 186 C386 186 382 132 400 132", "rep"),
        Edge("t3", "M612 132 H646", "ver"),
        Edge("l1", "M172 236 H204", "amb"),
        Edge("l2", "M366 236 C384 236 384 294 400 294", "amb"),
        Edge("l3", "M612 294 H646", "amb"),
        Edge("l4", "M560 352 V434 H646", "seal", "observe", "observe only", 566, 382),
        Edge("nw", "M900 352 V396", "warn", "blocked", "✕ no write", 908, 380),
        Edge("l5", "M450 352 V396", "ver"),
    ),
    steps=(
        Step(("exp", "gate"), ("t1",),
             "Traces: the exporter POSTs to /api/otlp/v1/traces; the route needs a token "
             "holding scope operate.",
             "<p>The route accepts an <code>ExportTraceServiceRequest</code> as OTLP/HTTP "
             "JSON, or protobuf when <code>Content-Type</code> is "
             "<code>application/x-protobuf</code>. Like every serve route it passes the "
             "ADR-0228 scope check first; both OTLP routes are classified "
             "<code>operate</code>.</p>",
             "serve/app.py:otlp_ingest_traces · serve/authz.py:ROUTE_SCOPES",
             "experimental", "gate", "rep"),
        Step(("tr",), ("t2",),
             "Only spans carrying a gen_ai.* attribute become events; the rest are counted "
             "as skipped, never guessed at.",
             "<p><code>ingest_otlp_json</code> / <code>ingest_otlp_protobuf</code> map GenAI "
             "spans to model and tool calls. If the export holds no GenAI span the response "
             "says so and <strong>no capsule is written</strong>.</p>",
             "otel/genai_ingest.py:ingest_otlp_json",
             "experimental", "tr", "rep"),
        Step(("newcap",), ("t3",),
             "write_ingest_capsule creates a new capsule under a fresh ULID; no existing "
             "capsule is ever opened for writing.",
             "<p>It reuses the native <code>CapsuleWriter</code>, environment lock, secret "
             "scanner and replay policy, and records <code>capture_mode: otel-import</code> "
             "and <code>capture_level: ingested-otlp</code>. This path does not apply a "
             "NovaSeal signature.</p>",
             "otel/genai_ingest.py:write_ingest_capsule",
             "experimental", "newcap", "ver"),
        Step(("exp", "gate", "lg"), ("l1", "l2"),
             "Logs: the exporter POSTs to /api/otlp/v1/logs (scope operate); over 16 MiB "
             "or 10,000 records is a 400 and nothing is written.",
             "<p>The body is decoded from JSON or protobuf. By default only metadata is "
             "kept: times, severity, ids, <code>service.name</code>, attribute keys and the "
             "body's type, length and SHA-256. <code>NOVAFABRIC_OTLP_LOGS_STORE_BODY=1</code> "
             "also keeps redacted, truncated text.</p>",
             "otel/logs_ingest.py:ingest_otlp_logs_body · register_otlp_logs_route",
             "experimental", "lg", "amb"),
        Step(("side",), ("l3",),
             "Each record is routed by link key: novafabric.run_id, else a valid traceId, "
             "else the UTC day of the record.",
             "<p>The sidecar root is <code>$NOVAFABRIC_OTLP_LOG_DIR</code> (default "
             "<code>$NOVAFABRIC_HOME/otlp-logs</code>). Directories are created 0700 and a "
             "symlinked path is refused.</p>",
             "otel/logs_ingest.py:ingest_otlp_logs · _secure_dir",
             "experimental", "side", "amb"),
        Step(("sealed",), ("l4", "nw"),
             "A run-linked record gets the capsule's observed state (sealed, unsealed or "
             "absent) stamped on it. The capsule is never written.",
             "<p><code>_capsule_state</code> only checks for <code>capsule.yaml</code> and "
             "<code>.seal/</code>. A sealed capsule stays byte-identical, so "
             "<code>nova verify</code> keeps passing; the response lists the run under "
             "<code>sealed_runs</code>.</p>",
             "otel/logs_ingest.py:_capsule_state · capsule/_manifest_write.py:is_sealed",
             "experimental", "sealed", "seal"),
        Step(("resp",), ("l5",),
             "Lines are appended with O_APPEND under flock; records past a stream's 64 MiB "
             "cap are rejected and counted in partialSuccess.",
             "<p>Rejection is reported, never silent: "
             "<code>partialSuccess.rejectedLogRecords</code> carries the count. The response "
             "always says <code>capsule_amended: false</code>: the sidecar is correlation "
             "data, not sealed evidence.</p>",
             "otel/logs_ingest.py:_append_lines · LogIngestResult.to_response",
             "experimental", "resp", "ver"),
    ),
)

# ---------------------------------------------------------------------------
# Flow 2 — nova serve request path (serve/authz.py, serve/tenancy.py, ADR-0228/0229/0231)
# ---------------------------------------------------------------------------

SERVE = Flow(
    id="serve-request-path",
    page="serve-request-path.md",
    title="nova serve: from request to handler",
    subtitle=(
        "Host guard → scope table (ADR-0228) → handler, "
        "with the tenancy gate (ADR-0229) at start-up"
    ),
    summary=(
        "How nova serve decides whether a request may run. Start-up refuses multi-tenant "
        "mode while any read-path store is tenant-unsafe. Each request passes a Host "
        "guard, then one app-level dependency that maps the credential to a scope and "
        "the route to its required scope. Unclassified routes and insufficient scopes "
        "get 403 and an audit record."
    ),
    diagram_h=482,
    nodes=(
        Node("start", 22, 76, 300, 112, "Start-up: tenancy gate",
             ("assert_multi_tenant_ready()", "NOVAFABRIC_SERVE_TENANCY=multi",
              "and an unsafe store ⇒ exit 2", "default: single-tenant"), "seal"),
        Node("stores", 358, 76, 600, 112, "Read-path stores (STORE_TENANCY)",
             ("aware: metadata_store · evidence_fabric · cost_store · object_capsule_store",
              "agnostic: capsule_dir (isolated by the deployment)",
              "unsafe: runs_cache · knowledge_graph · lineage_store",
              "posture reported by GET /api/doctor → tenancy_posture"), "seal"),
        Node("client", 22, 230, 130, 104, "Client",
             ("Bearer header", "or ?token=", "browser / curl"), "cap"),
        Node("host", 182, 230, 140, 104, "Host guard",
             ("middleware", "Host must be", "localhost"), "rep"),
        Node("resolve", 352, 230, 176, 104, "resolve_scope",
             ("server token → admin", "issued token → its", "scope · unknown → defer"), "seal"),
        Node("table", 558, 230, 176, 104, "ROUTE_SCOPES",
             ("(method, template)", "public · read · operate", "admin · audit · None"),
             "seal"),
        Node("sat", 764, 230, 194, 104, "satisfies(held, req)",
             ("admin ⊇ operate ⊇ read", "audit: audit + read only", "None: denied to all"),
             "seal"),
        Node("hostdeny", 182, 384, 140, 86, "403",
             ("host_not_localhost", "before any", "scope check"), "warn", style="warn"),
        Node("deny", 352, 384, 382, 86, "403 + one audit record",
             ("action authz.denied · required_scope · held_scope",
              "identity_source: shared-token | credential",
              "→ $NOVAFABRIC_HOME/dashboard-audit.jsonl"), "warn", style="warn"),
        Node("handler", 764, 384, 194, 86, "Route handler",
             ("own verify_token check:", "401 if missing/invalid", "then the endpoint runs"),
             "ver"),
    ),
    edges=(
        Edge("s1", "M322 132 H356", "seal"),
        Edge("r1", "M152 282 H180", "rep"),
        Edge("hd", "M252 334 V382", "warn", "deny"),
        Edge("r2", "M322 282 H350", "seal"),
        Edge("r3", "M528 282 H556", "seal"),
        Edge("df", "M440 230 V208 H972 V427 H960", "ver", "observe",
             "public route, or unknown credential: straight to the route", 560, 220),
        Edge("r4", "M734 282 H762", "seal"),
        Edge("dn", "M800 334 V358 H560 V382", "warn", "deny"),
        Edge("ok", "M900 334 V382", "ver"),
    ),
    steps=(
        Step(("start", "stores"), ("s1",),
             "Start-up: NOVAFABRIC_SERVE_TENANCY=multi exits with code 2 while any read-path "
             "store is unsafe. The default is single-tenant.",
             "<p>The check runs before the token is minted or a socket is bound, and the "
             "error names every blocking store. Three are unsafe today, including the runs "
             "index behind <code>/api/runs</code>, so multi-tenant <code>serve</code> is "
             "refused. Use <code>nova server</code> for multi-tenant deployments.</p>",
             "cli/serve.py · serve/tenancy.py:assert_multi_tenant_ready · STORE_TENANCY",
             "experimental", "start", "seal"),
        Step(("client", "host", "hostdeny"), ("r1", "hd"),
             "Every HTTP request first meets the Host guard: a Host header that is not "
             "localhost gets 403 host_not_localhost.",
             "<p>This is the DNS-rebinding defence. It is middleware, so it runs before any "
             "dependency, and it does not write an audit record.</p>",
             "serve/app.py:host_header_guard · serve/auth.py:is_localhost_host",
             "works today", "host", "rep"),
        Step(("resolve",), ("r2",),
             "One app-level dependency, enforce_scope, maps the credential: the server token "
             "holds admin, an issued token its minted scope.",
             "<p>A token issued before scopes existed reads as <code>admin</code>, so a "
             "laptop behaves exactly as before. WebSocket routes are skipped here and keep "
             "their inline host and token checks (ADR-0228 OQ-2).</p>",
             "serve/authz.py:build_authz_dependency · resolve_scope",
             "experimental", "resolve", "seal"),
        Step(("table", "handler"), ("r3", "df"),
             "required_scope looks up (method, route template). A public route or an "
             "unknown credential goes on to the route, which answers 401.",
             "<p>Deferring an unknown credential keeps the answer a 401 rather than a 403, "
             "which would also reveal that the route exists. The table is one dict, so a "
             "test can check that every mounted route is classified.</p>",
             "serve/authz.py:required_scope · ROUTE_SCOPES",
             "experimental", "table", "seal"),
        Step(("sat", "deny"), ("r4", "dn"),
             "If the held scope does not satisfy the route, or the route is unclassified, "
             "the answer is 403 and one authz.denied record is appended.",
             "<p>An unclassified route is denied to everyone, the server token included "
             "(ADR-0228 D3). The record names both scopes and states how well it knows the "
             "actor: <code>shared-token</code> for the one <code>.serve-token</code>, "
             "<code>credential</code> for an issued token (ADR-0231). An audit failure never "
             "turns a denial into an allow.</p>",
             "serve/authz.py:satisfies · _audit_denial → serve/audit.py:append",
             "experimental", "sat", "warn"),
        Step(("handler",), ("ok",),
             "Scope satisfied: the route's own verify_token checks the credential again and "
             "the endpoint runs.",
             "<p>Authorization says what a credential may do. Tenancy is the separate "
             "start-up gate above: single-tenant <code>serve</code> does not filter rows "
             "by tenant.</p>",
             "serve/app.py:verify_token",
             "experimental", "handler", "ver"),
    ),
)

# ---------------------------------------------------------------------------
# Flow 3 — encryption at rest (trust/envelope_encryption.py, object_capsule_store/)
# ---------------------------------------------------------------------------

CRYPTO = Flow(
    id="encryption-at-rest",
    page="encryption-at-rest.md",
    title="Encryption at rest: envelope v2 write and fail-closed read",
    subtitle="Object capsule store, opt-in (ADR-0185, ADR-0290, ADR-0295; experimental)",
    summary=(
        "The encrypting adapter around the WORM object store. Writes produce a v2 "
        "envelope whose AES-GCM associated data is the object key. Reads refuse "
        "plaintext unless it is chain-log data, globally allowed, or pinned in a "
        "digest-checked legacy inventory; strict mode also refuses unbound v1 envelopes."
    ),
    diagram_h=568,
    nodes=(
        Node("w0", 22, 78, 132, 104, "Write",
             ("put_object(key,", "data, sha256)"), "cap", mono=True),
        Node("w1", 174, 78, 150, 104, "CAS check",
             ("sha256(plaintext)", "must match caller", "else CASMismatch"), "rep"),
        Node("w2", 344, 78, 198, 104, "encrypt_blob",
             ("fresh DEK + nonce", "AES-256-GCM", "AAD = v2 domain ‖ key"), "seal"),
        Node("w3", 562, 78, 180, 104, "Wrap the DEK",
             ("KEK.wrap_key(dek)", "per-tenant KEK when", "<tenant>.kek exists"), "seal"),
        Node("w4", 762, 78, 196, 104, "WORM put",
             ("envelope_version: 2", "sha256 of stored bytes", "verify needs no key"), "ver"),
        Node("r0", 22, 250, 132, 104, "Read",
             ("get_object(key)", "raw stored bytes"), "cap", mono=True),
        Node("r1", 174, 250, 150, 104, "Envelope?",
             ("schema marker", "fields + algo tag"), "rep"),
        Node("r3", 344, 250, 198, 104, "Strict mode",
             ("v1 (unbound) envelope", "+ REFUSE_V1_ENVELOPES", "+ not pinned ⇒ refuse"),
             "amb"),
        Node("r4", 562, 250, 180, 104, "decrypt_blob",
             ("shredded? refuse", "sha256(ciphertext) ok?", "unwrap DEK with KEK"), "seal"),
        Node("r5", 762, 250, 196, 104, "AES-GCM open",
             ("AAD = v2 domain ‖ key", "being read; a moved", "copy fails to open"), "seal"),
        Node("plain", 22, 404, 302, 128, "Not an envelope: checked in order",
             ("1 _capsule_log/ key → returned", "2 ALLOW_PLAINTEXT_READS → returned",
              "3 inventory pins key + sha256 → returned", "4 else PlaintextObjectRefusedError"),
             "warn", style="warn"),
        Node("legacy", 344, 404, 198, 86, "Refused",
             ("LegacyEnvelope-", "RefusedError, before", "any key unwrap"), "warn",
             style="warn"),
        Node("errs", 562, 404, 180, 86, "Named failures",
             ("ShreddedBlobError", "CiphertextIntegrityError", "DekUnwrapError"), "warn",
             style="warn"),
        Node("ok", 762, 404, 196, 86, "Plaintext returned",
             ("v2: the AAD matched", "v1: decrypts and logs", "a warning (unbound)"),
             "ver"),
        Node("boot", 344, 504, 614, 52, "Start-up (backend_router.make_adapter)",
             ("legacy inventory loaded once; missing, malformed or "
              "pin-mismatched ⇒ refuse to start",),
             "amb"),
    ),
    edges=(
        Edge("ew1", "M154 130 H172", "rep"),
        Edge("ew2", "M324 130 H342", "seal"),
        Edge("ew3", "M542 130 H560", "seal"),
        Edge("ew4", "M742 130 H760", "ver"),
        Edge("er1", "M154 302 H172", "rep"),
        Edge("eno", "M249 354 V402", "warn", "deny", "no", 256, 382),
        Edge("er2", "M324 302 H342", "amb", label="yes", lx=326, ly=294),
        Edge("eleg", "M443 354 V402", "warn", "deny"),
        Edge("er3", "M542 302 H560", "seal"),
        Edge("eerr", "M652 354 V402", "warn", "deny"),
        Edge("er4", "M742 302 H760", "seal"),
        Edge("eok", "M860 354 V402", "ver"),
    ),
    steps=(
        Step(("w0", "w1"), ("ew1",),
             "put_object first checks the caller's SHA-256 over the plaintext, so a CAS "
             "mismatch fails before any cryptography runs.",
             "<p>Encryption is opt-in: <code>NOVA_OBJECT_STORE_ENCRYPTION=1</code> plus "
             "<code>NOVA_OBJECT_STORE_KEK_PATH</code>. Without both, the adapter is not "
             "installed and stored bytes are unchanged.</p>",
             "object_capsule_store/encryption_wrapper.py:EncryptingAdapter._encrypt",
             "experimental", "w1", "rep"),
        Step(("w2",), ("ew2",),
             "encrypt_blob makes a fresh 256-bit DEK and 96-bit nonce and seals with "
             "AES-256-GCM, authenticating the object key as associated data.",
             "<p>The AAD is a v2 domain separator followed by the UTF-8 object key. It "
             "deliberately excludes the KEK reference, so re-wrapping a DEK under a new KEK "
             "does not require re-encrypting the payload.</p>",
             "trust/envelope_encryption.py:encrypt_blob · envelope_aad",
             "experimental", "w2", "seal"),
        Step(("w3", "w4"), ("ew3", "ew4"),
             "The KEK wraps the DEK; the envelope JSON is WORM-written and hashed as "
             "ciphertext, so integrity checks never need the key.",
             "<p><code>content_sha256</code> and the checksum handed to the backend are "
             "computed over ciphertext (encrypt-before-WORM). The KEK itself never enters "
             "the envelope.</p>",
             "encryption_wrapper.py:put_object · trust/tenant_keys.py",
             "experimental", "w3", "seal"),
        Step(("r0", "r1", "plain", "boot"), ("er1", "eno"),
             "On read, bytes that are not an envelope are refused unless chain-log data, "
             "globally allowed, or pinned by the legacy inventory.",
             "<p>The reader cannot tell \"written before encryption\" from \"plaintext "
             "substituted by someone with write access\", so it fails closed with "
             "<code>PlaintextObjectRefusedError</code>. The ADR-0295 inventory admits a "
             "listed key only while its stored bytes still hash to the pinned SHA-256.</p>",
             "encryption_wrapper.py:get_object · legacy_inventory.py:LegacyInventory.admits",
             "experimental", "r1", "warn"),
        Step(("r3", "legacy"), ("er2", "eleg"),
             "Strict mode refuses an unbound v1 envelope that the inventory does not pin, "
             "before any key is unwrapped.",
             "<p><code>NOVA_OBJECT_STORE_REFUSE_V1_ENVELOPES=1</code> closes the gap "
             "ADR-0290 left: an old v1 envelope copied onto a new key. It is off by default; "
             "without it a v1 envelope still decrypts and logs a warning.</p>",
             "encryption_wrapper.py:get_object · LegacyEnvelopeRefusedError",
             "experimental", "r3", "amb"),
        Step(("r4", "errs"), ("er3", "eerr"),
             "decrypt_blob refuses a shredded envelope, checks the ciphertext hash, then "
             "unwraps the DEK; each failure has a named error.",
             "<p>The hash check runs before any key material is touched. Any backend unwrap "
             "failure, local or cloud KMS, becomes <code>DekUnwrapError</code> without "
             "leaking SDK internals.</p>",
             "trust/envelope_encryption.py:decrypt_blob",
             "experimental", "r4", "seal"),
        Step(("r5", "ok"), ("er4", "eok"),
             "AES-GCM opens with the key being read as associated data; an envelope copied "
             "to another key fails with BlobAuthenticationError.",
             "<p>Rewriting <code>envelope_version</code> from 2 to 1 does not help an "
             "attacker: the ciphertext was sealed with the AAD and fails authentication "
             "without it. Read counters live in process memory and reset on restart.</p>",
             "trust/envelope_encryption.py:decrypt_blob · EncryptingAdapter.read_counters",
             "experimental", "r5", "ver"),
    ),
)

# ---------------------------------------------------------------------------
# Flow 4 — server data plane (server/; ADR-0206, ADR-0208, ADR-0294)
# ---------------------------------------------------------------------------

DATA = Flow(
    id="server-data-plane",
    page="server-data-plane.md",
    title="Server data plane: admission, budgets and keyset pages",
    subtitle=(
        "nova server /v0 API (experimental): ADR-0294 binding and budgets, "
        "ADR-0206 keyset pagination"
    ),
    summary=(
        "Three paths through the nova server REST API. Admission: an API key bound to a "
        "workspace is refused outside it when enforcement is on. Upload: global, "
        "workspace and org budgets are checked and the strictest wins. Listing: an "
        "opaque keyset cursor seeks past the last row served."
    ),
    diagram_h=548,
    nodes=(
        Node("a0", 22, 76, 150, 104, "API client",
             ("Bearer nvfk_…", "may declare a", "workspace"), "cap"),
        Node("a1", 192, 76, 176, 104, "Authenticate",
             ("API key → AuthContext", "may carry a workspace", "binding (ADR-0178)"), "seal"),
        Node("a2", 388, 76, 196, 104, "enforce_key_binding",
             ("opt-in; bound keys only", "bound workspace exists?", "declared = binding?"),
             "seal"),
        Node("a3", 604, 76, 200, 104, "403",
             ("workspace_binding_invalid", "workspace_binding_mismatch",
              "audit ≤ 1 per 60 s per key"), "warn", style="warn"),
        Node("a4", 824, 76, 134, 104, "require_role",
             ("reader: list", "writer: upload"), "ver"),
        Node("b0", 22, 226, 170, 104, "POST /v0/capsules",
             ("capsule upload", "(an ingest route)"), "cap"),
        Node("b1", 212, 226, 220, 104, "enforce_storage_quota",
             ("global · workspace · org", "all checked; strictest wins", "inert unless configured"),
             "amb"),
        Node("b3", 452, 226, 300, 104, "Write the capsule",
             ("soft limit: still written, plus", "X-NovaFabric-Quota-Warning header"), "amb"),
        Node("b4", 772, 226, 186, 104, "Meter the write",
             ("usage ledger row", "invalidate workspace", "and org usage cache"), "ver"),
        Node("b2", 212, 346, 220, 60, "429 quota_exceeded",
             ("no Retry-After · details.org",), "warn", style="warn"),
        Node("c0", 22, 424, 170, 104, "GET /v0/capsules",
             ("?limit (≤ 500)", "?cursor=<opaque>"), "cap"),
        Node("c1", 212, 424, 220, 104, "parse_cursor (strict)",
             ("none → first page", "v1 → seek key [created_at, id]", "bad → 400 invalid_cursor"),
             "rep"),
        Node("c2", 452, 424, 300, 104, "query_runs(limit + 1, after=key)",
             ("WHERE (created_at, run_id) < (?, ?)", "   OR created_at IS NULL",
              "ORDER BY created_at DESC, run_id DESC"), "rep", mono=True),
        Node("c3", 772, 424, 186, 104, "Page",
             ("limit rows", "next_cursor if a +1 row", "total: first page only"), "ver"),
    ),
    edges=(
        Edge("ea1", "M172 128 H190", "seal"),
        Edge("ea2", "M368 128 H386", "seal"),
        Edge("ea3", "M584 112 H602", "warn", "deny"),
        Edge("ea4", "M486 180 V198 H891 V182", "ver",
             label="unbound key, or declared workspace matches", lx=600, ly=213),
        Edge("eb1", "M192 278 H210", "amb"),
        Edge("eb2", "M322 330 V344", "warn", "deny"),
        Edge("eb3", "M432 278 H450", "amb"),
        Edge("eb4", "M752 278 H770", "ver"),
        Edge("ec1", "M192 476 H210", "rep"),
        Edge("ec2", "M432 476 H450", "rep"),
        Edge("ec3", "M752 476 H770", "ver"),
    ),
    steps=(
        Step(("a0", "a1"), ("ea1",),
             "A /v0 request authenticates; an API key resolves to an AuthContext that may "
             "carry a workspace binding.",
             "<p>The binding was attribution-only (ADR-0208 metering). It is enforced at "
             "request time only when <code>api_keys.enforce_workspace_binding</code> is on. "
             "It is off by default.</p>",
             "server/auth.py · server/key_binding.py:enforce_key_binding",
             "experimental", "a1", "seal"),
        Step(("a2", "a3"), ("ea2", "ea3"),
             "With enforcement on, a bound key whose workspace does not exist, or that "
             "names another workspace, gets 403 and an audit entry.",
             "<p>The declared workspace comes from the <code>X-NovaFabric-Workspace</code> "
             "header or the <code>workspace</code> query parameter. An unreadable workspace "
             "store fails closed. Refusals are audited as <code>api_key.binding_refused</code>, "
             "at most once per (subject, reason, requested) per 60 s.</p>",
             "server/key_binding.py:enforce_key_binding · _audit_refusal",
             "experimental", "a2", "warn"),
        Step(("a4",), ("ea4",),
             "Unbound keys, non-key credentials and matching requests go on to the route's "
             "role check.",
             "<p>Binding scopes what a key may <em>name</em>; it does not narrow list results. "
             "The capsule store is not partitioned by workspace (ADR-0178).</p>",
             "server/routes/capsules.py:require_role",
             "experimental", "a4", "ver"),
        Step(("b0", "b1", "b2"), ("eb1", "eb2"),
             "Upload: global, workspace and org budgets are all checked; any hard limit "
             "rejects with 429 quota_exceeded.",
             "<p>Org usage is the sum of the metered counters of the org's workspaces "
             "(ADR-0294). Quota does not decay on a clock, so the 429 has no "
             "<code>Retry-After</code>. An unknown org slug in config refuses start-up.</p>",
             "server/quotas.py:enforce_storage_quota · OrgQuotaChecker",
             "experimental", "b1", "amb"),
        Step(("b3", "b4"), ("eb3", "eb4"),
             "Soft limits let the write through with an X-NovaFabric-Quota-Warning header; "
             "the upload is metered and cached usage invalidated.",
             "<p>Metering never fails the upload: an accounting error is logged and "
             "audited instead. Budgets read cached usage with a short TTL, so the cache is "
             "invalidated after each write.</p>",
             "server/routes/capsules.py:_record_usage_capsule_upload · server/usage.py",
             "experimental", "b3", "amb"),
        Step(("c0", "c1"), ("ec1",),
             "List: the cursor is strictly decoded; a bad cursor is 400 invalid_cursor, not "
             "a silent restart at page one.",
             "<p>A legacy <code>{\"offset\": N}</code> cursor is still served by the old path "
             "with a <code>Deprecation: true</code> header, or refused once "
             "<code>pagination.legacy_offset_cursors</code> is off (ADR-0188).</p>",
             "server/pagination.py:parse_cursor · server/routes/capsules.py:list_capsules",
             "experimental", "c1", "rep"),
        Step(("c2", "c3"), ("ec2", "ec3"),
             "The index seeks past (created_at, run_id), fetches limit + 1 to detect more, "
             "and encodes the last row as next_cursor.",
             "<p>The cursor is base64url JSON <code>{\"v\": 1, \"k\": [created_at, "
             "run_id]}</code>. A seek costs O(page), not O(offset). Rows with no "
             "<code>created_at</code> sort last and are paged by <code>run_id</code>. "
             "Later pages omit <code>total</code>, which would need the scan keyset "
             "avoids.</p>",
             "registry/runs_cache.py:query_runs · server/pagination.py:encode_keyset_cursor",
             "experimental", "c2", "ver"),
    ),
)

FLOWS: tuple[Flow, ...] = (OTLP, SERVE, CRYPTO, DATA)


# ---------------------------------------------------------------------------
# Validation — keep labels inside their boxes and references resolvable
# ---------------------------------------------------------------------------


def validate(flow: Flow) -> None:
    node_ids = {n.id for n in flow.nodes}
    edge_ids = {e.id for e in flow.edges}
    for n in flow.nodes:
        room = n.w - 24
        if len(n.title) * TITLE_PX > room:
            raise ValueError(f"{flow.id}/{n.id}: title too wide: {n.title!r}")
        per = MONO_PX if n.mono else TEXT_PX
        for line in n.lines:
            if len(line) * per > room:
                raise ValueError(f"{flow.id}/{n.id}: line too wide: {line!r}")
        if 46 + 17 * (len(n.lines) - 1) > n.h - 6:
            raise ValueError(f"{flow.id}/{n.id}: too many lines for height {n.h}")
    for i, s in enumerate(flow.steps, 1):
        missing = (set(s.nodes) | {s.badge}) - node_ids | (set(s.edges) - edge_ids)
        if missing:
            raise ValueError(f"{flow.id} step {i}: unknown ids {sorted(missing)}")
        if len(f"{i}  {s.caption}") * 6.2 > WIDTH - 100:
            raise ValueError(f"{flow.id} step {i}: caption too long for the legend")
        if s.mat not in {"works today", "experimental", "planned", "future design"}:
            raise ValueError(f"{flow.id} step {i}: unknown maturity {s.mat!r}")


# ---------------------------------------------------------------------------
# SVG rendering
# ---------------------------------------------------------------------------

_PALETTE = (
    "svg{--bg:#111114;--card:#1c1c22;--card2:#24242c;--stroke:#3a3a44;--fg:#ececf1;"
    "--muted:#a8a8b3;--line:#6b6b78;--cap:#7dd3fc;--seal:#c4b5fd;--rep:#5eead4;"
    "--ver:#c4f0a8;--amb:#fcd34d;--warn:#fda4af}\n"
    "@media (prefers-color-scheme:light){svg{--bg:#fafaf9;--card:#ffffff;--card2:#f1f1f3;"
    "--stroke:#d4d4d8;--fg:#18181b;--muted:#52525b;--line:#a1a1aa;--cap:#0369a1;"
    "--seal:#6d28d9;--rep:#0f766e;--ver:#3f6212;--amb:#a16207;--warn:#be123c}}\n"
)

_BASE_CSS = """.bg{fill:var(--bg)}
.card{fill:var(--card);stroke:var(--stroke);stroke-width:1.2}
.card2{fill:var(--card2);stroke:var(--stroke);stroke-width:1}
.store{stroke-dasharray:5 4}
.warnbox{stroke:var(--warn)}
text{font-family:ui-sans-serif,system-ui,-apple-system,'Segoe UI',Roboto,\
'Helvetica Neue',Arial,sans-serif;fill:var(--fg)}
.mono{font-family:ui-monospace,'JetBrains Mono','DejaVu Sans Mono',Menlo,monospace}
.h{font-size:14px;font-weight:650}
.t{font-size:12px}
.m{font-size:11px}
.s{font-size:11px;fill:var(--muted)}
.title{font-size:17px;font-weight:700}
.num{font-size:11px;font-weight:700;text-anchor:middle}
.wire{fill:none;stroke:var(--line);stroke-width:1.6}
.observe{stroke-dasharray:4 4}
.blocked{stroke:var(--warn);stroke-dasharray:3 4}
.deny{stroke:var(--warn)}
.hl{fill:none;stroke-width:3;stroke-dasharray:7 7;stroke-linecap:round}
.ring{fill:none;stroke-width:2.5}
.lit{fill-opacity:.18}
"""

_COLORS = ("cap", "seal", "rep", "ver", "amb", "warn", "muted")


def _color_css() -> str:
    out = []
    for c in _COLORS:
        var = "line" if c == "muted" else c
        out.append(f".c-{c}{{stroke:var(--{var})}}.f-{c}{{fill:var(--{var})}}")
    return "\n".join(out) + "\n"


def _anim_css(flow: Flow) -> str:
    n = len(flow.steps)
    cycle = n * SLOT_SECONDS
    span = 100.0 / n
    return (
        f"@keyframes on{{0%{{opacity:0}}1.5%{{opacity:1}}{span - 2.5:.2f}%{{opacity:1}}"
        f"{span - 0.5:.2f}%{{opacity:0}}100%{{opacity:0}}}}\n"
        "@keyframes dash{to{stroke-dashoffset:-28}}\n"
        f".k{{opacity:0;animation:on {cycle:.1f}s linear infinite}}\n"
        f".hl{{opacity:0;animation:on {cycle:.1f}s linear infinite,dash 1.1s linear infinite}}\n"
        "@media (prefers-reduced-motion:reduce){.k,.hl{animation:none!important;"
        "opacity:0!important}.motion{display:none}}\n"
    )


def _f(v: float) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".")


def _text(x: float, y: float, s: str, cls: str, anchor: str = "start") -> str:
    keep = ' xml:space="preserve"' if s.startswith(" ") else ""
    return (
        f'<text class="{cls}" x="{_f(x)}" y="{_f(y)}" text-anchor="{anchor}"{keep}>'
        f"{escape(s, quote=False)}</text>"
    )


def badge_xy(flow: Flow, step: Step) -> tuple[int, int]:
    """The step number sits on the top-left corner of its node."""
    node = next(n for n in flow.nodes if n.id == step.badge)
    return node.x + 2, node.y + 2


def _lock(x: int, y: int) -> str:
    return (
        f'<g aria-hidden="true"><rect class="f-seal" x="{x}" y="{y + 6}" '
        'width="12" height="9" rx="2"/>'
        f'<path class="c-seal" d="M{x + 3} {y + 6} v-2.5 a3 3 0 0 1 6 0 v2.5" '
        'fill="none" stroke-width="1.6"/></g>'
    )


def _node_svg(n: Node) -> str:
    cls = "card"
    if n.style == "store":
        cls += " store"
    if n.style == "warn":
        cls += " warnbox"
    parts = [
        f'<rect class="{cls}" x="{n.x}" y="{n.y}" width="{n.w}" height="{n.h}" rx="10"/>',
        f'<rect class="f-{n.color}" x="{n.x}" y="{n.y}" width="{n.w}" height="4" rx="2"/>',
        _text(n.x + 12, n.y + 24, n.title, "h"),
    ]
    if n.style == "sealed":
        parts.append(_lock(n.x + n.w - 26, n.y + 10))
    lcls = "m mono" if n.mono else "t"
    for i, line in enumerate(n.lines):
        parts.append(_text(n.x + 12, n.y + 46 + 17 * i, line, lcls))
    return "\n".join(parts)


def _edge_svg(e: Edge) -> str:
    cls = "wire"
    if e.style in ("observe", "blocked", "deny"):
        cls += f" {e.style}"
    marker = "ahw" if e.style in ("blocked", "deny") else "ah"
    out = f'<path class="{cls}" d="{e.d}" marker-end="url(#{marker})"/>'
    if e.label:
        lcls = "s" if e.style != "blocked" else "t f-warn"
        out += "\n" + _text(e.lx, e.ly, e.label, lcls)
    return out


def _steps_desc(flow: Flow) -> str:
    return " ".join(f"Step {i}: {s.caption}" for i, s in enumerate(flow.steps, 1))


def render_svg(flow: Flow) -> str:
    validate(flow)
    n = len(flow.steps)
    cycle = n * SLOT_SECONDS
    nodes = {x.id: x for x in flow.nodes}
    edges = {x.id: x for x in flow.edges}
    h = flow.height
    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {h}" width="{WIDTH}" '
        f'height="{h}" role="img" aria-labelledby="t d">',
        f'<title id="t">{escape(flow.title, quote=False)}</title>',
        f'<desc id="d">{escape(flow.summary + " " + _steps_desc(flow), quote=False)}</desc>',
        "<style>",
        _PALETTE + _BASE_CSS + _color_css() + _anim_css(flow) + "</style>",
        "<defs>"
        '<marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
        'orient="auto-start-reverse"><path class="f-muted" d="M0 0 L10 5 L0 10z"/></marker>'
        '<marker id="ahw" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
        'orient="auto-start-reverse"><path class="f-warn" d="M0 0 L10 5 L0 10z"/></marker>'
        "</defs>",
        f'<rect class="bg" x="0" y="0" width="{WIDTH}" height="{h}" rx="14"/>',
        _text(22, 36, flow.title, "title"),
        _text(22, 56, flow.subtitle, "s"),
    ]
    # Per-step highlight rings sit under the cards so the card stays legible.
    for i, s in enumerate(flow.steps):
        delay = f"animation-delay:{_f(i * SLOT_SECONDS)}s"
        for nid in s.nodes:
            nd = nodes[nid]
            out.append(
                f'<rect class="ring k c-{s.color}" x="{nd.x - 4}" y="{nd.y - 4}" '
                f'width="{nd.w + 8}" height="{nd.h + 8}" rx="13" style="{delay}"/>'
            )
    out += [_node_svg(x) for x in flow.nodes]
    out += [_edge_svg(x) for x in flow.edges]
    for i, s in enumerate(flow.steps):
        delay = f"animation-delay:{_f(i * SLOT_SECONDS)}s,0s"
        for eid in s.edges:
            ed = edges[eid]
            col = "warn" if ed.style in ("deny", "blocked") else ed.color
            out.append(f'<path class="hl c-{col}" d="{ed.d}" style="{delay}"/>')
    # Numbered badges: static (always visible) plus a lit overlay per step.
    for i, s in enumerate(flow.steps, 1):
        bx, by = badge_xy(flow, s)
        out.append(
            f'<circle class="card2 c-{s.color}" cx="{bx}" cy="{by}" r="10" stroke-width="1.6"/>'
        )
        out.append(
            f'<circle class="k f-{s.color}" cx="{bx}" cy="{by}" r="10" '
            f'style="animation-delay:{_f((i - 1) * SLOT_SECONDS)}s"/>'
        )
        out.append(_text(bx, by + 4, str(i), "num"))
    # Packets (SMIL): each travels its step's arrows in sequence inside the step's slot.
    out.append('<g class="motion" aria-hidden="true">')
    for i, s in enumerate(flow.steps):
        m = len(s.edges)
        for j, eid in enumerate(s.edges):
            ed = edges[eid]
            a = (i + j / m) / n + 0.004
            b = (i + (j + 1) / m) / n - 0.012
            col = "warn" if ed.style in ("deny", "blocked") else ed.color
            kt = f"0;{_f(a)};{_f(b)};1"
            ot = f"0;{_f(a)};{_f(a + 0.002)};{_f(b)};{_f(b + 0.004)};1"
            out.append(
                f'<circle r="5" class="f-{col}" opacity="0">'
                f'<animateMotion dur="{_f(cycle)}s" repeatCount="indefinite" path="{ed.d}" '
                f'keyPoints="0;0;1;1" keyTimes="{kt}" calcMode="linear"/>'
                f'<animate attributeName="opacity" dur="{_f(cycle)}s" repeatCount="indefinite" '
                f'values="0;0;1;1;0;0" keyTimes="{ot}"/></circle>'
            )
    out.append("</g>")
    # Legend: every step, always visible; the current one is lit.
    ly = flow.legend_y
    lh = 40 + 21 * n
    out.append(f'<rect class="card2" x="22" y="{ly}" width="{WIDTH - 44}" height="{lh}" rx="10"/>')
    out.append(_text(38, ly + 24, "Steps, in order", "h"))
    for i, s in enumerate(flow.steps, 1):
        ry = ly + 42 + 21 * (i - 1)
        out.append(
            f'<rect class="k lit f-{s.color}" x="30" y="{ry - 14}" width="{WIDTH - 60}" '
            f'height="20" rx="5" style="animation-delay:{_f((i - 1) * SLOT_SECONDS)}s"/>'
        )
        out.append(f'<circle class="f-{s.color}" cx="48" cy="{ry - 4}" r="8"/>')
        out.append(
            f'<text class="num" x="48" y="{ry}" style="fill:var(--bg)">{i}</text>'
        )
        out.append(_text(64, ry, s.caption, "t"))
    out.append("</svg>")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Explainer data block
# ---------------------------------------------------------------------------


def flows_json() -> str:
    data = []
    for flow in FLOWS:
        validate(flow)
        data.append({
            "id": flow.id,
            "page": flow.page,
            "svg": f"../assets/architecture/{flow.id}.svg",
            "title": flow.title,
            "subtitle": flow.subtitle,
            "summary": flow.summary,
            "h": flow.diagram_h,
            "nodes": [
                {"id": n.id, "x": n.x, "y": n.y, "w": n.w, "h": n.h, "title": n.title,
                 "lines": list(n.lines), "color": n.color, "style": n.style, "mono": n.mono}
                for n in flow.nodes
            ],
            "edges": [
                {"id": e.id, "d": e.d, "color": e.color, "style": e.style, "label": e.label,
                 "lx": e.lx, "ly": e.ly}
                for e in flow.edges
            ],
            "steps": [
                {"nodes": list(s.nodes), "edges": list(s.edges), "caption": s.caption,
                 "detail": s.detail, "ref": s.ref, "mat": s.mat, "badge": list(badge_xy(flow, s)),
                 "color": s.color}
                for s in flow.steps
            ],
        })
    body = json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True)
    return f"{BEGIN}\n  var FLOWS = {body};\n  {END}"


def render_explainer(current: str) -> str:
    start = current.find(BEGIN)
    end = current.find(END)
    if start < 0 or end < start:
        raise SystemExit(f"{EXPLAINER}: generated-flows markers not found")
    return current[:start] + flows_json() + current[end + len(END):]


def outputs() -> dict[Path, str]:
    out = {ASSETS / f"{f.id}.svg": render_svg(f) for f in FLOWS}
    out[EXPLAINER] = render_explainer(EXPLAINER.read_text(encoding="utf-8"))
    return out


def main(argv: list[str]) -> int:
    check = "--check" in argv
    stale = []
    for path, text in outputs().items():
        old = path.read_text(encoding="utf-8") if path.exists() else None
        if old == text:
            continue
        if check:
            stale.append(path.relative_to(REPO))
        else:
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path.relative_to(REPO)}")
    if stale:
        print("stale (run: python scripts/gen_architecture_flows.py):", *stale, sep="\n  ")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
