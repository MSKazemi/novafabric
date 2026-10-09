#!/usr/bin/env python3
"""Generate the animated architecture flow diagrams and the explainer story.

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
* the ``STORY`` data block inside the explainer, plus ``how-it-works.svg`` — the
  end-to-end "How NovaFabric works" map: every stage from the workload to server
  mode, one step at a time, each with its maturity label and the code it was
  checked against.

Flows come in two groups. ``pipeline`` flows (mocked replay, the diff gate) zoom
into one stage of the capture pipeline; ``server`` flows each have their own page
under ``docs/architecture/`` that embeds the SVG.

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
STORY_BEGIN = "/* BEGIN generated story: scripts/gen_architecture_flows.py */"
STORY_END = "/* END generated story */"
MATURITY = ("works today", "experimental", "planned", "future design")
# The last tagged release. Steps marked ``unreleased`` describe behaviour on main
# after it; bump this (and clear those flags) when the next release is cut.
LAST_RELEASE = "v0.104.0"

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
    style: str = "card"  # card | store | warn | sealed | planned
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
    # True when the behaviour shown is on main but not in a release yet. Rendered as
    # an "on main, unreleased" marker next to the maturity label (docs-honesty rule).
    unreleased: bool = False


UNRELEASED_NOTE = f"on main, unreleased (after {LAST_RELEASE})"


@dataclass(frozen=True)
class StoryStep(Step):
    """A step of the end-to-end story: a step plus its stage, title and tokens."""

    title: str = ""
    stage: str = ""
    tokens: tuple[str, ...] = ()  # label carried along each edge, in edge order
    term: str = ""  # one illustrative command or result line


@dataclass(frozen=True)
class Stage:
    key: str
    name: str
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
    group: str = "server"  # server | pipeline | story

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

# ---------------------------------------------------------------------------
# Pipeline flow 1 — mocked replay (replay/; ADR-0300, ADR-0304, ADR-0305, issue #16)
# ---------------------------------------------------------------------------

REPLAY = Flow(
    id="mocked-replay",
    page="replay-modes.md",
    group="pipeline",
    title="Mocked replay: what is served, what runs live, what is refused",
    subtitle="nova replay --mode mocked (the default) · ADR-0300, ADR-0304, ADR-0305",
    summary=(
        "How the default replay mode re-runs a capsule. A capsule with no command is "
        "refused before anything is spawned. The re-run gets in-process dispatchers that "
        "serve recorded model calls and MCP tool results, raise recorded model errors as "
        "the SDK's own exception class, and report live network connections without "
        "blocking them."
    ),
    diagram_h=496,
    nodes=(
        Node("cmd", 22, 78, 150, 104, "nova replay",
             ("--mode mocked", "(the default)", "capsule or run ID"), "cap"),
        Node("chk", 192, 78, 170, 104, "Replayable?",
             ("needs a real argv;", "sdk-decorator, otel-", "import, @label: no"), "rep"),
        Node("pre", 382, 78, 190, 104, "Pre-checks",
             ("env.lock vs this host", "tool-schema drift", "(ADR-0128)"), "rep"),
        Node("spawn", 592, 78, 180, 104, "Re-run the command",
             ("subprocess, 600 s", "sitecustomize installs", "the dispatchers"), "rep"),
        Node("res", 790, 78, 168, 104, "Replay result",
             ("replay_result.yaml", "status · exit code", "replay_contract"), "ver"),
        Node("model", 22, 236, 300, 120, "MockModelDispatcher",
             ("one queue per API surface:", "Chat Completions · Responses API",
              "· Anthropic Messages", "sync · async · stream=True"), "rep"),
        Node("err", 342, 236, 200, 120, "Recorded model error",
             ("raised as the SDK's", "own class, for example", "openai.RateLimitError",
              "allow-listed classes only"), "amb"),
        Node("tool", 562, 236, 196, 120, "MockToolDispatcher",
             ("MCP call_tool results", "served one-to-one", "other tools: run live",
              "(non-MCP: planned)"), "rep"),
        Node("net", 778, 236, 180, 120, "NetworkObserver",
             ("IPv4/IPv6 connects", "observed and counted", "never blocked"), "amb"),
        Node("refuse", 192, 404, 280, 80, "Refused up front",
             ("status aborted · exit 1", "error.type CapsuleNotReplayable"), "warn",
             style="warn"),
        Node("div", 502, 404, 456, 80, "Divergence fails closed",
             ("an unmatched, extra or unconsumed call is a named divergence",
              "--permissive reports it instead of stopping"), "warn", style="warn"),
    ),
    edges=(
        Edge("m1", "M172 130 H190", "rep"),
        Edge("mref", "M332 182 V402", "warn", "deny"),
        Edge("m2", "M362 130 H380", "rep"),
        Edge("m3", "M572 130 H590", "rep"),
        Edge("dmod", "M640 182 V210 H172 V234", "rep"),
        Edge("derr", "M322 296 H340", "amb"),
        Edge("dtool", "M680 182 V234", "rep"),
        Edge("ddiv", "M660 356 V402", "warn", "deny"),
        Edge("dnet", "M740 182 V210 H868 V234", "amb"),
        Edge("nres", "M900 236 V184", "ver", "observe"),
        Edge("m4", "M772 130 H788", "ver"),
    ),
    steps=(
        Step(("cmd", "chk", "refuse"), ("m1", "mref"),
             "A capsule with no command to re-run (framework adapter, OTLP import, an @label) "
             "is refused before anything is spawned.",
             "<p><code>not_reexecutable_reason</code> is the one check. A refused replay "
             "still writes <code>replay_result.yaml</code> with <code>status: aborted</code> "
             "and exits 1; the message names the modes that do work on that capsule "
             "(<code>forensic</code>, <code>semantic</code>). <code>--dry-run</code> says "
             "the same and also exits 1.</p>",
             "replay/_replayability.py:not_reexecutable_reason · "
             "replay/_errors.py:CapsuleNotReplayableError",
             "works today", "chk", "warn", unreleased=True),
        Step(("pre",), ("m2",),
             "Every mode first compares env.lock with this host and re-validates each "
             "recorded tool call against its current schema.",
             "<p>Environment differences become <code>env_warnings</code>. Tool-schema drift "
             "is recorded in every mode; only <code>exact</code> refuses on it.</p>",
             "replay/_env_check.py:EnvironmentResolver · "
             "capture/schema_validation.py:revalidate_tool_calls",
             "works today", "pre", "rep"),
        Step(("spawn", "model"), ("m3", "dmod"),
             "The command re-runs in a subprocess; MockModelDispatcher serves each surface's "
             "recorded calls in order, sync, async or streamed.",
             "<p>Chat Completions, the Responses API and Anthropic Messages each have their "
             "own queue (ADR-0304). A <code>stream=True</code> call gets the record back as "
             "the chunk or event stream the SDK would have produced, so no tokens are "
             "spent. Wire records are transport (ADR-0305) and are never served. Serving "
             "sync Chat Completions and Messages calls shipped earlier; async, streamed and "
             "Responses API serving is on main and not in a release yet.</p>",
             "replay/_engine.py:ReplayEngine.run · replay/_dispatcher.py:MockModelDispatcher",
             "experimental", "spawn", "rep", unreleased=True),
        Step(("model", "err"), ("derr",),
             "A recorded rate limit, 4xx, 5xx or timeout keeps its queue position and is "
             "raised again as the SDK's own exception class.",
             "<p>The class comes from an allow-list looked up on the SDK package, never "
             "imported by a name read from the capsule. A call the SDK retried and then "
             "completed is served as the success it was. A stream that raised part-way "
             "serves its delivered chunks, then raises; a failed or incomplete Responses API "
             "response is returned with its recorded status. An error that cannot be rebuilt "
             "faithfully is the divergence <code>recorded_error_unreconstructable</code>.</p>",
             "replay/_model_errors.py:rebuild_sdk_error · ALLOWED_SDK_ERRORS",
             "experimental", "err", "amb", unreleased=True),
        Step(("tool", "div"), ("dtool", "ddiv"),
             "MCP call_tool results are served one-to-one and an unmatched call fails "
             "closed. Other tools still run live.",
             "<p>Substituting non-MCP tool results on owned boundaries is "
             "<strong>planned</strong> (ADR-0306, proposed). Until then, a tool that writes "
             "files or calls an API does so again on replay, so replay in a sandbox.</p>",
             "replay/_dispatcher.py:MockToolDispatcher · replay/_contract.py",
             "experimental", "tool", "rep", unreleased=True),
        Step(("net",), ("dnet",),
             "NetworkObserver counts every IPv4/IPv6 connection the replayed process "
             "opens: observed and reported, never blocked.",
             "<p>It patches <code>socket.connect</code>, records host and port only, and "
             "stops counting at 10,000 events (then the count is a lower bound). The totals "
             "land in <code>replay_contract.network_*</code>.</p>",
             "replay/_dispatcher.py:NetworkObserver · replay/_contract.py",
             "experimental", "net", "amb", unreleased=True),
        Step(("res",), ("nres", "m4"),
             "replay_result.yaml records status, exit code and the replay_contract counters "
             "(served, unmatched, unconsumed, live network).",
             "<p><code>intervention</code> (experimental) runs the same machinery with one "
             "event substituted and records <code>substitution_delivered_to_workload</code>: "
             "a substituted model response reaches the re-run; a substituted tool result "
             "does not, because tools run live.</p>",
             "replay/_result.py:write_replay_result · replay/_engine.py",
             "works today", "res", "ver", unreleased=True),
    ),
)

# ---------------------------------------------------------------------------
# Pipeline flow 2 — the diff gate (cli/diff.py, diff/; ADR-0303, ADR-0305)
# ---------------------------------------------------------------------------

DIFF = Flow(
    id="diff-gate",
    page="pipeline.md",
    group="pipeline",
    title="The diff gate: nova diff A B and its exit codes",
    subtitle="nova diff --assert-no-regressions · ADR-0303 and Amendment 1, ADR-0305",
    summary=(
        "How nova diff compares two runs and how its exit code gates CI. A ref that does "
        "not resolve exits 2. Malformed record lines are skipped and counted. Only logical "
        "model calls are aligned. Under --assert-no-regressions an incomplete read exits 2, "
        "any difference exits 1, and otherwise the gate exits 0."
    ),
    diagram_h=482,
    nodes=(
        Node("cmd", 22, 78, 150, 104, "nova diff A B",
             ("capsule path, run", "ID, or name@version", "--output-format"), "cap"),
        Node("res", 192, 78, 170, 104, "Resolve both refs",
             ("both sides must", "resolve, else exit 2", "(nothing compared)"), "rep"),
        Node("read", 382, 78, 190, 104, "Read the records",
             ("model-calls.jsonl", "tool-calls.jsonl", "bad line: skip + count"), "amb"),
        Node("align", 592, 78, 180, 104, "Align logical calls",
             ("transport excluded", "unique parent_span_id,", "then anchored order"), "amb"),
        Node("facets", 792, 78, 166, 104, "Compare facets",
             ("env.lock keys", "model and tool calls", "outputs/ by sha256"), "amb"),
        Node("report", 792, 236, 166, 104, "Report",
             ("text · json", "github-annotation", "json: has_changes"), "ver"),
        Node("gate", 482, 236, 282, 104, "Gate: --assert-no-regressions",
             ("1  read incomplete?  → exit 2", "2  has_changes?  → exit 1",
              "3  otherwise  → exit 0"), "seal"),
        Node("e2", 22, 388, 300, 80, "exit 2 · could not compare",
             ("unresolvable ref · unknown asset · usage", "or malformed lines under the gate"),
             "warn", style="warn"),
        Node("e1", 342, 388, 300, 80, "exit 1 · a difference",
             ("a changed, added or removed entry", "in any section of the report"), "warn",
             style="warn"),
        Node("e0", 662, 388, 296, 80, "exit 0 · no difference",
             ("nothing changed; and without a", "gate flag a report always exits 0"), "ver"),
    ),
    edges=(
        Edge("a1", "M172 130 H190", "rep"),
        Edge("xres", "M277 182 V386", "warn", "deny"),
        Edge("a2", "M362 130 H380", "amb"),
        Edge("a3", "M572 130 H590", "amb"),
        Edge("a4", "M772 130 H790", "amb"),
        Edge("a5", "M875 182 V234", "ver"),
        Edge("a6", "M792 288 H766", "seal"),
        Edge("xbad", "M520 182 V234", "amb", "observe", "skipped_malformed_lines", 528, 214),
        Edge("g2", "M500 340 V364 H300 V386", "warn", "deny"),
        Edge("g1", "M560 340 V386", "warn", "deny"),
        Edge("g0", "M700 340 V386", "ver"),
    ),
    steps=(
        Step(("cmd", "res", "e2"), ("a1", "xres"),
             "nova diff takes two capsule paths or run IDs; a ref that does not resolve exits "
             "2 before anything is compared.",
             "<p>Exit 2 means <em>could not compare</em>: an unresolvable capsule ref, an "
             "asset that is not in the registry, or a usage error. It is never 1, the gate's "
             "<em>found a difference</em> code (ADR-0303).</p>",
             "cli/diff.py:diff_cmd · EXIT_CANNOT_COMPARE",
             "works today", "res", "rep", unreleased=True),
        Step(("read",), ("a2",),
             "A record line that is not UTF-8, not JSON or not a JSON object is skipped, "
             "counted per side and warned about on stderr.",
             "<p>Before ADR-0303 Amendment 1 such a line was dropped silently, so the gate "
             "could pass on runs it had not fully read. The count is carried in the "
             "additive <code>skipped_malformed_lines</code> JSON key.</p>",
             "diff/_engine.py · diff/_report.py:DiffReport",
             "works today", "read", "amb", unreleased=True),
        Step(("align",), ("a3",),
             "Only logical model calls are aligned: wire records marked transport are left "
             "out, so one SDK call is one pair (ADR-0305).",
             "<p>A <code>parent_span_id</code> unique on both sides pairs exactly; the rest "
             "pair by position, anchored on identical requests, so one inserted call does not "
             "shift every later pair. Unpaired calls count as added or removed.</p>",
             "capture/record_roles.py:logical_model_calls · diff/_align.py",
             "works today", "align", "amb", unreleased=True),
        Step(("facets",), ("a4",),
             "Environment, model calls, tool calls and outputs/ are compared; a provider "
             "change under the same model is a changed call.",
             "<p>A changed <code>gen_ai.system</code> sets <code>request_changed</code> and "
             "the additive <code>provider_changed</code> flag. Output files compare by "
             "SHA-256; symlinks are skipped and never followed.</p>",
             "diff/_engine.py:DiffEngine.compare",
             "works today", "facets", "amb", unreleased=True),
        Step(("report",), ("a5",),
             "One property, has_changes, is the verdict; text, json and github-annotation "
             "all read it, so an added-only diff is an error.",
             "<p>The JSON report carries <code>has_changes</code> at the top level. The "
             "<code>github-annotation</code> formatter emits <code>::error</code> for any "
             "change, never <code>::notice</code>.</p>",
             "diff/_report.py:DiffReport · diff/_format.py",
             "works today", "report", "ver", unreleased=True),
        Step(("gate", "e2"), ("a6", "xbad", "g2"),
             "With --assert-no-regressions an incomplete read is checked first: the runs "
             "were not fully compared, so the gate exits 2.",
             "<p>A skipped line could be the very record that pairs with an added or removed "
             "entry, so over an incomplete read neither <em>the runs differ</em> nor "
             "<em>they do not</em> is established (ADR-0303 Amendment 1).</p>",
             "cli/diff.py:diff_cmd · DiffReport.is_complete",
             "works today", "gate", "warn", unreleased=True),
        Step(("gate", "e1", "e0"), ("g1", "g0"),
             "Then has_changes decides: exit 1 for any difference, else 0. Without a gate "
             "flag, a difference is reported and exits 0.",
             "<p><code>--significance</code> is a separate path: a statistical test over "
             "stored pass/fail scores, which exits 3 on a significant regression.</p>",
             "cli/diff.py:EXIT_DIFFERENCES · eval/regression_diff.py",
             "works today", "gate", "seal", unreleased=True),
    ),
)

FLOWS: tuple[Flow, ...] = (REPLAY, DIFF, OTLP, SERVE, CRYPTO, DATA)


# ---------------------------------------------------------------------------
# The end-to-end story — "How NovaFabric works" (explainer top + how-it-works.svg)
# ---------------------------------------------------------------------------
# Five columns (x = 22, 214, 406, 598, 790; 170 wide) and four rows: capture lane,
# capture tail, "use the evidence" lane, server lane. The capsule spans rows 1-2.

STAGES: tuple[Stage, ...] = (
    Stage("workload", "Workload", "cap"),
    Stage("capture", "Capture", "cap"),
    Stage("capsule", "Run Capsule", "ver"),
    Stage("seal", "Seal", "seal"),
    Stage("registry", "Registry + lineage", "rep"),
    Stage("replay", "Replay", "rep"),
    Stage("diff", "Diff + CI gate", "amb"),
    Stage("verify", "Verify + evidence", "ver"),
    Stage("server", "Server mode", "seal"),
    Stage("next", "Not built yet", "amb"),
)


def _ss(stage: str, title: str, nodes: tuple[str, ...], edges: tuple[str, ...],
        tokens: tuple[str, ...], caption: str, detail: str, ref: str, mat: str,
        badge: str, color: str, term: str = "", unreleased: bool = False) -> StoryStep:
    return StoryStep(nodes, edges, caption, detail, ref, mat, badge, color, unreleased,
                     title=title, stage=stage, tokens=tokens, term=term)


STORY = Flow(
    id="how-it-works",
    page="README.md",
    group="story",
    title="How NovaFabric works, end to end",
    subtitle=(
        "Capture → Run Capsule → seal → registry and lineage → replay → diff gate → "
        "verify → Evidence Bundle, plus server mode"
    ),
    summary=(
        "The whole system on one map. A workload runs under nova capture or a framework "
        "adapter; its SDK calls are intercepted, secret-scanned and written into a Run "
        "Capsule, which is optionally sealed and then indexed for lineage. The capsule is "
        "replayed, diffed in CI, verified and exported as an Evidence Bundle. nova serve, "
        "OTLP ingest and nova server work on the same capsules."
    ),
    diagram_h=692,
    nodes=(
        Node("wl", 22, 84, 170, 116, "Workload",
             ("nova capture -- cmd", "your code, unchanged", "OpenAI · Anthropic SDK",
              "MCP · httpx · requests"), "cap"),
        Node("hooks", 214, 84, 170, 116, "SDK interception",
             ("sitecustomize hooks", "sync · async · stream", "Responses API",
              "1 logical record/call"), "cap"),
        Node("scan", 406, 84, 170, 116, "Secret scan",
             ("SecretScannerV0", "redact in place", "rule pack 0.7.0",
              "+ maskers (opt-in)"), "warn"),
        Node("capsule", 598, 84, 170, 264, "Run Capsule",
             ("capsule.yaml", "model-calls.jsonl", "tool-calls.jsonl", "trace.jsonl",
              "env.lock", "replay.yaml", "lineage.jsonl", "redaction-proof.json",
              "capture-health.json", "outputs/", "sha256 per file"),
             "ver", style="store", mono=True),
        Node("seal", 790, 84, 170, 116, "NovaSeal",
             ("DSSE signature", "Merkle log entry", "RFC 3161 (opt-in)",
              ".seal/ if configured"), "seal", style="sealed"),
        Node("adapt", 22, 232, 170, 116, "Framework adapters",
             ("LangGraph · CrewAI", "ADK · AutoGen · DSPy", "LlamaIndex · +5 more",
              "in-process capsule"), "cap"),
        Node("wire", 214, 232, 170, 116, "Wire record",
             ("httpx: per attempt", "role: transport", "→ logical_call_id",
              "counted once (0305)"), "amb"),
        Node("proof", 406, 232, 170, 116, "Residual pass",
             ("capture-health.json", "written first", "residual_scan",
              "redaction-proof.json"), "warn"),
        Node("reg", 790, 232, 170, 116, "Registry + lineage",
             ("assets: name@version", "lineage.jsonl edges", "→ SQLite store",
              "provenance queries"), "rep"),
        Node("replay", 22, 396, 170, 116, "nova replay",
             ("mocked (default)", "forensic · semantic", "exact · intervention",
              "replay_result.yaml"), "rep"),
        Node("diff", 214, 396, 170, 116, "nova diff A B",
             ("logical calls only", "env · model · tool", "outputs/ by sha256",
              "has_changes"), "amb"),
        Node("gate", 406, 396, 170, 116, "CI gate",
             ("--assert-no-", "regressions", "0 same · 1 changed", "2 cannot compare"), "amb"),
        Node("verify", 598, 396, 170, 116, "nova verify",
             ("1 DSSE signature", "2 RFC 3161 token", "3 Merkle inclusion",
              "4 binding 5 digests"), "ver"),
        Node("bundle", 790, 396, 170, 116, "Evidence Bundle",
             ("nova export-evidence", "capsule + lineage", "in-toto attestations",
              "offline recipe"), "ver"),
        Node("otel", 22, 560, 170, 116, "OTel exporter",
             ("SDK or Collector", "OTLP/HTTP", "JSON or protobuf"), "cap"),
        Node("serve", 214, 560, 170, 116, "nova serve",
             ("localhost dashboard", "token · scopes", "single-tenant"), "seal"),
        Node("otlp", 406, 560, 170, 116, "OTLP ingest",
             ("traces → new capsule", "logs → sidecar store", "sealed: never written"), "rep"),
        Node("server", 598, 560, 170, 116, "nova server /v0",
             ("team REST API", "API keys · quotas", "SQLite or Postgres"), "seal"),
        Node("next", 790, 560, 170, 116, "Not built yet",
             ("non-MCP tool replay", "→ planned (ADR-0306)", "adapter-capsule re-run",
              "→ future design"), "amb", style="planned"),
    ),
    edges=(
        Edge("wl_hooks", "M192 142 H212", "cap"),
        Edge("wl_adapt", "M107 200 V230", "cap"),
        Edge("hooks_wire", "M299 200 V230", "amb"),
        Edge("hooks_scan", "M384 142 H404", "cap"),
        Edge("wire_scan", "M384 290 H395 V172 H404", "amb"),
        Edge("scan_proof", "M491 200 V230", "warn"),
        Edge("scan_cap", "M576 142 H596", "ver"),
        Edge("proof_cap", "M576 290 H596", "ver"),
        Edge("adapt_cap", "M107 348 V362 H620 V350", "cap"),
        Edge("cap_seal", "M768 142 H788", "seal"),
        Edge("cap_reg", "M768 290 H788", "rep"),
        Edge("cap_replay", "M700 348 V380 H150 V394", "rep"),
        Edge("cap_diff", "M700 348 V380 H330 V394", "amb"),
        Edge("cap_verify", "M740 348 V394", "ver"),
        Edge("seal_verify", "M800 200 V214 H779 V384 H756 V394", "seal"),
        Edge("replay_wl", "M22 454 H10 V142 H20", "rep"),
        Edge("replay_diff", "M192 454 H212", "amb"),
        Edge("diff_gate", "M384 454 H404", "amb"),
        Edge("verify_bundle", "M768 454 H788", "ver"),
        Edge("otel_serve", "M192 618 H212", "cap"),
        Edge("serve_otlp", "M384 618 H404", "rep"),
        Edge("otlp_cap", "M576 618 H587 V372 H660 V350", "rep"),
    ),
    steps=(
        _ss("workload", "Start a capture", ("wl", "hooks"), ("wl_hooks",),
            ("sitecustomize.py",),
            "nova capture runs your command as a child process; the runner puts a "
            "sitecustomize hook loader on its PYTHONPATH.",
            "<p>Pre-flight gates run before anything is written: required asset status, "
            "deployment environment, A/B variant, session. A 26-character ULID names the run "
            "and <code>CapsuleWriter.open()</code> creates its directory. The default runner "
            "is <code>local</code>; <code>docker</code>, <code>slurm</code> and "
            "<code>kubernetes</code> also work (<code>lsf</code> and <code>pbs</code> are "
            "experimental).</p>",
            "cli/capture.py:capture_cmd · capture/orchestrator.py:CaptureOrchestrator.run · "
            "runners/_sitecustomize.py",
            "works today", "wl", "cap", "$ nova capture -- python agent.py"),
        _ss("workload", "Or capture inside a framework", ("wl", "adapt", "capsule"),
            ("wl_adapt", "adapt_cap"), ("agent run", "capsule"),
            "Or capture in-process: a framework adapter, or the sdk agent decorator, writes "
            "its own capsule with capture_mode sdk-decorator.",
            "<p>Eleven adapters ship: LangGraph, CrewAI, AutoGen, DSPy, Google ADK, OpenAI "
            "Agents, A2A, Bedrock AgentCore, LlamaIndex, Pydantic AI and Haystack, plus "
            "<code>novafabric.sdk.agent</code>. Each records the measured host block and "
            "finalizes through the same path as <code>nova capture</code>: secret scan, "
            "manifest redaction, <code>lineage.jsonl</code>, residual pass, "
            "<code>evidence_digests</code>, and a seal when a signing profile exists. A "
            "finalization failure leaves the capsule unsealed with "
            "<code>metadata.finalization_error</code>, never failing the wrapped call. Its "
            "<code>command</code> is a label such as <code>@langgraph:demo</code>, so mocked "
            "replay refuses it up front.</p>",
            "adapters/_capsule.py:AdapterCapture · adapters/langgraph.py · sdk/agent.py:agent "
            "· capture/finalize.py:finalize_in_process_capsule",
            "works today", "adapt", "cap", "graph = wrap_langgraph(graph)", True),
        _ss("capture", "Intercept SDK calls", ("wl", "hooks"), ("wl_hooks",),
            ("messages.create",),
            "In the child, install_all patches the OpenAI and Anthropic SDKs: sync, async, "
            "streamed and Responses API calls each become one record.",
            "<p>A <code>stream=True</code> call is folded into one record when the stream "
            "ends; an abandoned stream is flagged <code>io.novafabric.stream_complete: "
            "false</code>. Each SDK record names its API surface "
            "(<code>io.novafabric.api_surface</code>). A call that raised is recorded once, "
            "with the exception class, status, body and retry headers. MCP "
            "<code>call_tool</code> results go to <code>tool-calls.jsonl</code>.</p>",
            "capture/hooks/__init__.py:install_all · capture/hooks/_openai.py · "
            "capture/hooks/_sdk_streams.py",
            "experimental", "hooks", "cap", "  chat.completions.create(stream=True) → 1 record",
            True),
        _ss("capture", "The wire record is transport", ("hooks", "wire"), ("hooks_wire",),
            ("HTTP attempt",),
            "The httpx hook also records each HTTP attempt. Under an SDK call it is marked "
            "transport and linked by logical_call_id (ADR-0305).",
            "<p>Both records are kept: the wire record is transport evidence, the SDK record "
            "is the logical call. Everything that counts or iterates calls "
            "(<code>model_call_count</code>, cost, <code>nova diff</code>, replay) reads "
            "logical calls through <code>record_roles</code>. A capsule captured before the "
            "marker is read through a conservative fallback that recognises the old "
            "duplicate shape.</p>",
            "capture/record_roles.py:stamp_wire_record · count_logical_model_calls · "
            "capture/hooks/_httpx.py",
            "works today", "wire", "amb", "  model_call_count: 2 logical (3 transport records)",
            True),
        _ss("capture", "Scan and redact secrets", ("scan",), ("hooks_scan", "wire_scan"),
            ("api_key=sk-…", "[REDACTED]"),
            "When the workload exits, env.lock is written with the measured host block; then "
            "SecretScannerV0 redacts matches in place.",
            "<p>The scan covers the call and event streams, <code>env.lock</code>, "
            "<code>assets.jsonl</code> and everything under <code>inputs/</code> and "
            "<code>outputs/</code>; configured maskers (experimental) run next. The proof "
            "records hashes and rule IDs, never the secret. Detection is rule-based: PEM "
            "keys, JWTs and passwords, for example, are <strong>not</strong> matched. One "
            "<code>host_info()</code> measures arch, CPU count and memory for every "
            "capsule.</p>",
            "capture/secrets.py:SecretScannerV0.scan_and_redact · capture/env.py:host_info",
            "works today", "scan", "warn", "  redaction: 1 finding (rule api-key)", True),
        _ss("capture", "Residual pass and capture health", ("proof",), ("scan_proof",),
            ("capture-health.json",),
            "If events were dropped, capture-health.json is written first, so the residual "
            "secret pass rescans it and the digests bind it.",
            "<p><code>residual_scan</code> then rescans every file except "
            "<code>capsule.yaml</code>, the proof and <code>.seal/</code>, including files "
            "written after the first pass. <code>redaction-proof.json</code> is written once, "
            "after it. Until issue #10, <code>capture-health.json</code> was written after "
            "sealing and nothing bound it.</p>",
            "capture/event_recorder.py:finalize_health · "
            "capture/secrets.py:SecretScannerV0.residual_scan",
            "works today", "proof", "warn", "  residual pass: 0 new findings · proof written",
            True),
        _ss("capsule", "Assemble the Run Capsule", ("capsule",), ("scan_cap", "proof_cap"),
            ("streams", "proof"),
            "replay.yaml, lineage.jsonl and capsule.yaml are written; evidence_digests pins "
            "a SHA-256 and size for every evidence file.",
            "<p>The manifest is redacted as a data structure before it is written and checked "
            "once more (<code>assert_manifest_clean</code>); if anything still matches, the "
            "capsule is not sealed. A failed workload still yields a complete capsule with "
            "<code>status: failure</code>, and a failing NovaFabric component is recorded "
            "without blocking the workload.</p>",
            "capture/finalize.py:evidence_digests · capture/capsule.py:CapsuleWriter · "
            "lineage/_writer.py:LineageWriter",
            "works today", "capsule", "ver", "✓ capsule written  ~/.novafabric/capsules/01J9Z…/"),
        _ss("seal", "Sign the manifest", ("capsule", "seal"), ("cap_seal",),
            ("capsule.yaml",),
            "If NovaSeal is configured, the canonical manifest is signed into a DSSE "
            "envelope; a sealing failure only warns, never fails capture.",
            "<p>The configuration comes from <code>NOVAFABRIC_SEAL_CONFIG</code> or "
            "<code>$NOVAFABRIC_HOME/novaseal.yaml</code>; without one, sealing is skipped. "
            "The SHA-256 of the canonical manifest is the <code>capsule_id</code>. Keys are "
            "ECDSA P-256 (local or cloud KMS) or Ed25519 (local).</p>",
            "capture/finalize.py:seal_capsule · trust/novaseal/__init__.py:NovaSeal.seal "
            "· trust/novaseal/envelope.py",
            "experimental", "seal", "seal", "  sealed: capsule_id 3f9a…"),
        _ss("seal", "Log and timestamp the seal", ("seal",), ("cap_seal",),
            ("H(0x00‖entry)",),
            "The seal is appended to a SHA-256 Merkle log; an RFC 3161 token is requested "
            "only from a tsa_url you configure.",
            "<p>With no TSA configured there is no timestamp and no network call. "
            "<code>.seal/log-entry.json</code> carries the leaf index, the root and the "
            "inclusion proof, so verification needs nothing but the capsule.</p>",
            "trust/novaseal/merkle.py · trust/novaseal/timestamp.py · trust/_rfc3161.py",
            "experimental", "seal", "seal",
            "  .seal/manifest.dsse · manifest.dsse.tsr · log-entry.json"),
        _ss("registry", "Index lineage and assets", ("capsule", "reg"), ("cap_reg",),
            ("lineage edges",),
            "lineage.jsonl edges are indexed into the local SQLite lineage store; assets are "
            "versioned as name@version in the registry.",
            "<p>Typed edges (<code>consumed</code>, <code>produced_by</code>, "
            "<code>replayed_from</code>) answer provenance and blast-radius queries "
            "(<code>nova lineage</code>). Kuzu, Postgres, Apache AGE and JanusGraph backends "
            "are experimental and library-level. The runs index that <code>nova serve</code> "
            "lists is built off the capture path, by a capsule watcher.</p>",
            "lineage/_importer.py:index_capsule_lineage · lineage/_store.py:LineageStore · "
            "registry/service.py",
            "works today", "reg", "rep", "  lineage: edges indexed into SQLite"),
        _ss("replay", "Replay with recorded responses", ("capsule", "replay", "wl"),
            ("cap_replay", "replay_wl"), ("capsule", "re-run"),
            "nova replay re-runs the command (mocked, the default) and serves recorded model "
            "calls per API surface, sync, async or streamed.",
            "<p>MCP tool results are served one-to-one; other tools run live, so replay in a "
            "sandbox. Divergence fails closed. Every IPv4/IPv6 connection the re-run opens is "
            "reported in <code>replay_contract.network_*</code>, never blocked. "
            "<code>forensic</code>, <code>semantic</code> and <code>exact</code> read the "
            "capsule without executing anything. Zoom in: "
            "<a href=\"#flow-mocked-replay-3\">the mocked replay flow</a>.</p>",
            "replay/_engine.py:ReplayEngine.run · replay/_dispatcher.py:MockModelDispatcher · "
            "replay/_contract.py",
            "experimental", "replay", "rep", "$ nova replay 01J9Z…", True),
        _ss("replay", "Replay a recorded failure", ("replay", "wl"), ("replay_wl",),
            ("RateLimitError 429",),
            "A recorded rate limit or 5xx is raised again as the SDK's own exception class; "
            "transport attempts are never served.",
            "<p>The error record keeps its queue position, so the workload's error handling "
            "runs against the same failure. Classes come from an allow-list looked up on the "
            "SDK package (for example <code>openai.RateLimitError</code> with "
            "<code>status_code == 429</code>, the body and the request id), never from a "
            "name read out of the capsule. A stream that raised part-way is served as its "
            "delivered chunks, then the same exception. A Responses API response with "
            "<code>status: failed</code> is returned as recorded, because the SDK returns it "
            "rather than raising.</p>",
            "replay/_model_errors.py:rebuild_sdk_error · "
            "replay/_dispatcher.py:MockModelDispatcher",
            "experimental", "replay", "amb",
            "  call 2 raised openai.RateLimitError (429), as recorded", True),
        _ss("replay", "Intervene, or refuse up front", ("replay", "diff"), ("replay_diff",),
            ("counterfactual",),
            "intervention substitutes one event and writes a counterfactual capsule; it "
            "records whether the substitute reached the workload.",
            "<p>A substituted model response reaches the re-run; a substituted tool result "
            "does not, because tools run live, and "
            "<code>substitution_delivered_to_workload: false</code> says so. A capsule with no "
            "command to re-run (adapter, OTLP import) is refused by <code>mocked</code> before "
            "anything is spawned (exit 1, <code>CapsuleNotReplayable</code>).</p>",
            "replay/_intervention.py · replay/_engine.py · "
            "replay/_replayability.py:not_reexecutable_reason",
            "experimental", "replay", "rep",
            "$ nova replay --mode intervention --intervention-file spec.yaml 01J9Z…", True),
        _ss("diff", "Diff two runs", ("capsule", "diff"), ("cap_diff",), ("A ⇄ B",),
            "nova diff A B pairs logical model calls, tool calls and outputs/; a provider "
            "or request-parameter change is a changed call.",
            "<p>It compares the environment keys of <code>env.lock</code>, model calls, tool "
            "calls and every file under <code>outputs/</code> by SHA-256. Malformed record "
            "lines are skipped and counted, never dropped silently. Zoom in: "
            "<a href=\"#flow-diff-gate-1\">the diff gate flow</a>.</p>",
            "diff/_engine.py:DiffEngine.compare · diff/_align.py · "
            "capture/record_roles.py:logical_model_calls",
            "works today", "diff", "amb", "$ nova diff 01J9A… 01J9B… --assert-no-regressions",
            True),
        _ss("diff", "Gate CI on the exit code", ("diff", "gate"), ("diff_gate",),
            ("has_changes",),
            "With --assert-no-regressions: exit 0 no difference, 1 a difference, 2 could not "
            "compare (bad ref, malformed capsule file or lines).",
            "<p>A ref that does not resolve, or an unreadable or malformed "
            "<code>capsule.yaml</code> or <code>env.lock</code>, exits 2 with or without the "
            "flag. Without a gate "
            "flag a difference is reported and exits 0. The JSON report carries "
            "<code>has_changes</code> and <code>skipped_malformed_lines</code>; "
            "<code>github-annotation</code> output turns any change into an "
            "<code>::error</code>.</p>",
            "cli/diff.py:diff_cmd · EXIT_DIFFERENCES · EXIT_CANNOT_COMPARE",
            "works today", "gate", "amb", "  1 model call changed · exit 1", True),
        _ss("verify", "Verify the seal", ("seal", "capsule", "verify"),
            ("seal_verify", "cap_verify"), ("manifest.dsse", "files"),
            "nova verify checks signature, timestamp, Merkle inclusion, manifest binding and "
            "every file digest; one changed byte exits 1.",
            "<p>A missing timestamp is reported as NOT PRESENT and an unconfigured log as NOT "
            "CHECKED; neither fails verification. Editing a file breaks its digest; editing "
            "<code>capsule.yaml</code> breaks the binding. The signature stays valid in both "
            "cases, which is why the binding and digest checks exist.</p>",
            "cli/verify.py:verify_cmd",
            "experimental", "verify", "ver", "$ nova verify ~/.novafabric/capsules/01J9Z…/"),
        _ss("verify", "Export an Evidence Bundle", ("verify", "bundle"), ("verify_bundle",),
            ("bundle.zip",),
            "nova export-evidence packs the capsule, a lineage subgraph and Ed25519-signed "
            "in-toto attestations into one ZIP.",
            "<p>Its <code>manifest.json</code> carries a verification recipe that needs only a "
            "SHA-256 tool and an Ed25519 verifier, so a third party can check it without "
            "NovaFabric. A capsule without <code>redaction-proof.json</code> cannot be "
            "exported.</p>",
            "cli/export_evidence.py:export_evidence_cmd · evidence/bundle.py:EvidenceBundleBuilder "
            "· evidence/intoto.py",
            "works today", "bundle", "ver",
            "$ nova export-evidence ~/.novafabric/capsules/01J9Z…/ -o bundle.zip"),
        _ss("server", "Browse locally with nova serve", ("serve",), (), (),
            "nova serve is a localhost dashboard and API over your capsules: a Host guard, a "
            "token, and a scope on every route.",
            "<p>An unclassified route is denied to everyone. Multi-tenant mode is refused at "
            "start-up while any read-path store is tenant-unsafe, so <code>serve</code> is "
            "single-tenant. Zoom in: <a href=\"#flow-serve-request-path-1\">the request path "
            "flow</a>.</p>",
            "serve/app.py · serve/authz.py:ROUTE_SCOPES · "
            "serve/tenancy.py:assert_multi_tenant_ready",
            "experimental", "serve", "seal", "$ nova serve --experimental"),
        _ss("server", "Ingest OpenTelemetry", ("otel", "serve", "otlp", "capsule"),
            ("otel_serve", "serve_otlp", "otlp_cap"), ("gen_ai spans", "scope operate",
                                                       "new ULID"),
            "OTLP traces carrying gen_ai.* spans become a brand-new capsule; OTLP logs go to "
            "an append-only sidecar, never into a capsule.",
            "<p>An ingested capsule is secret-scanned and records <code>capture_mode: "
            "otel-import</code>; this path does not sign it. A sealed capsule is observed, "
            "never written. Zoom in: <a href=\"#flow-otlp-ingest-1\">the OTLP ingest "
            "flow</a>.</p>",
            "otel/genai_ingest.py:write_ingest_capsule · otel/logs_ingest.py:ingest_otlp_logs",
            "experimental", "otlp", "rep", "POST /api/otlp/v1/traces → new capsule 01J9…"),
        _ss("server", "Share with nova server", ("server",), (), (),
            "nova server is the team REST API (/v0): API keys with workspace binding, budgets "
            "and keyset pages, on SQLite or Postgres.",
            "<p>SQLite is the default backend; Postgres (<code>--backend postgres</code>, the "
            "<code>server</code> extra) applies row-level security per transaction. Local mode "
            "never needs Postgres. Zoom in: <a href=\"#flow-server-data-plane-1\">the data "
            "plane flow</a>.</p>",
            "server/config.py:ServerConfig · server/routes/capsules.py · "
            "metadata_store/postgres.py",
            "experimental", "server", "seal", "$ nova server start --backend postgres"),
        _ss("next", "Planned: substitute every tool", ("next", "replay"), (), (),
            "Planned: mocked replay substitutes non-MCP tool results on owned boundaries "
            "(ADR-0306, proposed). Today those tools run live.",
            "<p>Nothing in ADR-0306 is implemented yet. Until it ships, only MCP "
            "<code>call_tool</code> results are served on replay.</p>",
            "replay/_dispatcher.py:MockToolDispatcher",
            "planned", "next", "amb"),
        _ss("next", "Future design", ("next",), (), (),
            "Future design: re-running a framework-adapter capsule, and multi-cluster "
            "federation of capsules and lineage. Not implemented.",
            "<p>An adapter capsule records a label, not a command, so it cannot be re-run "
            "(ADR-0306, open question 7). Federation is documented intent with no "
            "implementation and no target.</p>",
            "replay/_replayability.py:not_reexecutable_reason",
            "future design", "next", "amb"),
    ),
)



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
    stage_keys = {s.key for s in STAGES}
    for i, s in enumerate(flow.steps, 1):
        missing = (set(s.nodes) | {s.badge}) - node_ids | (set(s.edges) - edge_ids)
        if missing:
            raise ValueError(f"{flow.id} step {i}: unknown ids {sorted(missing)}")
        if len(f"{i}  {s.caption}") * 6.2 > WIDTH - 100:
            raise ValueError(f"{flow.id} step {i}: caption too long for the legend")
        if s.mat not in MATURITY:
            raise ValueError(f"{flow.id} step {i}: unknown maturity {s.mat!r}")
        if s.unreleased and s.mat in ("planned", "future design"):
            raise ValueError(f"{flow.id} step {i}: unbuilt work cannot be 'unreleased'")
        if isinstance(s, StoryStep):
            if s.stage not in stage_keys:
                raise ValueError(f"{flow.id} step {i}: unknown stage {s.stage!r}")
            if not s.title or len(s.tokens) > len(s.edges):
                raise ValueError(f"{flow.id} step {i}: needs a title, ≤ 1 token per edge")


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

# Only emitted by flows that use them, so older diagrams stay byte-identical.
_PLANNED_CSS = ".planned{stroke-dasharray:3 4;fill-opacity:.6}\n"
_DAGGER = "†"

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


def badges(flow: Flow) -> list[tuple[int, int]]:
    """Each step number sits on the top-left corner of its node.

    When several steps share a node, later badges move right along its top edge so
    they never overlap.
    """
    nodes = {n.id: n for n in flow.nodes}
    seen: dict[str, int] = {}
    out = []
    for s in flow.steps:
        k = seen.get(s.badge, 0)
        seen[s.badge] = k + 1
        node = nodes[s.badge]
        out.append((node.x + 2 + 22 * k, node.y + 2))
    return out


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
    if n.style == "planned":
        cls += " planned"
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
    return " ".join(
        f"Step {i}: {s.caption}" + (f" ({UNRELEASED_NOTE}.)" if s.unreleased else "")
        for i, s in enumerate(flow.steps, 1)
    )


def _has_unreleased(flow: Flow) -> bool:
    return any(s.unreleased for s in flow.steps)


def svg_height(flow: Flow) -> int:
    return flow.height + (22 if _has_unreleased(flow) else 0)


def render_svg(flow: Flow) -> str:
    validate(flow)
    n = len(flow.steps)
    cycle = n * SLOT_SECONDS
    nodes = {x.id: x for x in flow.nodes}
    edges = {x.id: x for x in flow.edges}
    h = svg_height(flow)
    extra = _PLANNED_CSS if any(x.style == "planned" for x in flow.nodes) else ""
    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {h}" width="{WIDTH}" '
        f'height="{h}" role="img" aria-labelledby="t d">',
        f'<title id="t">{escape(flow.title, quote=False)}</title>',
        f'<desc id="d">{escape(flow.summary + " " + _steps_desc(flow), quote=False)}</desc>',
        "<style>",
        _PALETTE + _BASE_CSS + extra + _color_css() + _anim_css(flow) + "</style>",
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
    for i, (s, (bx, by)) in enumerate(zip(flow.steps, badges(flow), strict=True), 1):
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
    # Legend: every step, always visible; the current one is lit. A dagger marks a
    # step whose behaviour is on main but not released yet.
    marked = _has_unreleased(flow)
    tx = 70 if marked else 64
    ly = flow.legend_y
    lh = 40 + 21 * n + (22 if marked else 0)
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
        if s.unreleased:
            out.append(_text(59, ry, _DAGGER, "s"))
        out.append(_text(tx, ry, s.caption, "t"))
    if marked:
        out.append(_text(38, ly + 42 + 21 * n + 4,
                         f"{_DAGGER} {UNRELEASED_NOTE[0].upper()}{UNRELEASED_NOTE[1:]}.", "s"))
    out.append("</svg>")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Explainer data blocks
# ---------------------------------------------------------------------------


def _flow_data(flow: Flow) -> dict[str, object]:
    validate(flow)
    data: dict[str, object] = {
        "id": flow.id,
        "page": flow.page,
        "group": flow.group,
        "svg": f"../assets/architecture/{flow.id}.svg",
        "title": flow.title,
        "subtitle": flow.subtitle,
        "summary": flow.summary,
        "h": flow.diagram_h,
        "note": UNRELEASED_NOTE,
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
    }
    steps = []
    for s, badge in zip(flow.steps, badges(flow), strict=True):
        row: dict[str, object] = {
            "nodes": list(s.nodes), "edges": list(s.edges), "caption": s.caption,
            "detail": s.detail, "ref": s.ref, "mat": s.mat, "badge": list(badge),
            "color": s.color, "unreleased": s.unreleased,
        }
        if isinstance(s, StoryStep):
            row.update(title=s.title, stage=s.stage, tokens=list(s.tokens), term=s.term)
        steps.append(row)
    data["steps"] = steps
    if flow.group == "story":
        data["stages"] = [{"key": st.key, "name": st.name, "color": st.color}
                          for st in STAGES]
    return data


def _block(begin: str, end: str, name: str, value: object) -> str:
    body = json.dumps(value, indent=1, ensure_ascii=False, sort_keys=True)
    return f"{begin}\n  var {name} = {body};\n  {end}"


def flows_json() -> str:
    return _block(BEGIN, END, "FLOWS", [_flow_data(f) for f in FLOWS])


def story_json() -> str:
    return _block(STORY_BEGIN, STORY_END, "STORY", _flow_data(STORY))


def _splice(current: str, begin: str, end: str, block: str) -> str:
    start = current.find(begin)
    stop = current.find(end)
    if start < 0 or stop < start:
        raise SystemExit(f"{EXPLAINER}: markers {begin!r} … {end!r} not found")
    return current[:start] + block + current[stop + len(end):]


def render_explainer(current: str) -> str:
    out = _splice(current, BEGIN, END, flows_json())
    return _splice(out, STORY_BEGIN, STORY_END, story_json())


def outputs() -> dict[Path, str]:
    out = {ASSETS / f"{f.id}.svg": render_svg(f) for f in (*FLOWS, STORY)}
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
