# NovaFabric — Root Makefile
#
# Delegates to sub-project Makefiles and provides top-level convenience targets.
# Python quality gates are run via `uv run`; Go targets delegate to collector/Makefile.

COMPOSE      := docker compose -f deploy/docker/docker-compose.yml
COMPOSE_PROD := docker compose -f deploy/docker/docker-compose.yml --profile prod

.PHONY: whitepaper help test lint typecheck coverage benchmark \
        bundle serve-local \
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
        docker-up docker-build docker-down docker-logs docker-token update _wait-token

# ── Whitepaper ───────────────────────────────────────────────────────────────

whitepaper:
	@command -v pandoc >/dev/null 2>&1 || { \
		echo "[whitepaper] pandoc not found — installing via snap..."; \
		sudo snap install pandoc; \
	}
	pandoc docs/whitepaper/novafabric-position-paper.md \
	  --from markdown \
	  --to pdf \
	  --pdf-engine=xelatex \
	  -V geometry:margin=1in \
	  -V fontsize=11pt \
	  -V mainfont="DejaVu Serif" \
	  -V monofont="DejaVu Sans Mono" \
	  -o docs/whitepaper/novafabric-position-paper.pdf
	@echo "[whitepaper] → docs/whitepaper/novafabric-position-paper.pdf"

whitepaper-html:
	@python3 -c "\
import markdown, pathlib, sys; \
src = pathlib.Path('docs/whitepaper/novafabric-position-paper.md').read_text(); \
src = src[src.find('---', 3)+3:].strip() if src.startswith('---') else src; \
html = markdown.markdown(src, extensions=['tables', 'fenced_code']); \
out = pathlib.Path('docs/whitepaper/novafabric-position-paper.html'); \
style = 'body{font-family:sans-serif;max-width:900px;margin:40px auto;padding:0 20px;line-height:1.6}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:6px 12px}code{background:#f4f4f4;padding:2px 4px;border-radius:3px}pre{background:#f4f4f4;padding:12px;overflow-x:auto}pre code{background:none;padding:0}'; \
out.write_text(f'<!DOCTYPE html><html><head><meta charset=\"utf-8\"><style>{style}</style></head><body>' + html + '</body></html>'); \
print(f'[whitepaper] → {out} ({len(html)} chars)')"

# ── Help ─────────────────────────────────────────────────────────────────────

help:
	@echo "NovaFabric — available targets:"
	@echo "  test              Run Python test suite (pytest, benchmarks skipped)"
	@echo "  benchmark         Run NovaSeal p99 latency gate (100 rounds, < 200 ms)"
	@echo "  lint              Run ruff linter on src/ and tests/"
	@echo "  typecheck         Run mypy on src/"
	@echo "  whitepaper        Build PDF from docs/whitepaper/novafabric-position-paper.md"
	@echo "  whitepaper-html   Build HTML version of the whitepaper"
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

benchmark:
	mkdir -p .benchmark-results
	uv run pytest tests/seal/test_benchmark.py -v \
		--benchmark-json=.benchmark-results/seal_latency.json

lint:
	uv run ruff check src tests

typecheck:
	uv run mypy src

coverage:
	uv run pytest --cov=novafabric --cov-report=term-missing --cov-report=html

# ── Dashboard bundle ──────────────────────────────────────────────────────────

bundle:
	cd web && npm run build:dashboard

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

# Aliases for backwards compatibility
docker-up: dev-up
docker-down: dev-down
docker-logs: dev-logs

docker-build:
	$(COMPOSE) build nova

# git pull → rebuild nova image → rolling restart (databases untouched)
update:
	git pull
	$(COMPOSE) build nova
	$(COMPOSE) up -d postgres
	$(COMPOSE) up -d --no-deps nova
	$(MAKE) _wait-token
	$(MAKE) docker-token

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
