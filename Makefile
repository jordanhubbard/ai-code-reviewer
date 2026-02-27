# Angry AI - Universal Code Reviewer
#
# AI-powered code reviewer for ANY codebase (C, C++, Rust, Go, Python, etc.)
# Validates changes with YOUR build command (configurable in config.yaml).
# Talks to any OpenAI-compatible LLM provider (vLLM, OpenAI, Ollama, etc.)
#
# Quick start:
#   make config-init      # Interactive setup (configures LLM providers + source)
#   make validate         # Test connection to LLM provider
#   make run              # Start the review loop

# Python interpreter (FreeBSD typically has python3)
PYTHON?=	python3
VENV?=		.venv
VENV_PY=	$(VENV)/bin/python
VENV_PIP=	$(VENV_PY) -m pip
PIP_FLAGS?=
FREEBSD_PYYAML_PKG?=py311-pyyaml

# LLM provider settings (override with env vars or on the make command line)
# These are applied to the first provider in config.yaml
LLM_API_KEY?=

# LLM_URL priority: env var / make command-line > config.yaml > localhost:8090
_CFG_LLM_URL != if [ -n "$$LLM_URL" ]; then \
    echo "$$LLM_URL"; \
elif [ -f config.yaml ]; then \
    $(PYTHON) -c "import yaml;d=yaml.safe_load(open('config.yaml'));provs=(d.get('llm')or{}).get('providers')or[];print(provs[0]['url'] if provs else 'http://localhost:8090')" 2>/dev/null \
    || echo http://localhost:8090; \
else \
    echo http://localhost:8090; \
fi
LLM_URL ?= $(_CFG_LLM_URL)

# No directory variables needed - make runs from Makefile location
# All paths are relative to the Makefile

# Phony targets
.PHONY: all venv deps check-deps config-init config-update \
        check-llm validate run run-verbose run-forever test test-all \
        validate-persona validate-build show-metrics release clean clean-all help

# Default target
all: help

#
# Setup targets
#

# Create project-local virtual environment
venv:
	@echo "Ensuring virtual environment at $(VENV)..."
	@# Check if venv exists AND is functional (python binary works)
	@if [ ! -d "$(VENV)" ] || ! $(VENV_PY) --version >/dev/null 2>&1; then \
		if [ -d "$(VENV)" ]; then \
			echo "Existing venv is broken (stale Python). Recreating..."; \
			rm -rf "$(VENV)"; \
		fi; \
		echo "Creating virtual environment..."; \
		$(PYTHON) -m venv "$(VENV)"; \
	fi
	@# Ensure pip is available inside the venv
	@if ! $(VENV_PY) -m pip --version >/dev/null 2>&1; then \
		echo "Bootstrapping pip in venv..."; \
		$(VENV_PY) -m ensurepip --upgrade; \
	fi

# Check and install system dependencies (Python3, pip, pyyaml, beads)
check-deps:
	@echo "Checking dependencies..."
	@# Check for python3
	@if ! command -v $(PYTHON) >/dev/null 2>&1; then \
		echo "Python3 not found. Installing..."; \
		sudo pkg install -y python3; \
	else \
		echo "✓ Python3 found: $$($(PYTHON) --version)"; \
	fi
	@$(MAKE) venv
	@# Check for pyyaml in venv
	@if ! $(VENV_PY) -c "import yaml" >/dev/null 2>&1; then \
		echo "PyYAML not found in venv. Installing..."; \
		if ! $(VENV_PIP) install $(PIP_FLAGS) -r requirements.txt; then \
			echo "pip install failed."; \
			if command -v pkg >/dev/null 2>&1; then \
				echo "Trying FreeBSD pkg install: $(FREEBSD_PYYAML_PKG)"; \
				sudo pkg install -y $(FREEBSD_PYYAML_PKG); \
				echo "Recreating venv with system-site-packages..."; \
				rm -rf "$(VENV)"; \
				$(PYTHON) -m venv --system-site-packages "$(VENV)"; \
				$(MAKE) venv; \
			else \
				echo "pip install failed and pkg is unavailable."; \
				echo "Set PIP_INDEX_URL or PIP_FIND_LINKS to a reachable mirror, or fix DNS."; \
				exit 1; \
			fi; \
		fi; \
		$(VENV_PY) -c "import yaml" >/dev/null 2>&1 || { echo "PyYAML still missing in venv."; exit 1; }; \
	else \
		echo "✓ PyYAML found in venv"; \
	fi
	@# Check for httpx in venv (optional but recommended for connection pooling)
	@if ! $(VENV_PY) -c "import httpx" >/dev/null 2>&1; then \
		echo "httpx not found in venv. Installing for connection pooling..."; \
		if ! $(VENV_PIP) install $(PIP_FLAGS) "httpx>=0.25.0"; then \
			echo "⚠  httpx install failed - will use urllib fallback (no connection pooling)"; \
			echo "   Performance will be reduced by 10-20%"; \
			echo "   To fix: $(VENV_PIP) install httpx"; \
		else \
			echo "✓ httpx installed in venv (connection pooling enabled)"; \
		fi; \
	else \
		echo "✓ httpx found in venv (connection pooling enabled)"; \
	fi
	@# Check for beads (bd)
	@if ! command -v bd >/dev/null 2>&1; then \
		echo "⚠  Beads (bd) CLI not found"; \
		echo "   Install from: https://github.com/steveyegge/beads"; \
		echo "   (Optional but recommended for issue tracking)"; \
	else \
		echo "✓ Beads (bd) found: $$(bd --version 2>/dev/null || echo 'installed')"; \
	fi
	@# Check for config.yaml
	@if [ ! -f config.yaml ]; then \
		echo ""; \
		echo "⚠  config.yaml not found - run 'make config-init' to create it"; \
	fi
	@echo ""
	@echo "  [INFO] LLM provider URL: $(LLM_URL)  (override with LLM_URL=<url>)"
	@echo "  [INFO] Run 'make validate' to verify LLM connectivity"
	@echo ""
	@echo "All required dependencies satisfied!"

# Install Python dependencies (just PyYAML - no torch/GPU stuff)
# Legacy target - use check-deps instead
deps: check-deps
	@echo "Done. Dependencies installed in $(VENV)."

# Interactive configuration setup
# Creates config.yaml with user prompts, validates hosts, shows defaults
config-init:
	@bash ./scripts/config-init.sh

# Update config.yaml with new defaults from config.yaml.sample
# If config.yaml doesn't exist, runs interactive setup instead
config-update: check-deps
	@if [ ! -f config.yaml ]; then \
		echo "No config.yaml found. Running interactive setup..."; \
		./scripts/config-init.sh; \
	else \
		echo "Updating config.yaml with new defaults..."; \
		$(VENV_PY) scripts/config_update.py; \
	fi

#
# Validation targets
#

# Internal: check that at least the first LLM provider is reachable
check-llm:
	@curl -sf --max-time 5 $(LLM_URL)/v1/models >/dev/null 2>&1 || ( \
	    echo "ERROR: LLM provider not reachable at $(LLM_URL)" ; \
	    echo "  Ensure your LLM server is running, or update llm.providers in config.yaml" ; \
	    echo "  Override with: LLM_URL=http://your-server:port make validate" ; \
	    exit 1 )

# Validate LLM connection
validate: check-deps check-llm
	@echo "Validating LLM connection..."
	LLM_API_KEY="$(LLM_API_KEY)" $(VENV_PY) reviewer.py --config config.yaml --validate-only

# Run component self-tests (syntax check only, no server connection)
test: check-deps
	@echo "=== Syntax Check: All Python Modules ==="
	@$(VENV_PY) -m py_compile llm_client.py \
		async_http_client.py build_executor.py reviewer.py chunker.py index_generator.py \
		ops_logger.py scripts/config_update.py
	@echo "✓ All modules pass syntax check"
	@echo ""
	@echo "=== Import Check: LLM Client ==="
	@$(VENV_PY) -c "from llm_client import create_client_from_config, LLMClient, LLMError; print('✓ llm_client imports OK')"
	@echo ""
	@echo "=== Import Check: Build Executor ==="
	@$(VENV_PY) -c "from build_executor import create_executor_from_config; print('✓ build_executor imports OK')"
	@echo ""
	@echo "=== Unit Tests ==="
	@$(VENV_PY) -m unittest discover -s tests -p "test_*.py"
	@echo ""
	@echo "All tests passed!"

# Run full tests including LLM connectivity (requires running provider)
test-all: test check-llm
	@echo ""
	@echo "=== Testing LLM Client (requires running provider) ==="
	LLM_API_KEY="$(LLM_API_KEY)" $(VENV_PY) -c "from llm_client import create_client_from_config; import yaml; cfg = yaml.safe_load(open('config.yaml')); c = create_client_from_config(cfg); print('✓ LLM connected; models: ' + str(c.list_models()))"
	@echo ""
	@echo "=== Testing Build Executor ==="
	$(VENV_PY) build_executor.py
	@echo ""
	@echo "All component tests passed!"

#
# Run targets
#

# Run the review loop (checks dependencies and LLM provider first)
run: check-llm
	@LLM_API_KEY="$(LLM_API_KEY)" $(PYTHON) scripts/make_run.py

# Run with verbose logging
run-verbose: check-deps check-llm
	LLM_API_KEY="$(LLM_API_KEY)" $(VENV_PY) reviewer.py --config config.yaml -v

# Run in forever mode (review all directories until complete)
run-forever: check-llm
	@LLM_API_KEY="$(LLM_API_KEY)" $(PYTHON) scripts/make_run_forever.py

#
# Validation targets
#

# Validate persona files
validate-persona:
	@if [ ! -f config.yaml ]; then \
		echo "config.yaml not found. Run 'make config-init' first."; \
		exit 1; \
	fi
	@PERSONA=$$(grep "persona:" config.yaml | head -1 | sed 's/.*: *"\{0,1\}\([^"]*\)"\{0,1\}/\1/'); \
	echo "Validating persona: $$PERSONA"; \
	$(VENV_PY) persona_validator.py "$$PERSONA"

# Validate build command
validate-build:
	@if [ ! -f config.yaml ]; then \
		echo "config.yaml not found. Run 'make config-init' first."; \
		exit 1; \
	fi
	@SOURCE_ROOT=$$(grep "root:" config.yaml | grep -v "#" | head -1 | sed 's/.*: *"\{0,1\}\([^"]*\)"\{0,1\}/\1/'); \
	BUILD_CMD=$$(grep "build_command:" config.yaml | head -1 | sed 's/.*: *"\{0,1\}\([^"]*\)"\{0,1\}/\1/'); \
	echo "Validating build command for: $$SOURCE_ROOT"; \
	echo "Build command: $$BUILD_CMD"; \
	$(VENV_PY) build_validator.py "$$SOURCE_ROOT" "$$BUILD_CMD"

# Show persona effectiveness metrics
show-metrics:
	@if [ ! -f config.yaml ]; then \
		echo "config.yaml not found. Run 'make config-init' first."; \
		exit 1; \
	fi
	@SOURCE_ROOT=$$(grep "root:" config.yaml | grep -v "#" | head -1 | sed 's/.*: *"\{0,1\}\([^"]*\)"\{0,1\}/\1/'); \
	echo "Showing metrics for: $$SOURCE_ROOT"; \
	$(VENV_PY) scripts/show_metrics.py "$$SOURCE_ROOT"

#
# Release target
#

# Create a new release: runs tests, updates CHANGELOG, tags, and pushes release via gh CLI
# Bumps minor version (0.1 -> 0.2 -> 0.3, etc.) or starts at 0.1 if no previous release
release: test
	@echo ""
	@echo "=== Creating New Release ==="
	@# Check for uncommitted changes
	@if [ -n "$$(git status --porcelain)" ]; then \
		echo "ERROR: Uncommitted changes detected. Commit or stash them first."; \
		git status --short; \
		exit 1; \
	fi
	@# Check for unpushed commits on the current branch
	@BRANCH=$$(git rev-parse --abbrev-ref HEAD); \
	if ! git rev-parse --verify "origin/$$BRANCH" >/dev/null 2>&1; then \
		echo "ERROR: Branch '$$BRANCH' has no upstream. Push it first: git push -u origin $$BRANCH"; \
		exit 1; \
	fi; \
	UNPUSHED=$$(git rev-list "origin/$$BRANCH..HEAD" --count); \
	if [ "$$UNPUSHED" -gt 0 ]; then \
		echo "ERROR: $$UNPUSHED unpushed commit(s) on branch '$$BRANCH'. Run 'git push' first."; \
		git log "origin/$$BRANCH..HEAD" --oneline; \
		exit 1; \
	fi
	@# Check gh CLI is available and authenticated
	@if ! command -v gh >/dev/null 2>&1; then \
		echo "ERROR: gh CLI not found. Install from https://cli.github.com/"; \
		exit 1; \
	fi
	@if ! gh auth status >/dev/null 2>&1; then \
		echo "ERROR: gh CLI not authenticated. Run 'gh auth login' first."; \
		exit 1; \
	fi
	@# Determine next version from the HIGHEST of local tags and GitHub releases
	@LATEST_TAG=$$(git tag -l 'v0.*' | sed 's/^v//' | sort -t. -k1,1n -k2,2n | tail -1); \
	LATEST_RELEASE=$$(gh release list --limit 1 --json tagName --jq '.[0].tagName // empty' 2>/dev/null | sed 's/^v//'); \
	if [ -z "$$LATEST_TAG" ] && [ -z "$$LATEST_RELEASE" ]; then \
		NEW_VERSION="0.1"; \
		echo "No previous version found. Starting at v$$NEW_VERSION"; \
	else \
		TAG_MINOR=$$(echo "$${LATEST_TAG:-0.0}" | cut -d. -f2); \
		REL_MINOR=$$(echo "$${LATEST_RELEASE:-0.0}" | cut -d. -f2); \
		if [ "$$TAG_MINOR" -gt "$$REL_MINOR" ] 2>/dev/null; then \
			HIGHEST="$$LATEST_TAG"; \
		else \
			HIGHEST="$$LATEST_RELEASE"; \
		fi; \
		MAJOR=$$(echo "$$HIGHEST" | cut -d. -f1); \
		MINOR=$$(echo "$$HIGHEST" | cut -d. -f2); \
		NEW_MINOR=$$((MINOR + 1)); \
		NEW_VERSION="$$MAJOR.$$NEW_MINOR"; \
		echo "Highest existing version: v$$HIGHEST -> New release: v$$NEW_VERSION"; \
	fi; \
	echo ""; \
	if git rev-parse "v$$NEW_VERSION" >/dev/null 2>&1; then \
		echo "ERROR: Tag v$$NEW_VERSION already exists locally."; \
		echo "  Points to: $$(git log v$$NEW_VERSION --oneline -1)"; \
		echo "  HEAD is:   $$(git log HEAD --oneline -1)"; \
		echo "  To fix: git tag -d v$$NEW_VERSION && git push origin :refs/tags/v$$NEW_VERSION"; \
		exit 1; \
	fi; \
	if gh release view "v$$NEW_VERSION" >/dev/null 2>&1; then \
		echo "ERROR: GitHub release v$$NEW_VERSION already exists."; \
		echo "  To fix: gh release delete v$$NEW_VERSION --yes"; \
		exit 1; \
	fi; \
	TODAY=$$(date +%Y-%m-%d); \
	if grep -q '## \[Unreleased\]' CHANGELOG.md; then \
		echo "Updating CHANGELOG.md: [Unreleased] -> [$$NEW_VERSION] - $$TODAY"; \
		awk -v ver="$$NEW_VERSION" -v date="$$TODAY" \
			'/^## \[Unreleased\]/{print; print ""; print "## [" ver "] - " date; next} {print}' \
			CHANGELOG.md > CHANGELOG.md.tmp && mv CHANGELOG.md.tmp CHANGELOG.md; \
		git add CHANGELOG.md; \
		git commit -m "Update CHANGELOG for v$$NEW_VERSION"; \
		git push; \
	else \
		echo "WARNING: No [Unreleased] section found in CHANGELOG.md, skipping update"; \
	fi; \
	echo "Creating tag v$$NEW_VERSION..."; \
	if ! git tag -a "v$$NEW_VERSION" -m "Release v$$NEW_VERSION"; then \
		echo "ERROR: Failed to create tag v$$NEW_VERSION"; \
		exit 1; \
	fi; \
	echo "Pushing tag to origin..."; \
	if ! git push origin "v$$NEW_VERSION"; then \
		echo "ERROR: Failed to push tag. Cleaning up local tag..."; \
		git tag -d "v$$NEW_VERSION"; \
		exit 1; \
	fi; \
	echo "Creating GitHub release..."; \
	if ! gh release create "v$$NEW_VERSION" \
		--title "v$$NEW_VERSION" \
		--generate-notes; then \
		echo "ERROR: Failed to create GitHub release. Tag v$$NEW_VERSION was pushed."; \
		echo "  To clean up: git tag -d v$$NEW_VERSION && git push origin :refs/tags/v$$NEW_VERSION"; \
		exit 1; \
	fi; \
	echo ""; \
	echo "✓ Release v$$NEW_VERSION created successfully!"; \
	echo "  View at: $$(gh release view v$$NEW_VERSION --json url --jq '.url')"

#
# Cleanup targets
#

# Clean logs and Python cache
clean:
	rm -rf ../.ai-code-reviewer/logs/*.txt
	rm -rf __pycache__
	rm -rf *.pyc

# Deep clean - also remove any leftover model weights (they belong on the backend LLM server)
clean-all: clean
	@if [ -d "Qwen2.5-Coder-32B-Instruct" ]; then \
		echo "Removing local model weights (these should be on the LLM backend)..."; \
		rm -rf Qwen2.5-Coder-32B-Instruct; \
		echo "Done."; \
	fi

#
# Help
#

help:
	@echo "Angry AI - Universal Code Reviewer"
	@echo "==================================="
	@echo ""
	@echo "AI-powered code reviewer with build validation for ANY codebase."
	@echo "Talks to any OpenAI-compatible LLM provider (vLLM, OpenAI, Ollama, etc.)"
	@echo ""
	@echo "Setup:"
	@echo "  make check-deps       Check/install Python3, pip, and PyYAML"
	@echo "  make config-init      Interactive setup wizard (LLM providers + source config)"
	@echo "  make config-update    Merge new defaults into config.yaml"
	@echo ""
	@echo "Usage:"
	@echo "  make validate      Test connection to LLM provider"
	@echo "  make run           Start the review loop"
	@echo "  make run-verbose   Run with verbose logging"
	@echo "  make run-forever   Run until all directories are reviewed"
	@echo "  make test          Run syntax and import tests (no server required)"
	@echo "  make test-all      Run all tests including LLM connectivity"
	@echo ""
	@echo "Validation:"
	@echo "  make validate-persona  Validate persona files"
	@echo "  make validate-build    Validate build command matches project type"
	@echo "  make show-metrics      Show persona effectiveness metrics"
	@echo ""
	@echo "Release:"
	@echo "  make release      Run tests, update CHANGELOG, tag, and create GitHub release"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean        Remove logs and Python cache"
	@echo "  make clean-all    Also remove any leftover files"
	@echo ""
	@echo "Options:"
	@echo "  CONFIG=path           Use alternate config file (default: config.yaml)"
	@echo "  PYTHON=path           Use alternate Python interpreter (default: python3)"
	@echo "  LLM_URL=url           First LLM provider URL (default: http://localhost:8090)"
	@echo "  LLM_API_KEY=key       API key for first provider (or set in config.yaml)"
	@echo ""
	@echo "Requirements:"
	@echo "  - Python 3.8+ with PyYAML (auto-installed by check-deps)"
	@echo "  - At least one running OpenAI-compatible LLM provider"
	@echo "  - Source code at source.root (default: ../)"
	@echo "  - Working build command (configured in config.yaml)"
	@echo ""
	@echo "First time? Run 'make config-init' to set everything up!"
	@echo ""
	@echo "Works with: C/C++ (make/cmake), Rust, Go, Python, Node.js, etc."
