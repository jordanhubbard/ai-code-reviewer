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

# No directory variables needed - make runs from Makefile location
# All paths are relative to the Makefile

# Phony targets
.PHONY: all deps check-deps config-update validate run run-verbose test test-all release clean clean-all help

# Default target
all: help

#
# Setup targets
#

# Check and install system dependencies (Python3, pip, pyyaml, beads)
check-deps:
	@echo "Checking dependencies..."
	@# Check for python3
	@if ! command -v python3 >/dev/null 2>&1; then \
		echo "Python3 not found. Installing..."; \
		sudo pkg install -y python3; \
	else \
		echo "✓ Python3 found: $$(python3 --version)"; \
	fi
	@# Check for pip
	@if ! python3 -m pip --version >/dev/null 2>&1; then \
		echo "pip not found. Installing..."; \
		sudo pkg install -y py311-pip; \
	else \
		echo "✓ pip found: $$(python3 -m pip --version)"; \
	fi
	@# Check for pyyaml
	@if ! python3 -c "import yaml" >/dev/null 2>&1; then \
		echo "PyYAML not found. Installing..."; \
		sudo pip install pyyaml; \
	else \
		echo "✓ PyYAML found"; \
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
deps:
	@echo "Installing Python dependencies..."
	$(PYTHON) -m pip install --user -r requirements.txt
	@echo "Done. Dependencies installed."

# Update config.yaml with new defaults from config.yaml.defaults
# If config.yaml doesn't exist, creates it from defaults
config-update:
	@if [ ! -f config.yaml ]; then \
		echo "Creating config.yaml from defaults..."; \
		cp config.yaml.defaults config.yaml; \
		echo ""; \
		echo "*** config.yaml created - do not forget to customize it! ***"; \
		echo "    At minimum, set your Ollama server URL:"; \
		echo "      ollama.url: \"http://your-ollama-server:11434\""; \
		echo ""; \
	else \
		echo "Updating config.yaml with new defaults..."; \
		$(PYTHON) scripts/config_update.py; \
	fi

#
# Validation targets
#

# Validate Ollama connection and model availability
validate: check-deps
	@echo "Validating Ollama connection..."
	$(PYTHON) reviewer.py --config config.yaml --validate-only

# Run component self-tests (syntax check only, no server connection)
test:
	@echo "=== Syntax Check: All Python Modules ==="
	@$(PYTHON) -m py_compile ollama_client.py vllm_client.py llm_client.py \
		build_executor.py reviewer.py chunker.py index_generator.py ops_logger.py \
		scripts/config_update.py
	@echo "✓ All modules pass syntax check"
	@echo ""
	@echo "=== Import Check: LLM Client ==="
	@$(PYTHON) -c "from llm_client import create_client_from_config, MultiHostClient, LLMError; print('✓ llm_client imports OK')"
	@echo ""
	@echo "=== Import Check: Build Executor ==="
	@$(PYTHON) -c "from build_executor import create_executor_from_config; print('✓ build_executor imports OK')"
	@echo ""
	@echo "=== Config Migration Test ==="
	@$(PYTHON) -c "\
import yaml; \
from scripts.config_update import migrate_ollama_to_llm; \
cfg = {'ollama': {'url': 'http://test:11434', 'model': 'test-model'}}; \
migrate_ollama_to_llm(cfg); \
assert 'llm' in cfg and 'ollama' not in cfg, 'Migration failed'; \
assert 'nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16' in cfg['llm']['models'], 'Missing preferred model'; \
print('✓ Config migration OK')"
	@echo ""
	@echo "All tests passed!"

# Run full tests including server connectivity (requires running Ollama/vLLM)
test-all: test
	@echo ""
	@echo "=== Testing Ollama Client (requires server) ==="
	$(PYTHON) ollama_client.py
	@echo ""
	@echo "=== Testing Build Executor ==="
	$(PYTHON) build_executor.py
	@echo ""
	@echo "All component tests passed!"

#
# Run targets
#

# Run the review loop (checks dependencies first, auto-updates config if defaults are newer)
run: check-deps
	@# Auto-update config.yaml if defaults are newer
	@if [ -f config.yaml ] && [ -f config.yaml.defaults ] && [ config.yaml.defaults -nt config.yaml ]; then \
		echo "*** config.yaml.defaults is newer than config.yaml"; \
		echo "*** Running config-update to merge new settings..."; \
		$(MAKE) config-update; \
	fi
	$(PYTHON) reviewer.py --config config.yaml

# Run with verbose logging
run-verbose:
	$(PYTHON) reviewer.py --config config.yaml -v

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
	@echo "  make config-update  Create or update config.yaml with new defaults"
	@echo "  vim config.yaml     Configure Ollama server URL and model"
	@echo "  make validate       Test connection to Ollama server"
	@echo ""
	@echo "Usage:"
	@echo "  make run          Start the review loop (auto-checks dependencies)"
	@echo "  make run-verbose  Run with verbose logging"
	@echo "  make test         Run syntax and import tests (no server required)"
	@echo "  make test-all     Run all tests including server connectivity"
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
	@echo "  - Network access to Ollama server"
	@echo "  - Source code at source.root (default: ../)"
	@echo "  - Working build command (configured in config.yaml)"
	@echo ""
	@echo "Works with: C/C++ (make/cmake), Rust, Go, Python, Node.js, etc."
	@echo "Just configure your build command in config.yaml!"
