# Angry AI - Universal Code Reviewer
#
# AI-powered code reviewer for ANY codebase (C, C++, Rust, Go, Python, etc.)
# Validates changes with YOUR build command (configurable in config.yaml).
#
# Quick start:
#   make deps        # Install Python dependencies (PyYAML)
#   vim config.yaml  # Set your Ollama server URL
#   make validate    # Test connection to Ollama
#   make run         # Start the review loop

# Python interpreter (FreeBSD typically has python3)
PYTHON?=	python3
VENV?=		.venv
VENV_PY=	$(VENV)/bin/python
VENV_PIP=	$(VENV_PY) -m pip
PIP_FLAGS?=
FREEBSD_PYYAML_PKG?=py311-pyyaml

# No directory variables needed - make runs from Makefile location
# All paths are relative to the Makefile

# Phony targets
.PHONY: all venv deps check-deps config-init config-update validate run run-verbose run-forever test test-all validate-persona validate-build show-metrics release clean clean-all help

# Default target
all: help

#
# Setup targets
#

# Create project-local virtual environment
venv:
	@echo "Ensuring virtual environment at $(VENV)..."
	@if [ ! -d "$(VENV)" ]; then \
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
		echo "⚠  config.yaml not found - will be created on first run"; \
		echo "   You'll need to edit it to set:"; \
		echo "   - Ollama server URL"; \
		echo "   - Source tree path"; \
		echo "   - Build command"; \
	fi
	@echo "All required dependencies satisfied!"

# Install Python dependencies (just PyYAML - no torch/GPU stuff)
# Legacy target - use check-deps instead
deps: check-deps
	@echo "Done. Dependencies installed in $(VENV)."

# Interactive configuration setup
# Creates config.yaml with user prompts, validates hosts, shows defaults
config-init:
	@bash ./scripts/config-init.sh

# Update config.yaml with new defaults from config.yaml.defaults
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

# Validate Ollama connection and model availability
validate: check-deps
	@echo "Validating Ollama connection..."
	$(VENV_PY) reviewer.py --config config.yaml --validate-only

# Run component self-tests (syntax check only, no server connection)
test: check-deps
	@echo "=== Syntax Check: All Python Modules ==="
	@$(VENV_PY) -m py_compile ollama_client.py vllm_client.py llm_client.py \
		build_executor.py reviewer.py chunker.py index_generator.py ops_logger.py \
		scripts/config_update.py
	@echo "✓ All modules pass syntax check"
	@echo ""
	@echo "=== Import Check: LLM Client ==="
	@$(VENV_PY) -c "from llm_client import create_client_from_config, MultiHostClient, LLMError; print('✓ llm_client imports OK')"
	@echo ""
	@echo "=== Import Check: Build Executor ==="
	@$(VENV_PY) -c "from build_executor import create_executor_from_config; print('✓ build_executor imports OK')"
	@echo ""
	@echo "=== Unit Tests ==="
	@$(VENV_PY) -m unittest discover -s tests -p "test_*.py"
	@echo ""
	@echo "=== Config Migration Test ==="
	@$(VENV_PY) -c "import yaml; from scripts.config_update import migrate_ollama_to_llm; cfg = {'ollama': {'url': 'http://test:11434', 'model': 'test-model'}}; migrate_ollama_to_llm(cfg); assert 'llm' in cfg and 'ollama' not in cfg, 'Migration failed'; assert 'nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16' in cfg['llm']['models'], 'Missing preferred model'; print('✓ Config migration OK')"
	@echo ""
	@echo "All tests passed!"

# Run full tests including server connectivity (requires running Ollama/vLLM)
test-all: test
	@echo ""
	@echo "=== Testing Ollama Client (requires server) ==="
	$(VENV_PY) ollama_client.py
	@echo ""
	@echo "=== Testing Build Executor ==="
	$(VENV_PY) build_executor.py
	@echo ""
	@echo "All component tests passed!"

#
# Run targets
#

# Run the review loop (checks dependencies first, auto-creates config if missing)
run:
	@$(PYTHON) scripts/make_run.py

# Run with verbose logging
run-verbose: check-deps
	$(VENV_PY) reviewer.py --config config.yaml -v

# Run in forever mode (review all directories until complete)
run-forever: check-deps
	$(VENV_PY) reviewer.py --config config.yaml --forever

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

# Create a new release: runs tests, tags, and pushes release via gh CLI
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
	@# Check gh CLI is available and authenticated
	@if ! command -v gh >/dev/null 2>&1; then \
		echo "ERROR: gh CLI not found. Install from https://cli.github.com/"; \
		exit 1; \
	fi
	@if ! gh auth status >/dev/null 2>&1; then \
		echo "ERROR: gh CLI not authenticated. Run 'gh auth login' first."; \
		exit 1; \
	fi
	@# Get latest release version and bump it
	@LATEST=$$(gh release list --limit 1 --json tagName --jq '.[0].tagName // empty' 2>/dev/null | sed 's/^v//'); \
	if [ -z "$$LATEST" ]; then \
		NEW_VERSION="0.1"; \
		echo "No previous release found. Starting at v$$NEW_VERSION"; \
	else \
		MAJOR=$$(echo "$$LATEST" | cut -d. -f1); \
		MINOR=$$(echo "$$LATEST" | cut -d. -f2); \
		NEW_MINOR=$$((MINOR + 1)); \
		NEW_VERSION="$$MAJOR.$$NEW_MINOR"; \
		echo "Previous release: v$$LATEST -> New release: v$$NEW_VERSION"; \
	fi; \
	echo ""; \
	echo "Creating tag v$$NEW_VERSION..."; \
	git tag -a "v$$NEW_VERSION" -m "Release v$$NEW_VERSION"; \
	echo "Pushing tag to origin..."; \
	git push origin "v$$NEW_VERSION"; \
	echo "Creating GitHub release..."; \
	gh release create "v$$NEW_VERSION" \
		--title "v$$NEW_VERSION" \
		--generate-notes; \
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

# Deep clean - also remove any leftover model weights (they belong on Ollama server)
clean-all: clean
	@if [ -d "Qwen2.5-Coder-32B-Instruct" ]; then \
		echo "Removing local model weights (these should be on Ollama server)..."; \
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
	@echo "Uses remote Ollama server for AI, validates with YOUR build command."
	@echo ""
	@echo "Setup:"
	@echo "  make check-deps     Check/install Python3, pip, and PyYAML (auto-runs on 'make run')"
	@echo "  make config-init    Interactive setup wizard (creates/updates config.yaml)"
	@echo "  make config-update  Merge new defaults into existing config.yaml"
	@echo "  make validate       Test connection to LLM server"
	@echo ""
	@echo "Usage:"
	@echo "  make run            Start the review loop (auto-checks dependencies)"
	@echo "  make run-verbose    Run with verbose logging"
	@echo "  make run-forever    Run until all directories are reviewed"
	@echo "  make test           Run syntax and import tests (no server required)"
	@echo "  make test-all       Run all tests including server connectivity"
	@echo ""
	@echo "Validation:"
	@echo "  make validate-persona  Validate persona files (AI_START_HERE.md, etc.)"
	@echo "  make validate-build    Validate build command matches project type"
	@echo "  make show-metrics      Show persona effectiveness metrics"
	@echo ""
	@echo "Release:"
	@echo "  make release      Run tests, tag, and create GitHub release (bumps version)"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean        Remove logs and Python cache"
	@echo "  make clean-all    Also remove any leftover model weights"
	@echo ""
	@echo "Options:"
	@echo "  CONFIG=path       Use alternate config file (default: config.yaml)"
	@echo "  PYTHON=path       Use alternate Python interpreter (default: python3)"
	@echo ""
	@echo "Requirements:"
	@echo "  - Python 3.8+ with PyYAML (auto-installed by check-deps)"
	@echo "  - Network access to vLLM or Ollama server"
	@echo "  - Source code at source.root (default: ../)"
	@echo "  - Working build command (configured in config.yaml)"
	@echo ""
	@echo "First time? Just run 'make run' - it will guide you through setup!"
	@echo ""
	@echo "Works with: C/C++ (make/cmake), Rust, Go, Python, Node.js, etc."
	@echo "Just configure your build command in config.yaml!"
