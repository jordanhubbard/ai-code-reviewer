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

# Source directory
# Note: $(CURDIR) is GNU Make, ${.CURDIR} is BSD make
# Use CURDIR if set, otherwise use current directory
SRCDIR?=	$(CURDIR)
ifeq ($(SRCDIR),)
SRCDIR=		$(shell pwd)
endif

# Configuration file (relative to source directory)
CONFIG?=	$(SRCDIR)/config.yaml

# Phony targets
.PHONY: all deps check-deps validate run run-verbose test clean clean-all help

# Default target
all: help

#
# Setup targets
#

# Check and install system dependencies (Python3, pip, pyyaml)
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
	@echo "All dependencies satisfied!"

# Install Python dependencies (just PyYAML - no torch/GPU stuff)
# Legacy target - use check-deps instead
deps:
	@echo "Installing Python dependencies..."
	$(PYTHON) -m pip install --user -r $(SRCDIR)/requirements.txt
	@echo "Done. Dependencies installed."

#
# Validation targets
#

# Validate Ollama connection and model availability
validate: check-deps
	@echo "Validating Ollama connection..."
	cd $(SRCDIR) && $(PYTHON) reviewer.py --config $(CONFIG) --validate-only

# Run component self-tests
test:
	@echo "=== Testing Ollama Client ==="
	cd $(SRCDIR) && $(PYTHON) ollama_client.py
	@echo ""
	@echo "=== Testing Build Executor ==="
	cd $(SRCDIR) && $(PYTHON) build_executor.py
	@echo ""
	@echo "All component tests passed!"

#
# Run targets
#

# Run the review loop (checks dependencies first)
run: check-deps
	cd $(SRCDIR) && $(PYTHON) reviewer.py --config $(CONFIG)

# Run with verbose logging
run-verbose:
	cd $(SRCDIR) && $(PYTHON) reviewer.py --config $(CONFIG) -v

#
# Cleanup targets
#

# Clean logs and Python cache
clean:
	rm -rf $(SRCDIR)/../.angry-ai/logs/*.txt
	rm -rf $(SRCDIR)/__pycache__
	rm -rf $(SRCDIR)/*.pyc

# Deep clean - also remove any leftover model weights (they belong on Ollama server)
clean-all: clean
	@if [ -d "$(SRCDIR)/Qwen2.5-Coder-32B-Instruct" ]; then \
		echo "Removing local model weights (these should be on Ollama server)..."; \
		rm -rf $(SRCDIR)/Qwen2.5-Coder-32B-Instruct; \
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
	@echo "  make check-deps   Check/install Python3, pip, and PyYAML (auto-runs on 'make run')"
	@echo "  vim config.yaml   Configure Ollama server URL and model"
	@echo "  make validate     Test connection to Ollama server"
	@echo ""
	@echo "Usage:"
	@echo "  make run          Start the review loop (auto-checks dependencies)"
	@echo "  make run-verbose  Run with verbose logging"
	@echo "  make test         Run component self-tests"
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
