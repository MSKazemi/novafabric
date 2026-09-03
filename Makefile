# NovaFabric — Root Makefile
#
# Delegates to sub-project Makefiles and provides top-level convenience targets.
# Python quality gates are run via `uv run`; Go targets delegate to collector/Makefile.

# Load-aware `-n auto`: pytest-xdist reads PYTEST_XDIST_AUTO_NUM_WORKERS as the
# value of `auto`, so every test target inherits a worker count sized to the
# cores and memory that are actually free (scripts/test-workers.sh) instead of
# claiming all of them. The pytest command lines are untouched — `test-par`
# stays byte-for-byte CI's command. Override: NOVA_TEST_WORKERS=8 make test-fast
export PYTEST_XDIST_AUTO_NUM_WORKERS ?= $(shell ./scripts/test-workers.sh)

COMPOSE      := docker compose -f deploy/docker/docker-compose.yml
COMPOSE_PROD := docker compose -f deploy/docker/docker-compose.yml --profile prod
COMPOSE_AGE  := docker compose -f deploy/docker/docker-compose.yml --profile age

.PHONY: papers papers-check help test lint typecheck coverage benchmark benchmark-capture \
	test-fast test-par test-container test-changed test-watch \
	test-direct test-impacted test-index \
	bench-scale bench-lineage \
        check-links check-decisions site \
        bundle serve-local deploy-local \
        topology-build topology-test serve-topology \
        compliance-smoke classify-smoke audit-smoke migrate-schema-smoke \
        verify-smoke suggest-register-smoke reports-smoke \
        kg-smoke entity-queue-smoke ingest-capsule-smoke \
        init-smoke serve-compliance-smoke \
        eval-list-smoke policy-list-smoke \
        wave2-smoke collector-smoke \
        collector-build collector-test collector-spec-test spec-test \
        dev-up dev-down dev-logs \
        prod-up prod-down prod-logs \
        age-up age-down \
        docker-up docker-build docker-down docker-logs docker-token update _wait-token

# ── Papers ───────────────────────────────────────────────────────────

# The manuscript portfolio (ADR 0264). papers/ is private in full and is not
# part of any public build; these targets exist so the gate is one command.
# They replace `whitepaper` / `whitepaper-html`, deleted 2026-08-29: both read
# docs/whitepaper/novafabric-position-paper.md, which no longer exists, and the
# first one's opening move was `sudo snap install pandoc`.

papers:
	@$(MAKE) --no-print-directory -C papers all

papers-check:
	@$(MAKE) --no-print-directory -C papers check

# ── Help ─────────────────────────────────────────────────────────────────────

help:
	@echo "NovaFabric — available targets:"
	@echo "  test              Run Python test suite (pytest, benchmarks skipped)"
	@echo "  test-fast         Fast dev loop: parallel (-n auto), no coverage, no Docker (-m 'not container')"
	@echo "  test-direct       Tier 0 inner loop: tests directly covering your change (~17s)"
	@echo "  test-impacted     Tier 1: everything whose import closure reaches your change"
	@echo "  test-index        Rebuild the test-selector import index"
	@echo "  test-changed      DISABLED - testmon is unusable here (>10 min); use test-direct"
	@echo "  test-watch        test-direct, rerun on every save (pytest-watcher)"
	@echo "  test-container    The Docker tier only (testcontainers + docker CLI); skips without a daemon"
	@echo "  test-par          Release gate — byte-for-byte what CI's unit job runs (~5 min)"
	@echo "  benchmark         Run NovaSeal p99 latency gate (100 rounds, < 200 ms)"
	@echo "  benchmark-capture Run capture-overhead p95 gate (30 captured runs, < 2000 ms)"
	@echo "  bench-scale       Dashboard p95 gate at 100K rows (~7s; skipped by default)"
	@echo "  bench-lineage     Lineage bench (standalone pkg — bench/ is not in the root run)"
	@echo "  lint              Run ruff linter on src/, tests/ and scripts/"
	@echo "  typecheck         Run mypy on src/"
	@echo "  check-links       Verify every relative link in public docs resolves"
	@echo "  check-decisions   Verify docs/decisions.md matches the ADR tree"
	@echo "  site              Build the public website (web/dist/), docs pages included"
	@echo "  papers            Build every LaTeX manuscript in papers/ (private)"
	@echo "  papers-check      Build, then run the portfolio gate over papers/"
	@echo "  coverage          Run pytest with coverage report"
	@echo "  bundle            Build web dashboard (web/) and copy to static dir"
	@echo "  serve-local       Build bundle + start nova serve --experimental"
	@echo "  topology-build    Build nova-dashboard SPA and copy to static/topology/"
	@echo "  topology-test     Run nova-dashboard TypeScript tests (vitest)"
	@echo "  serve-topology    Build topology SPA + start nova serve --experimental --topology"
	@echo "  serve-topology-only  Start nova serve --topology without rebuilding the SPA (use on servers without npm)"
	@echo "  compliance-smoke  Smoke-test compliance CLI commands (requires NOVA_PII_PEPPER)"
	@echo "  classify-smoke    Smoke-test nova classify (EU AI Act + NIST AI RMF vocabularies)"
	@echo "  audit-smoke            Smoke-test nova audit map for all 6 profiles"
	@echo "  migrate-schema-smoke   Smoke-test nova migrate-schema --dry-run on NOVAFABRIC_HOME capsules"
	@echo "  verify-smoke           Smoke-test nova verify on the most recent local capsule"
	@echo "  suggest-register-smoke Smoke-test nova suggest-register (dry run, no auto-register)"
	@echo "  reports-smoke          Run /api/reports/* backend tests"
	@echo "  kg-smoke               Run /api/kg/* backend tests"
	@echo "  entity-queue-smoke     Run /api/kg/entity-queue/* backend tests"
	@echo "  init-smoke             Smoke-test nova init (creates dirs + Ed25519 keypair in /tmp)"
	@echo "  ingest-capsule-smoke   Smoke-test nova ingest-capsule --all (Scale-S3)"
	@echo "  serve-compliance-smoke Run serve compliance endpoint tests (RoPA/AIBOM CycloneDX 1.7/NIST-RMF, v0.37.0+v0.39.0)"
	@echo "  eval-list-smoke        Smoke-test nova eval list (shows all registered adapters)"
	@echo "  policy-list-smoke      Smoke-test nova policy list (shows Rego bundle + stored policies)"
	@echo "  collector-build   Build Go collector binaries"
	@echo "  collector-test    Run Go collector tests (race detector)"
	@echo "  collector-spec-test  Validate 1000 corpus events"
	@echo "  spec-test         Alias for collector-spec-test"
	@echo ""
	@echo "Docker stack:"
	@echo "  dev-up            Build and start dev stack (Postgres + novafabric-serve)"
	@echo "  dev-down          Stop dev stack"
	@echo "  dev-logs          Follow live logs (dev stack)"
	@echo "  prod-up           Build and start full prod stack (+ ClickHouse + NATS + Kafka + PgBouncer + JanusGraph)"
	@echo "  prod-down         Stop prod stack"
	@echo "  prod-logs         Follow live logs (prod stack)"
	@echo "  age-up            EXPERIMENTAL: start ONLY the standalone Apache AGE lineage backend on :5433"
	@echo "  age-down          Stop and remove ONLY the age container (never touches dev/prod services)"
	@echo "  docker-build      Rebuild nova image only (no pull)"
	@echo "  docker-token      Print the dashboard URL with the live auth token"
	@echo "  update            git pull + rebuild nova image + rolling restart"
	@echo ""
	@echo "  docker-up         Alias for dev-up"
	@echo "  docker-down       Alias for dev-down"
	@echo "  docker-logs       Alias for dev-logs"

# ── Python quality gates ──────────────────────────────────────────────────────

test:
	uv run pytest --benchmark-disable --cov=novafabric --cov-report=term-missing

# Fast dev loop: parallel, no coverage, no Docker.
#
# Scope is chosen by MARKER, not by directory. `-m "not container"` drops
# exactly the tests whose fixture closure starts a container (see
# CONTAINER_FIXTURES in tests/conftest.py) and nothing else. The directory-level
# `--ignore=tests/metadata_store` this replaces was throwing away 73 Docker-free
# tests in that tier — which is how a docstring regression survived seven passes
# of this gate. tests/integration stays ignored: it is CI-only by design.
#
# Full `make test` / `make test-par` remain the release gates and still run the
# container tier.
test-fast:
	uv run pytest -n auto --dist=loadgroup --benchmark-disable -q \
		-m "not container" \
		--ignore=tests/integration

# The container tier on its own — testcontainers Postgres/AGE/JanusGraph and the
# docker-CLI examples. Needs a reachable Docker daemon; skips cleanly without one.
# Export TESTCONTAINERS_REUSE_ENABLE=true (see docs/developer-guide.md) to keep
# the containers warm between runs instead of paying startup every time.
test-container:
	uv run pytest -m "container" --benchmark-disable -q \
		--dist=loadgroup -n auto

# Tier 0 — the inner loop. The tests that directly cover what you changed,
# selected by scripts/testsel.py from location and module name. ~17 s on a
# one-package change, against ~4 min for the whole fast suite.
#
# This is what the Claude Code Stop hook runs automatically at the end of every
# turn (.claude/settings.json), so in normal work nobody types it.
test-direct:
	./scripts/run-scoped-tests.sh direct

# Tier 1 — every test whose static import closure reaches the changed modules.
# Broader and slower than test-direct, narrower than the whole suite. If the
# closure exceeds 40% of the suite it escalates to `test-fast` on its own,
# because past that point selecting has stopped saving anything.
test-impacted:
	./scripts/run-scoped-tests.sh impact

# Rebuild the selector's import index. It is rebuilt automatically when missing;
# do this by hand after a large refactor. ~2.5 s for ~860 test files.
test-index:
	uv run python scripts/testsel.py --rebuild-index --build-only

# ⚠ MEASURED UNUSABLE ON THIS SUITE (2026-09-01) — kept only so the finding is
# not rediscovered. pytest-testmon cannot run under pytest-xdist, so its baseline
# is a SERIAL run of 12,280 tests: measured at over 10 MINUTES on both a cold and
# a warm index, not the "sub-second" this once claimed. Use `test-direct`.
test-changed:
	@echo "test-changed is unusable on this suite (>10 min, testmon cannot parallelise)."
	@echo "Use 'make test-direct' (~17 s) or 'make test-impacted'. See docs/developer-guide.md."
	@exit 1

# test-direct, rerun on every save. Uses --runner because the selection is
# recomputed per run from the current diff, not fixed at launch.
test-watch:
	uv run ptw . --runner ./scripts/watch-runner.sh

# The release gate. Byte-for-byte the command CI's `unit` job runs, so passing
# this locally means passing there — including the scope, the distribution and
# the coverage floor. `tests/docs/test_makefile_matches_ci_gate.py` fails if the
# two ever drift apart.
#
# It is written out in full rather than factored into a variable: this recipe is
# meant to be readable next to .github/workflows/ci.yml and diffed against it by
# eye as well as by the guard.
test-par:
	uv run pytest tests/ --ignore=tests/integration --benchmark-disable \
		-n auto --dist=loadgroup \
		--cov=novafabric --cov-report=term-missing --cov-fail-under=90

benchmark:
	mkdir -p .benchmark-results
	uv run pytest tests/seal/test_benchmark.py -v \
		--benchmark-json=.benchmark-results/seal_latency.json

benchmark-capture:
	mkdir -p .benchmark-results
	uv run pytest tests/bench/test_capture_overhead_gate.py -v \
		--benchmark-json=.benchmark-results/capture_overhead.json

# --- bench/ is NOT part of the root pytest run -------------------------------
# `testpaths = ["tests"]`, so a bare `pytest` collects 0 of its tests. Running
# `pytest bench/` from the repo root FAILS with 7 collection errors, and that is
# expected, not a regression: bench/lineage is a separate distribution that is
# not in the main venv, and bench/lineage/tests/ and bench/testbench/tests/ both
# claim the top-level module name `tests`. Each bench is its own package and
# must be run from its own directory. These targets are the documented way in.
bench-scale:  ## Dashboard scale gate (p95 @ 100K rows) — cheap, ~7s, normally skipped
	NOVA_DASHBOARD_SCALE=1 uv run pytest tests/bench/test_dashboard_scale_gate.py -v

bench-lineage:  ## Lineage bench suite — standalone package, own venv/lockfile
	cd bench/lineage && uv run --frozen pytest -q

lint:
	# --no-cache: a stale ruff cache reported "All checks passed!" for hours on
	# 2026-07-20 while two real I001 errors existed. Three separate agents saw the
	# errors in fresh checkouts and were told, wrongly, that main was clean. A lint
	# gate that can report a false green is worse than no gate, because it is
	# trusted. The cache saves a few seconds and costs correctness.
	uv run ruff check src tests scripts --no-cache

typecheck:
	uv run mypy src

check-links:
	# Every relative link in a public markdown file must resolve for someone who
	# cloned only the public repository. On 2026-08-05 this was false for 142
	# links pointing into the private design/ tree — including the RFC-process
	# link CONTRIBUTING tells a new contributor to read first.
	uv run python scripts/check_doc_links.py

check-decisions:
	uv run python scripts/gen_decisions_index.py --check

coverage:
	uv run pytest --cov=novafabric --cov-report=term-missing --cov-report=html

# ── Dashboard bundle ──────────────────────────────────────────────────────────

bundle:
	cd web && npm run build:dashboard

airgap-bundle:  ## Build a signed air-gap bundle from dist/ (ADR-0249 slice 1)
	uv build
	uv run python scripts/build_airgap_bundle.py \
		--out dist/novafabric-airgap.tar \
		--signing-key $${NOVA_AIRGAP_SIGNING_KEY:?set NOVA_AIRGAP_SIGNING_KEY to an ed25519 private key (generate_keypair)} \
		$$(for w in dist/*.whl dist/*.tar.gz; do echo --member "dist/$$(basename $$w)=$$w"; done)

site:
	# Builds the public website, including the docs/ tree as static pages.
	# The deploy itself is manual — see web/README.md "Deploy". Building is not
	# deploying, which is why novafabric.ai/docs/ can 404 while these pages exist.
	cd web && npm run build
	@echo ""
	@echo "Built $$(find web/dist -name '*.html' | wc -l) pages, of which \
$$(find web/dist/docs -name index.html 2>/dev/null | wc -l) are docs pages."
	@echo "Deployable artifact: web/dist/  — copy the WHOLE directory,"
	@echo "including _astro/ and docs/. See web/README.md for why."

serve-local: bundle
	uv run nova serve --experimental

# ── Topology dashboard (v0.16.1) ─────────────────────────────────────────────

topology-build:
	cd packages/nova-dashboard && npm run build

topology-test:
	cd packages/nova-dashboard && npm test

serve-topology: topology-build
	uv run nova serve --experimental --topology

serve-topology-only:
	uv run nova serve --experimental --topology

# ── Compliance + governance smoke tests (v0.15.0, v0.16.0) ───────────────────

compliance-smoke:
	uv run pytest tests/compliance/ -v --benchmark-disable

classify-smoke:
	@echo '{"name": "test-model", "description": "smoke", "use_cases": ["text-classification"]}' > /tmp/nova-classify-smoke.yaml
	uv run nova classify run --system /tmp/nova-classify-smoke.yaml
	uv run nova classify list-vocabularies
	@rm -f /tmp/nova-classify-smoke.yaml

audit-smoke:
	uv run nova audit map --profile nist-ai-rmf
	uv run nova audit map --profile eu-ai-act-high-risk
	uv run nova audit map --profile gdpr
	uv run nova audit map --profile soc2-type2
	uv run nova audit map --profile iso42001
	uv run nova audit map --profile scientific-reproducibility

migrate-schema-smoke:
	uv run nova migrate-schema --capsule-dir "$${NOVAFABRIC_HOME:-$$HOME/.novafabric}/capsules" --dry-run

verify-smoke: ## Smoke-test nova verify on the most recent local capsule
	@echo "=== nova verify smoke ==="
	@CAPSULE=$$(ls -1t "$${NOVAFABRIC_HOME:-$$HOME/.novafabric}/capsules" 2>/dev/null | head -1); \
	if [ -z "$$CAPSULE" ]; then echo "No capsules found — run nova capture first."; exit 0; fi; \
	uv run nova verify "$${NOVAFABRIC_HOME:-$$HOME/.novafabric}/capsules/$$CAPSULE" || true

suggest-register-smoke: ## Smoke-test nova suggest-register (dry run, no auto-register)
	@echo "=== nova suggest-register smoke ==="
	uv run nova suggest-register --limit 5 || true

reports-smoke: ## Run /api/reports/* backend tests
	@echo "=== reports API smoke ==="
	uv run pytest tests/serve/test_reports.py -v

kg-smoke: ## Run /api/kg/* backend tests (KG query, audit, aliases, entity-queue)
	@echo "=== kg API smoke ==="
	uv run pytest tests/test_serve_app.py -k "kg" -v

entity-queue-smoke: ## Run /api/kg/entity-queue/* backend tests
	@echo "=== entity-queue API smoke ==="
	uv run pytest tests/test_serve_app.py -k "entity_queue" -v

init-smoke: ## Smoke-test nova init (creates dirs + Ed25519 keypair in /tmp, v0.38.1)
	@echo "=== nova init smoke ==="
	NOVAFABRIC_HOME=$$(mktemp -d) uv run nova init && echo "nova init: OK"

ingest-capsule-smoke: ## Smoke-test nova ingest-capsule --all (Scale-S3, v0.36.0)
	@echo "=== nova ingest-capsule smoke ==="
	uv run nova ingest-capsule --all --capsule-dir "$${NOVAFABRIC_HOME:-$$HOME/.novafabric}/capsules" || true

eval-list-smoke: ## Smoke-test nova eval list — shows all registered adapters (v0.40.0)
	@echo "=== nova eval list smoke ==="
	uv run nova eval list && echo "nova eval list: OK"

policy-list-smoke: ## Smoke-test nova policy list — shows Rego bundle files (v0.40.0)
	@echo "=== nova policy list smoke ==="
	uv run nova policy list && echo "nova policy list: OK"

wave2-smoke: ## Smoke-test the v0.50.0 Wave-2 CLI surfaces (evidence/incident/seal ratchet/replay intervention)
	@echo "=== Wave-2 CLI smoke ==="
	uv run nova evidence --help > /dev/null && echo "nova evidence: OK"
	uv run nova incident --help > /dev/null && echo "nova incident: OK"
	uv run nova seal ratchet --help > /dev/null && echo "nova seal ratchet: OK"
	uv run nova replay --help | grep -q intervention && echo "nova replay --mode intervention: OK"
	uv run nova lineage provenance --help | grep -q with-facets && echo "nova lineage --with-facets: OK"

collector-smoke: ## Smoke-test nova collector rebuild (v0.51.0; CLI only, no broker needed)
	@echo "=== nova collector smoke ==="
	uv run nova collector rebuild --help > /dev/null && echo "nova collector rebuild: OK"

serve-compliance-smoke: ## Run serve compliance endpoint integration tests (RoPA/AIBOM CycloneDX 1.7/NIST-RMF/AIBOM-Status, v0.37.0+v0.39.0)
	@echo "=== serve compliance endpoint smoke ==="
	uv run pytest tests/test_serve_compliance.py -v --benchmark-disable

# ── Collector (Phase 2) ──────────────────────────────────────────────────────

collector-build:
	$(MAKE) -C collector build

collector-test:
	$(MAKE) -C collector test

collector-spec-test:
	$(MAKE) -C collector spec-test

spec-test: collector-spec-test

# ── Docker stack ─────────────────────────────────────────────────────────────

# dev — Postgres + novafabric-serve only (fast, ~512 MB)
dev-up:
	$(COMPOSE) up --build -d
	$(MAKE) _wait-token
	$(MAKE) docker-token

dev-down:
	$(COMPOSE) down

dev-logs:
	$(COMPOSE) logs -f

# prod — full stack: + ClickHouse + NATS + Kafka + PgBouncer + JanusGraph
prod-up:
	$(COMPOSE_PROD) up --build -d
	$(MAKE) _wait-token
	$(MAKE) docker-token

prod-down:
	$(COMPOSE_PROD) down

prod-logs:
	$(COMPOSE_PROD) logs -f

# age — EXPERIMENTAL: standalone Apache AGE lineage backend (opt-in, not part
# of prod). See deploy/docker/docker-compose.yml's `age` service.
#
# Deliberately scoped to the single `age` service by naming it explicitly on
# every command — never bare `up`/`down`. `docker compose --profile age
# config --services` resolves to the UNION {age, postgres, nova} (postgres +
# nova have no `profiles:` key, so they're always "active" under any profile
# filter); a bare `up -d` or `down` would start/stop/remove that whole union,
# which on a host that already has the main dev/prod stack running would
# restart or destroy it as a side effect. Naming `age` explicitly on `up`,
# `stop`, and `rm` scopes each command to that one container only — verified
# live: `up -d age` / `stop age` / `rm -f age` never touch
# novafabric-postgres or novafabric-serve. `--build` is intentionally omitted
# — `age` uses a pulled image (`apache/age:...`), not a `build:` key, so
# there is nothing to build. No `_wait-token`/`docker-token` call either:
# those read the nova dashboard's token from novafabric-serve's logs, which
# this target never starts or touches.
age-up:
	$(COMPOSE_AGE) up -d age
	@echo "novafabric-age is up: postgresql://nova:nova@localhost:5433/nova_lineage"

age-down:
	$(COMPOSE_AGE) stop age
	$(COMPOSE_AGE) rm -f age

# Aliases for backwards compatibility
docker-up: dev-up
docker-down: dev-down
docker-logs: dev-logs

docker-build:
	$(COMPOSE) build nova

# ── Helm chart (deploy/helm/novafabric) ─────────────────────────────────────
helm-lint: ## Lint the NovaFabric Helm chart
	helm lint deploy/helm/novafabric

helm-template: ## Render the chart (bundled + external-DB modes) to validate templates
	helm template r deploy/helm/novafabric >/dev/null
	helm template r deploy/helm/novafabric \
		--set postgres.enabled=false \
		--set externalDatabase.host=pg.example.com \
		--set externalDatabase.password=changeme \
		--set ingress.enabled=true >/dev/null
	@echo "helm chart renders cleanly (bundled + external-DB modes)"

# git pull → rebuild nova image → rolling restart (databases untouched)
update:
	git pull
	$(COMPOSE) build nova
	$(COMPOSE) up -d postgres
	$(COMPOSE) up -d --no-deps nova
	$(MAKE) _wait-token
	$(MAKE) docker-token

# deploy-local — build & run novafabric-serve straight from the CURRENT working
# tree. No `git pull`, no GitHub round-trip: whatever is checked out here is what
# deploys. The fast dev-iteration path. Prints the working-tree commit (+ -dirty
# when there are uncommitted changes) so "what's running on n1" stays answerable.
deploy-local:
	@REV=$$(git rev-parse --short HEAD 2>/dev/null || echo nogit); \
	git diff --quiet 2>/dev/null && git diff --cached --quiet 2>/dev/null || REV="$$REV-dirty"; \
	echo "[nova] deploy-local: building novafabric-serve from working tree @ $$REV (no pull)"
	$(COMPOSE) build nova
	$(COMPOSE) up -d postgres
	$(COMPOSE) up -d --no-deps nova
	$(MAKE) _wait-token
	$(MAKE) docker-token
	@REV=$$(git rev-parse --short HEAD 2>/dev/null || echo nogit); \
	git diff --quiet 2>/dev/null && git diff --cached --quiet 2>/dev/null || REV="$$REV-dirty"; \
	echo "[nova] deploy-local complete — novafabric-serve now running working tree @ $$REV"

# Print the dashboard URL with the live token from the running container
docker-token:
	@TOKEN=$$($(COMPOSE) exec nova sh -c 'cat "$${NOVAFABRIC_HOME:-/root/.novafabric}/.serve-token"' 2>/dev/null | tr -d '[:space:]'); \
	if [ -z "$$TOKEN" ]; then \
		echo "novafabric-serve is not running or the token file is missing."; \
		echo "Start the stack with: make dev-up  (or make prod-up for the full stack)"; \
		exit 1; \
	fi; \
	echo ""; \
	echo "  Dashboard : http://localhost:4321/dashboard?token=$$TOKEN"; \
	echo "  API docs  : http://localhost:4321/api/docs?token=$$TOKEN"; \
	echo ""

# Poll until novafabric-serve writes its token file (waits for Postgres + migrations)
_wait-token:
	@echo "[nova] waiting for novafabric-serve to be ready..."; \
	i=0; \
	while ! $(COMPOSE) exec nova sh -c 'cat "$${NOVAFABRIC_HOME:-/root/.novafabric}/.serve-token"' 2>/dev/null | grep -q .; do \
		i=$$((i+1)); \
		if [ $$i -ge 60 ]; then \
			echo "[nova] timed out waiting for novafabric-serve (60s). Check: make dev-logs"; \
			exit 1; \
		fi; \
		sleep 1; \
	done
