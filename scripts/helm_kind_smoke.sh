#!/usr/bin/env bash
# Kind install, authenticated smoke, upgrade and rollback for the novafabric chart (#224).
#
# Usage:
#   scripts/helm_kind_smoke.sh run                  # the whole lifecycle; exits non-zero on the first failed check
#   scripts/helm_kind_smoke.sh diagnostics <dir>    # collect pod logs, events and Helm state into <dir>
#
# It acts on whatever cluster the current kubectl context points at. In CI that is
# the throwaway Kind cluster created by .github/workflows/helm-kind.yml, with the
# image built from the pull request's tree already loaded into the node
# (`kind load docker-image`). The image is used with `pullPolicy: Never`, so a
# missing image fails loudly instead of silently pulling a published one.
#
# What `run` does, in order:
#   1. generates a random API token and a random database password into files under
#      $STATE_DIR (mode 0700) and stores them as Kubernetes Secrets with --from-file,
#      so neither value is ever printed or placed on a command line;
#   2. starts a throwaway Postgres in the namespace that reads its password from that
#      Secret, standing in for an operator's external database;
#   3. renders the chart with the CI values and checks that no chart-managed Secret
#      (and so no evaluation default password) and neither generated value appears;
#   4. `helm install`, waits for the rollout, and smokes the API through a
#      port-forward: /livez and /readyz 200, GET /v0/version 200 with the token,
#      401/403 without a token and with a wrong one; GET /metrics 200, which opens
#      the MetadataStore, then checks in Postgres that the database carries the
#      registry-track stamp and the lazily bootstrapped MetadataStore tables;
#   5. writes a marker file onto the data PVC;
#   6. `helm upgrade` with a changed value, checks the value reached the Deployment,
#      re-smokes, and checks the PVC and the marker survived;
#   7. `helm rollback` to revision 1, checks the value is back, re-smokes, re-checks
#      the PVC and the marker;
#   8. checks that neither generated value appears in any container log, in
#      `helm get values --all` or in `helm get manifest`.
#
# Requires: kubectl, helm (3.x), curl, jq.
set -euo pipefail

NAMESPACE="${NAMESPACE:-nova-ci}"
# The chart's fullname is "<release>-novafabric" unless the release name already
# contains "novafabric"; keep the default release name.
RELEASE="${RELEASE:-nova}"
CHART="${CHART:-deploy/helm/novafabric}"
IMAGE_REPO="${IMAGE_REPO:-novafabric-ci}"
IMAGE_TAG="${IMAGE_TAG:-kind}"
PG_IMAGE="${PG_IMAGE:-postgres:16-alpine}"
LOCAL_PORT="${LOCAL_PORT:-18433}"
# Service port of the chart (values.yaml `service.port`); it targets the container's
# `http` port, which is `server.port` in server mode.
SERVICE_PORT="${SERVICE_PORT:-4321}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-6m}"
# The value changed by the upgrade and restored by the rollback.
UPGRADE_MEMORY_REQUEST="${UPGRADE_MEMORY_REQUEST:-384Mi}"
STATE_DIR="${STATE_DIR:-${RUNNER_TEMP:-/tmp}/helm-kind-smoke}"

FULLNAME="${RELEASE}-novafabric"
DB_SECRET="${RELEASE}-ci-db"
AUTH_SECRET="${RELEASE}-ci-auth"
PG_NAME="${RELEASE}-ci-pg"
MARKER_PATH="/data/ci-retention-marker"

PF_PID=""

log() { printf '[helm-kind] %s\n' "$*" >&2; }
die() {
  printf '[helm-kind] FAIL: %s\n' "$*" >&2
  exit 1
}

summary() {
  # One line per passed check in the job summary; a no-op outside GitHub Actions.
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    printf -- '- %s\n' "$*" >>"$GITHUB_STEP_SUMMARY"
  fi
}

stop_port_forward() {
  if [ -n "$PF_PID" ]; then
    kill "$PF_PID" 2>/dev/null || true
    wait "$PF_PID" 2>/dev/null || true
    PF_PID=""
  fi
}
trap stop_port_forward EXIT

kc() { kubectl --namespace "$NAMESPACE" "$@"; }

# 64 hex characters from the kernel CSPRNG. Hex keeps the values free of shell,
# YAML and URL metacharacters. No pipe stage exits early, so pipefail cannot trip
# on a SIGPIPE here.
random_hex() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

# Fail if either generated value occurs in <file>. The pattern is read from the
# state file (grep -f), so the value never appears in an argument list.
assert_no_secret_in() {
  local file="$1" what="$2" name
  for name in token db-password; do
    if grep -qF -f "$STATE_DIR/$name" "$file"; then
      die "$what contains the generated $name"
    fi
  done
}

preflight() {
  local tool
  for tool in kubectl helm curl jq; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is not on PATH"
  done
  [ -d "$CHART" ] || die "chart directory $CHART not found (run from the repository root)"
  kubectl cluster-info >/dev/null || die "no reachable cluster in the current kubectl context"
  helm version --short >&2
}

new_secrets() {
  umask 077
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"
  random_hex >"$STATE_DIR/token"
  random_hex >"$STATE_DIR/db-password"
  if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
    # A workflow command: the runner consumes it and does not show it in the log.
    # From here on, any accidental print of either value is replaced by *** in the
    # job log. printf is a shell builtin, so the value is not in any process argv.
    printf '::add-mask::%s\n' "$(cat "$STATE_DIR/token")"
    printf '::add-mask::%s\n' "$(cat "$STATE_DIR/db-password")"
  fi
  # curl reads these with `-H @file`, keeping the token off its command line.
  printf 'Authorization: Bearer %s\n' "$(cat "$STATE_DIR/token")" >"$STATE_DIR/auth-header"
  printf 'Authorization: Bearer %s\n' "wrong-$(random_hex)" >"$STATE_DIR/wrong-auth-header"

  kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE" >/dev/null
  kc create secret generic "$DB_SECRET" --from-file=password="$STATE_DIR/db-password" >/dev/null
  kc create secret generic "$AUTH_SECRET" --from-file=token="$STATE_DIR/token" >/dev/null
  log "created Secrets $DB_SECRET and $AUTH_SECRET in namespace $NAMESPACE (values not shown)"
  summary "random API token and DB password stored as Secrets (never printed)"
}

deploy_postgres() {
  # Stand-in for an operator's managed Postgres: the chart runs with
  # postgres.enabled=false and reads the password from the existing Secret, the
  # production path the chart README documents. emptyDir is enough here: this
  # Postgres is not managed by the release, so upgrade and rollback never restart it.
  kc apply -f - >/dev/null <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${PG_NAME}
  labels:
    app.kubernetes.io/name: ${PG_NAME}
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: ${PG_NAME}
  template:
    metadata:
      labels:
        app.kubernetes.io/name: ${PG_NAME}
    spec:
      containers:
        - name: postgres
          image: ${PG_IMAGE}
          env:
            - name: POSTGRES_USER
              value: nova
            - name: POSTGRES_DB
              value: nova
            - name: POSTGRES_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: ${DB_SECRET}
                  key: password
            - name: PGDATA
              value: /var/lib/postgresql/data/pgdata
          ports:
            - name: postgres
              containerPort: 5432
          # TCP, not the socket: during initdb the image runs a temporary server
          # that listens on the socket only, then restarts it.
          readinessProbe:
            exec:
              command: ["pg_isready", "-h", "127.0.0.1", "-U", "nova", "-q"]
            periodSeconds: 3
          resources:
            requests:
              cpu: 50m
              memory: 128Mi
            limits:
              memory: 512Mi
          volumeMounts:
            - name: pgdata
              mountPath: /var/lib/postgresql/data
      volumes:
        - name: pgdata
          emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: ${PG_NAME}
spec:
  selector:
    app.kubernetes.io/name: ${PG_NAME}
  ports:
    - name: postgres
      port: 5432
      targetPort: postgres
EOF
  kc rollout status "deploy/$PG_NAME" --timeout=180s
  summary "external Postgres stand-in ready"
}

write_values() {
  # Only real chart keys (deploy/helm/novafabric/values.yaml). No secret value is in
  # these files: both credentials are referenced by Secret name.
  cat >"$STATE_DIR/values-ci.yaml" <<EOF
image:
  repository: ${IMAGE_REPO}
  tag: ${IMAGE_TAG}
  pullPolicy: Never
mode: server
postgres:
  enabled: false
externalDatabase:
  host: ${PG_NAME}
  port: 5432
  username: nova
  database: nova
  existingSecret: ${DB_SECRET}
  existingSecretPasswordKey: password
persistence:
  enabled: true
  size: 1Gi
extraEnv:
  # Pins the server's local bearer token (src/novafabric/server/local_token.py)
  # to the generated value instead of a token generated inside the pod.
  - name: NOVAFABRIC_SERVER_TOKEN
    valueFrom:
      secretKeyRef:
        name: ${AUTH_SECRET}
        key: token
EOF
  # The upgrade changes one value and acknowledges the v0.102.0 default flip, which
  # the chart requires on every `helm upgrade` (ADR-0230 D5).
  cat >"$STATE_DIR/values-upgrade.yaml" <<EOF
upgradeAcknowledged: true
resources:
  requests:
    memory: ${UPGRADE_MEMORY_REQUEST}
EOF
}

render_check() {
  helm lint "$CHART" -f "$STATE_DIR/values-ci.yaml" >&2
  helm template "$RELEASE" "$CHART" --namespace "$NAMESPACE" -f "$STATE_DIR/values-ci.yaml" \
    >"$STATE_DIR/rendered.yaml"
  if grep -q '^kind: Secret' "$STATE_DIR/rendered.yaml"; then
    die "the chart rendered its own Secret although externalDatabase.existingSecret is set"
  fi
  if grep -q 'app.kubernetes.io/component: postgres' "$STATE_DIR/rendered.yaml"; then
    die "the chart rendered the bundled evaluation Postgres although postgres.enabled=false"
  fi
  assert_no_secret_in "$STATE_DIR/rendered.yaml" "the rendered manifest"
  summary "render: no chart-managed Secret, no bundled Postgres, no secret value in the manifest"
}

memory_request() {
  kc get deploy "$FULLNAME" \
    -o jsonpath='{.spec.template.spec.containers[?(@.name=="nova")].resources.requests.memory}'
}

pvc_uid() { kc get pvc "${FULLNAME}-data" -o jsonpath='{.metadata.uid}'; }

expect_revision() {
  local want="$1" rev status
  rev=$(helm status "$RELEASE" --namespace "$NAMESPACE" -o json | jq -r '.version')
  status=$(helm status "$RELEASE" --namespace "$NAMESPACE" -o json | jq -r '.info.status')
  [ "$rev" = "$want" ] || die "expected Helm revision $want, found $rev"
  [ "$status" = "deployed" ] || die "Helm revision $rev is $status, not deployed"
  log "Helm revision $rev is deployed"
}

http_status() {
  local path="$1" header_file="${2:-}"
  local args=(-sS -o "$STATE_DIR/body" -w '%{http_code}' --max-time 10)
  if [ -n "$header_file" ]; then
    args+=(-H "@$header_file")
  fi
  curl "${args[@]}" "http://127.0.0.1:${LOCAL_PORT}${path}"
}

expect_status() {
  local phase="$1" label="$2" want="$3" path="$4" header_file="${5:-}" got
  got=$(http_status "$path" "$header_file") || got="curl-error"
  if [[ ! "$got" =~ ^(${want})$ ]]; then
    log "response body (first 500 bytes):"
    head -c 500 "$STATE_DIR/body" >&2 || true
    printf '\n' >&2
    die "$phase: $label: GET $path returned $got, expected $want"
  fi
  log "$phase: $label: GET $path -> $got"
}

smoke() {
  local phase="$1" i
  kc rollout status "deploy/$FULLNAME" --timeout=180s
  kc port-forward "svc/$FULLNAME" "${LOCAL_PORT}:${SERVICE_PORT}" \
    >"$STATE_DIR/port-forward-${phase}.log" 2>&1 &
  PF_PID=$!
  for i in $(seq 1 30); do
    if curl -fsS -o /dev/null --max-time 2 "http://127.0.0.1:${LOCAL_PORT}/livez" 2>/dev/null; then
      break
    fi
    if [ "$i" -eq 30 ]; then
      die "$phase: the port-forward to svc/$FULLNAME never answered /livez"
    fi
    sleep 1
  done

  expect_status "$phase" "liveness" "200" /livez
  expect_status "$phase" "readiness (database + schema at head)" "200" /readyz
  expect_status "$phase" "authenticated API call" "200" /v0/version "$STATE_DIR/auth-header"
  jq -e '.version | type == "string"' "$STATE_DIR/body" >/dev/null \
    || die "$phase: /v0/version did not return a version string"
  expect_status "$phase" "no token" "401|403" /v0/version
  expect_status "$phase" "wrong token" "401|403" /v0/version "$STATE_DIR/wrong-auth-header"
  # A MetadataStore-backed call. Only the registry track is migrated before start-up
  # (by the chart's init container, in server mode); the MetadataStore tables are
  # created lazily by PostgresMetadataStore.bootstrap() the first time
  # the server asks for the store (server/deps.py). With the local admin token
  # (no tenant) the upload and delete paths skip the MetadataStore, so the call that
  # reaches it is /metrics, which samples the store's connection pool. That sampling
  # swallows errors, so the table check below is what proves the bootstrap worked.
  expect_status "$phase" "metrics (opens the MetadataStore)" "200" /metrics "$STATE_DIR/auth-header"
  stop_port_forward
  check_database "$phase"
  summary "$phase: /livez 200, /readyz 200, /v0/version 200 with token, 401/403 without and with a wrong token, /metrics 200, MetadataStore tables present"
}

# One database holds both schemas: the registry track (the server's own Alembic
# stamp) and the MetadataStore tables. Checked through the Postgres pod over the
# local socket, so no password is passed.
psql_nova() {
  kc exec "deploy/$PG_NAME" -c postgres -- psql -v ON_ERROR_STOP=1 -At -U nova -d nova -c "$1"
}

check_database() {
  local phase="$1" stamp present
  stamp=$(psql_nova "SELECT version_num FROM alembic_version")
  # Registry-track revisions are p0001, p0002, ...; the MetadataStore track's are
  # v001, v002, ... A v-stamp here is the defect that made the server refuse to start.
  [[ "$stamp" =~ ^p[0-9]+$ ]] \
    || die "$phase: alembic_version is '$stamp', expected a registry-track revision (p...)"
  # The four tenant-scoped MetadataStore tables (metadata_store/postgres.py
  # _TENANT_TABLES; no registry-track table shares these names), with row-level
  # security enabled and forced: the last thing bootstrap() applies, so a partial
  # bootstrap does not pass.
  present=$(psql_nova "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname IN ('runs', 'capsules', 'signatures', 'retention_policies') AND c.relrowsecurity AND c.relforcerowsecurity")
  [ "$present" = "4" ] \
    || die "$phase: $present of the 4 MetadataStore tables exist with forced RLS after /metrics (lazy bootstrap)"
  log "$phase: registry stamp $stamp; MetadataStore tables bootstrapped with forced RLS"
}

write_marker() {
  MARKER="ci-${GITHUB_RUN_ID:-local}-$(date +%s)"
  # $1 inside the single-quoted script is the marker passed as an argument.
  # shellcheck disable=SC2016
  kc exec "deploy/$FULLNAME" -c nova -- sh -c 'printf %s "$1" > "$2"' sh "$MARKER" "$MARKER_PATH"
  PVC_UID=$(pvc_uid)
  [ -n "$PVC_UID" ] || die "data PVC ${FULLNAME}-data not found"
  log "wrote retention marker to $MARKER_PATH on PVC $PVC_UID"
}

check_retention() {
  local phase="$1" got uid
  uid=$(pvc_uid)
  [ "$uid" = "$PVC_UID" ] || die "$phase: data PVC was replaced ($PVC_UID -> $uid)"
  got=$(kc exec "deploy/$FULLNAME" -c nova -- cat "$MARKER_PATH") \
    || die "$phase: retention marker $MARKER_PATH is gone"
  [ "$got" = "$MARKER" ] || die "$phase: retention marker changed"
  log "$phase: data PVC and marker retained"
  summary "$phase: data PVC $uid and the marker file survived"
}

install() {
  helm install "$RELEASE" "$CHART" --namespace "$NAMESPACE" \
    -f "$STATE_DIR/values-ci.yaml" --wait --timeout "$WAIT_TIMEOUT"
  expect_revision 1
  INITIAL_MEMORY_REQUEST=$(memory_request)
  [ "$INITIAL_MEMORY_REQUEST" != "$UPGRADE_MEMORY_REQUEST" ] \
    || die "the stock memory request already equals $UPGRADE_MEMORY_REQUEST; pick another upgrade value"
  summary "install: revision 1 deployed (memory request $INITIAL_MEMORY_REQUEST)"
}

upgrade() {
  helm upgrade "$RELEASE" "$CHART" --namespace "$NAMESPACE" \
    -f "$STATE_DIR/values-ci.yaml" -f "$STATE_DIR/values-upgrade.yaml" \
    --wait --timeout "$WAIT_TIMEOUT"
  expect_revision 2
  [ "$(memory_request)" = "$UPGRADE_MEMORY_REQUEST" ] \
    || die "upgrade: memory request is $(memory_request), expected $UPGRADE_MEMORY_REQUEST"
  summary "upgrade: revision 2 deployed (memory request $UPGRADE_MEMORY_REQUEST)"
}

rollback() {
  helm rollback "$RELEASE" 1 --namespace "$NAMESPACE" --wait --timeout "$WAIT_TIMEOUT"
  expect_revision 3
  [ "$(memory_request)" = "$INITIAL_MEMORY_REQUEST" ] \
    || die "rollback: memory request is $(memory_request), expected $INITIAL_MEMORY_REQUEST"
  summary "rollback: revision 3 deployed with revision 1's values (memory request $INITIAL_MEMORY_REQUEST)"
}

leak_check() {
  local pod container out="$STATE_DIR/leak-check"
  mkdir -p "$out"
  for pod in $(kc get pods -o name); do
    for container in $(kc get "$pod" -o jsonpath='{.spec.initContainers[*].name} {.spec.containers[*].name}'); do
      kc logs "$pod" -c "$container" >"$out/${pod#pod/}-${container}.log" 2>&1 || true
      assert_no_secret_in "$out/${pod#pod/}-${container}.log" "the log of $pod/$container"
    done
  done
  helm get values "$RELEASE" --namespace "$NAMESPACE" --all >"$out/helm-values.yaml"
  assert_no_secret_in "$out/helm-values.yaml" "helm get values --all"
  helm get manifest "$RELEASE" --namespace "$NAMESPACE" >"$out/helm-manifest.yaml"
  assert_no_secret_in "$out/helm-manifest.yaml" "helm get manifest"
  summary "no generated secret in any container log, the Helm values or the Helm manifest"
}

cmd_run() {
  preflight
  new_secrets
  deploy_postgres
  write_values
  render_check
  install
  smoke install
  write_marker
  upgrade
  smoke upgrade
  check_retention upgrade
  rollback
  smoke rollback
  check_retention rollback
  leak_check
  log "PASS: install, authenticated smoke, upgrade and rollback"
}

cmd_diagnostics() {
  local out="${1:?usage: $0 diagnostics <out-dir>}" pod container name file
  # Collect as much as possible; one failing command must not hide the rest.
  set +e
  mkdir -p "$out"
  kubectl get nodes -o wide >"$out/nodes.txt" 2>&1
  kubectl get events --all-namespaces --sort-by=.lastTimestamp >"$out/events.txt" 2>&1
  kc get all,pvc,configmap -o wide >"$out/resources.txt" 2>&1
  # Table output lists names, types and key counts only, never data.
  kc get secrets >"$out/secret-names.txt" 2>&1
  kc describe pods >"$out/pods-describe.txt" 2>&1
  kc describe pvc >"$out/pvc-describe.txt" 2>&1
  for pod in $(kc get pods -o name 2>/dev/null); do
    for container in $(kc get "$pod" -o jsonpath='{.spec.initContainers[*].name} {.spec.containers[*].name}' 2>/dev/null); do
      kc logs "$pod" -c "$container" --tail=400 >"$out/logs-${pod#pod/}-${container}.txt" 2>&1
      kc logs "$pod" -c "$container" --previous --tail=200 \
        >"$out/logs-${pod#pod/}-${container}-previous.txt" 2>&1 \
        || rm -f "$out/logs-${pod#pod/}-${container}-previous.txt"
    done
  done
  helm history "$RELEASE" --namespace "$NAMESPACE" >"$out/helm-history.txt" 2>&1
  helm status "$RELEASE" --namespace "$NAMESPACE" >"$out/helm-status.txt" 2>&1
  cp "$STATE_DIR"/port-forward-*.log "$out/" 2>/dev/null
  # Belt and braces: drop any collected file that contains a generated value. The
  # state directory itself (which holds them) is never part of the artifact.
  for name in token db-password; do
    [ -s "$STATE_DIR/$name" ] || continue
    for file in $(grep -rlF -f "$STATE_DIR/$name" "$out" 2>/dev/null); do
      printf '[removed: this file contained a generated %s]\n' "$name" >"$file"
      log "WARNING: removed $file from the diagnostics: it contained a generated $name"
    done
  done
  log "diagnostics written to $out"
  return 0
}

case "${1:-}" in
  run) cmd_run ;;
  diagnostics) cmd_diagnostics "${2:-}" ;;
  *)
    printf 'usage: %s run | diagnostics <out-dir>\n' "$0" >&2
    exit 2
    ;;
esac
