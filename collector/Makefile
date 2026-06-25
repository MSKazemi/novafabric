GOBIN   := $(HOME)/go/bin
GO      := go
# Use a project-local GOPATH to avoid polluting the global module cache
GOPATH_LOCAL := $(HOME)/gopath

.PHONY: build test test-race test-coverage test-integration spec-test lint \
        build-images build-tarball eval-smoke roundtrip-test

# ── Build ────────────────────────────────────────────────────────────────────

build:
	GOPATH=$(GOPATH_LOCAL) $(GO) build -o bin/novafabric-collector ./cmd/novafabric-collector
	GOPATH=$(GOPATH_LOCAL) $(GO) build -o bin/novafabric-verifier  ./cmd/novafabric-verifier
	GOPATH=$(GOPATH_LOCAL) $(GO) build -o bin/novafabric-hpc-hub   ./cmd/novafabric-hpc-hub

# ── Test ─────────────────────────────────────────────────────────────────────

test:
	GOPATH=$(GOPATH_LOCAL) $(GO) test -race -count=1 ./...

test-race: test

test-coverage:
	GOPATH=$(GOPATH_LOCAL) $(GO) test -race -coverprofile=coverage.out ./...
	$(GO) tool cover -func coverage.out

# ── Cross-language canonical round-trip ──────────────────────────────────────

roundtrip-test: ## Run 10K cross-language canonical round-trip (requires python3 + cryptography)
	GOPATH=$(GOPATH_LOCAL) $(GO) test -v -run TestCanonicalEncode_CrossLanguageRoundTrip10K ./pkg/canonical/

# ── Spec test (corpus validation) ────────────────────────────────────────────

spec-test:
	@echo "==> Validating 1000 corpus events against envelope-v1.json …"
	cd ../schemas/event-envelope-v1/corpus && \
	python3 -c "\
import json, pathlib, sys; \
schema = json.load(open('../envelope-v1.json')); \
required = schema['required']; \
files = sorted(pathlib.Path('.').glob('[0-9]*.json')); \
errors = []; \
[errors.append(str(f) + ': missing ' + fld) \
 for f in files for fld in required if fld not in json.load(open(f))]; \
[print('FAIL:', e) for e in errors]; \
sys.exit(1) if errors else print(f'{len(files)}/{len(files)} corpus events OK') \
"

# ── Lint ─────────────────────────────────────────────────────────────────────

lint:
	GOPATH=$(GOPATH_LOCAL) $(GO) vet ./...

# ── Integration tests (require Docker) ───────────────────────────────────────

test-integration:
	@echo "Integration tests require Docker; run manually:"
	@echo "  cd tests/integration && $(GO) test -tags=integration -timeout=10m -v ./..."

# ── Eval smoke (requires running collector) ───────────────────────────────────

eval-smoke:
	@echo "Eval smoke: tc-001 tc-003 tc-005 tc-006 (requires running collector)"
	GOPATH=$(GOPATH_LOCAL) $(GO) test -run TestEvalSmoke -v ./tests/integration/...

# ── Distribution ─────────────────────────────────────────────────────────────

build-images:
	docker build -t ghcr.io/novafabric/novafabric-collector:dev .

build-tarball: build
	tar -czf novafabric-collector.tar.gz bin/
	@echo "Tarball: novafabric-collector.tar.gz"
