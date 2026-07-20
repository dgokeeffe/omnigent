# Omnigent → Databricks Apps deploy driver.
#
# Encodes the working sequence from the databricks-app-deploy skill:
#   build (proxy)  →  normalize lock (public PyPI)  →  bundle deploy  →
#   bundle run  →  health check.
#
# Per-workspace values live in profiles/<name>.mk (see profiles/_template.mk).
# Select one with PROFILE=<name>. Default is aws-syd.
#
#   make deploy   PROFILE=aws-syd      # full build + deploy + run + health
#   make redeploy PROFILE=aws-syd      # reuse existing wheels (skip build)
#   make health   PROFILE=aws-syd      # just hit /health
#   make config   PROFILE=aws-daveok   # print resolved vars for a profile
#   make list-profiles
#
# Add a new workspace:
#   1. cp profiles/_template.mk profiles/<name>.mk  and fill it in
#   2. add a `<name>:` target block to databricks.yml (literal workspace.host)
#   3. make deploy PROFILE=<name>

PROFILE ?= aws-syd

# Pull in the selected profile's variables (TARGET, APP_NAME, VOLUME_NAME, ...).
_PROFILE_MK := profiles/$(PROFILE).mk
ifeq ($(wildcard $(_PROFILE_MK)),)
$(error unknown PROFILE '$(PROFILE)' — no $(_PROFILE_MK). Run 'make list-profiles')
endif
include $(_PROFILE_MK)

# --- fixed environment facts (see skill) ------------------------------------
# pypi.org is blocked locally; build + lock through the Databricks proxy.
PROXY_INDEX := https://pypi-proxy.cloud.databricks.com/simple/
REPO_ROOT   := $(abspath ../..)
SRC_LOCK    := deploy/databricks/src/uv.lock
PY          := uv run python

# uv proxy env, applied to build/lock steps only.
BUILD_ENV := UV_INDEX_URL=$(PROXY_INDEX) UV_DEFAULT_INDEX=$(PROXY_INDEX)

# --var set shared by bundle deploy / bundle run (must match deploy.py).
VARS := --var app_name=$(APP_NAME) \
        --var lakebase_branch=$(LAKEBASE_BRANCH) \
        --var lakebase_database=$(LAKEBASE_DATABASE) \
        --var volume_name=$(VOLUME_NAME) \
        --var otel_table_schema=$(OTEL_TABLE_SCHEMA)

# Derived Lakebase grant inputs (note underscore DB name vs hyphen resource path).
LAKEBASE_ENDPOINT   := projects/$(APP_NAME)/branches/production/endpoints/primary
DATABASE_UNDERSCORE := databricks_postgres

# /Volumes path form of VOLUME_NAME (catalog.schema.volume -> catalog/schema/volume).
VOLUME_ROOT := /Volumes/$(subst .,/,$(VOLUME_NAME))

.DEFAULT_GOAL := help

.PHONY: help list-profiles config build normalize check-lock \
        bundle-deploy bundle-run deploy redeploy health grant publish clean

help: ## Show this help
	@echo "Omnigent Databricks deploy — PROFILE=$(PROFILE) (target=$(TARGET))"
	@echo
	@grep -hE '^[a-z-]+:.*## ' $(firstword $(MAKEFILE_LIST)) | \
		awk 'BEGIN{FS=":.*## "}{printf "  %-14s %s\n", $$1, $$2}'
	@echo
	@echo "Profiles: $(patsubst profiles/%.mk,%,$(filter-out profiles/_template.mk,$(wildcard profiles/*.mk)))"

list-profiles: ## List available deploy profiles
	@for f in $(filter-out profiles/_template.mk,$(wildcard profiles/*.mk)); do \
		echo "  $${f#profiles/}" | sed 's/\.mk//'; done

config: ## Print the resolved variables for PROFILE
	@echo "PROFILE           = $(PROFILE)"
	@echo "TARGET            = $(TARGET)"
	@echo "APP_NAME          = $(APP_NAME)"
	@echo "LAKEBASE_BRANCH   = $(LAKEBASE_BRANCH)"
	@echo "LAKEBASE_DATABASE = $(LAKEBASE_DATABASE)"
	@echo "VOLUME_NAME       = $(VOLUME_NAME)"
	@echo "OTEL_TABLE_SCHEMA = $(OTEL_TABLE_SCHEMA)"
	@echo "APP_URL           = $(APP_URL)"

build: ## Build wheels + generate lock via deploy.py (proxy). Bundle run 404 is expected.
	cd $(REPO_ROOT) && $(BUILD_ENV) $(PY) deploy/databricks/deploy.py \
		--app-name $(APP_NAME) --profile $(PROFILE) --target $(TARGET) \
		--lakebase-branch $(LAKEBASE_BRANCH) \
		--lakebase-database $(LAKEBASE_DATABASE) \
		--volume-name $(VOLUME_NAME) \
		--otel-table-schema $(OTEL_TABLE_SCHEMA) \
		--allow-dirty --no-smoke-check || true

normalize: ## Rewrite the proxy lock URLs to public PyPI (fixes in-container 404)
	@# The normalizer exits 1 when it rewrites the file (pre-commit convention);
	@# that's success here. check-lock is the authoritative gate.
	-cd $(REPO_ROOT) && $(PY) scripts/normalize_uv_lock_registry.py $(SRC_LOCK)
	@$(MAKE) --no-print-directory check-lock

check-lock: ## Verify src/uv.lock has no proxy URLs left
	cd $(REPO_ROOT) && $(PY) scripts/normalize_uv_lock_registry.py --check $(SRC_LOCK)

bundle-deploy: ## databricks bundle deploy (uses the normalized lock)
	databricks bundle deploy --target $(TARGET) --profile $(PROFILE) $(VARS)

bundle-run: ## databricks bundle run (start/restart the app)
	databricks bundle run $(APP_NAME) --target $(TARGET) --profile $(PROFILE) $(VARS)

deploy: build normalize bundle-deploy bundle-run health ## Full deploy: build → normalize → deploy → run → health

redeploy: normalize bundle-deploy bundle-run health ## Redeploy reusing existing src/ wheels (skip build)

# Retries because a fresh bundle run 502s for a bit while the container reinstalls.
HEALTH_RETRIES ?= 15
HEALTH_SLEEP   ?= 20

health: ## Hit /health on the deployed app (retries through boot-time 502s)
	@test -n "$(APP_URL)" || { echo "APP_URL not set for PROFILE=$(PROFILE)"; exit 1; }
	@TOKEN="$$(databricks auth token $(PROFILE) --output json \
		| $(PY) -c 'import json,sys;print(json.load(sys.stdin)["access_token"])')"; \
	echo "GET $(APP_URL)/health"; \
	for i in $$(seq 1 $(HEALTH_RETRIES)); do \
		body=$$(curl --http1.1 -s -w '\n%{http_code}' -H "Authorization: Bearer $$TOKEN" $(APP_URL)/health); \
		code=$$(echo "$$body" | tail -1); \
		if [ "$$code" = "200" ]; then echo "$$body" | head -1; echo "healthy (200)"; exit 0; fi; \
		echo "  attempt $$i/$(HEALTH_RETRIES): HTTP $$code — retrying in $(HEALTH_SLEEP)s"; \
		sleep $(HEALTH_SLEEP); \
	done; \
	echo "app did not become healthy after $(HEALTH_RETRIES) attempts"; exit 1

publish: ## Upload built dist/*.whl to <volume>/wheels/<version>/ for external consumers
	@cd $(REPO_ROOT) && ver=$$(ls dist/omnigent-*-py3-none-any.whl 2>/dev/null \
		| head -1 | sed -E 's|.*/omnigent-(.*)-py3-none-any\.whl|\1|'); \
	test -n "$$ver" || { echo "no wheels in dist/ — run 'make build PROFILE=$(PROFILE)' first"; exit 1; }; \
	dest="$(VOLUME_ROOT)/wheels/$$ver"; \
	echo "publishing dist/*.whl → $$dest"; \
	databricks fs mkdir "dbfs:$$dest" --profile $(PROFILE); \
	for w in dist/*.whl; do \
		echo "  $$w"; \
		databricks fs cp "$$w" "dbfs:$$dest/$$(basename $$w)" --overwrite --profile $(PROFILE); \
	done; \
	echo "volume now contains:"; databricks fs ls "dbfs:$$dest" --profile $(PROFILE)

grant: ## Grant the app SP Lakebase perms (first-time bootstrap)
	cd $(REPO_ROOT) && $(PY) deploy/databricks/grant_sp_perms.py \
		--app-name $(APP_NAME) \
		--lakebase-endpoint $(LAKEBASE_ENDPOINT) \
		--database $(DATABASE_UNDERSCORE) \
		--profile $(PROFILE)

clean: ## Remove per-deploy build artifacts under src/
	rm -f deploy/databricks/src/*.whl src/pyproject.toml src/uv.lock 2>/dev/null || true
	rm -f src/*.whl 2>/dev/null || true
