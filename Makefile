# Angry AI - Universal Code Reviewer
#
# AI-powered code reviewer for ANY codebase (C, C++, Rust, Go, Python, etc.)
# Validates changes with YOUR build command (configurable in config.yaml).
# Cross-platform: FreeBSD, macOS, Linux
#
# Quick start:
#   make check-deps  # Auto-detect OS and install dependencies
#   vim config.yaml  # Set your Ollama server URL
#   make validate    # Test connection to Ollama
#   make run         # Start the review loop

# Python interpreter
PYTHON?=	python3

# Source directory (bmake sets .CURDIR to Makefile location, not obj dir)
SRCDIR=		${.CURDIR}

# Configuration file (relative to source directory)
CONFIG?=	${SRCDIR}/config.yaml

# Phony targets
.PHONY: all deps check-deps validate run run-verbose test clean clean-all help
.PHONY: bootstrap worker coordinator status distributed-help deduplicate

# Default target
all: help

#
# Setup targets
#

# Check and install system dependencies (Python3, pip, pyyaml)
check-deps:
	@echo "Checking dependencies..."
	@# Detect OS
	@OS=$$(uname -s); \
	echo "Detected OS: $$OS"; \
	\
	if ! command -v python3 >/dev/null 2>&1; then \
		echo "Python3 not found. Installing..."; \
		case "$$OS" in \
			FreeBSD) \
				sudo pkg install -y python3 ;; \
			Darwin) \
				if command -v brew >/dev/null 2>&1; then \
					brew install python3; \
				else \
					echo "ERROR: Homebrew not found. Install from https://brew.sh"; \
					exit 1; \
				fi ;; \
			Linux) \
				if command -v apt-get >/dev/null 2>&1; then \
					sudo apt-get update && sudo apt-get install -y python3; \
				elif command -v yum >/dev/null 2>&1; then \
					sudo yum install -y python3; \
				elif command -v dnf >/dev/null 2>&1; then \
					sudo dnf install -y python3; \
				else \
					echo "ERROR: No supported package manager found (apt-get, yum, dnf)"; \
					exit 1; \
				fi ;; \
			*) \
				echo "ERROR: Unsupported OS: $$OS"; \
				exit 1 ;; \
		esac \
	else \
		echo "✓ Python3 found: $$(python3 --version)"; \
	fi
	@# Check for pip
	@OS=$$(uname -s); \
	if ! python3 -m pip --version >/dev/null 2>&1; then \
		echo "pip not found. Installing..."; \
		case "$$OS" in \
			FreeBSD) \
				sudo pkg install -y py311-pip ;; \
			Darwin) \
				python3 -m ensurepip --upgrade || curl https://bootstrap.pypa.io/get-pip.py | python3 ;; \
			Linux) \
				if command -v apt-get >/dev/null 2>&1; then \
					sudo apt-get install -y python3-pip; \
				elif command -v yum >/dev/null 2>&1; then \
					sudo yum install -y python3-pip; \
				elif command -v dnf >/dev/null 2>&1; then \
					sudo dnf install -y python3-pip; \
				else \
					python3 -m ensurepip --upgrade || curl https://bootstrap.pypa.io/get-pip.py | python3; \
				fi ;; \
		esac \
	else \
		echo "✓ pip found: $$(python3 -m pip --version)"; \
	fi
	@# Check for pyyaml
	@if ! python3 -c "import yaml" >/dev/null 2>&1; then \
		echo "PyYAML not found. Installing..."; \
		OS=$$(uname -s); \
		case "$$OS" in \
			Darwin) \
				if python3 -m pip install --user pyyaml 2>/dev/null; then \
					echo "✓ PyYAML installed via pip --user"; \
				elif python3 -m pip install --break-system-packages pyyaml 2>/dev/null; then \
					echo "✓ PyYAML installed via pip --break-system-packages"; \
				else \
					echo "WARNING: pip install failed. Trying Homebrew..."; \
					if command -v brew >/dev/null 2>&1; then \
						brew install libyaml && python3 -m pip install --break-system-packages pyyaml; \
					else \
						echo "ERROR: Could not install PyYAML"; \
						exit 1; \
					fi; \
				fi ;; \
			*) \
				python3 -m pip install --user pyyaml ;; \
		esac \
	else \
		echo "✓ PyYAML found"; \
	fi
	@echo "All dependencies satisfied!"

# Install Python dependencies (just PyYAML - no torch/GPU stuff)
# Legacy target - use check-deps instead
deps:
	@echo "Installing Python dependencies..."
	${PYTHON} -m pip install --user -r ${SRCDIR}/requirements.txt
	@echo "Done. Dependencies installed."

#
# Validation targets
#

# Validate Ollama connection and model availability
validate: check-deps
	@echo "Validating Ollama connection..."
	cd ${SRCDIR} && ${PYTHON} reviewer.py --config ${CONFIG} --validate-only

# Run component self-tests
test:
	@echo "=== Testing Ollama Client ==="
	cd ${SRCDIR} && ${PYTHON} ollama_client.py
	@echo ""
	@echo "=== Testing Build Executor ==="
	cd ${SRCDIR} && ${PYTHON} build_executor.py
	@echo ""
	@echo "All component tests passed!"

#
# Run targets
#

# Run the review loop (bootstrap runs first to ensure tasks exist)
# Note: In single-node mode, bootstrap creates tasks but they're optional
# In distributed mode, bootstrap is required
run: bootstrap
	cd ${SRCDIR} && ${PYTHON} reviewer.py --config ${CONFIG}

# Run with verbose logging
run-verbose:
	cd ${SRCDIR} && ${PYTHON} reviewer.py --config ${CONFIG} -v

#
# Distributed mode targets
#

# Bootstrap: Initialize bd and create tasks (idempotent, safe to run multiple times)
bootstrap: check-deps
	@echo "Running bootstrap phase..."
	cd ${SRCDIR} && ./bootstrap.sh --config ${CONFIG}

# Start a worker node (bootstrap runs first to ensure tasks exist)
worker:
	@echo "Starting worker node..."
	@echo "NOTE: Assumes tasks already created by 'make bootstrap'"
	cd ${SRCDIR} && ./worker-node.sh --config ${CONFIG}

# Show coordinator status
coordinator:
	cd ${SRCDIR} && ./coordinator.sh

# Watch status continuously
status:
	cd ${SRCDIR} && ./coordinator.sh --watch

# Deduplicate tasks (cleanup from older versions)
deduplicate:
	cd ${SRCDIR} && ./bootstrap.sh --deduplicate

# Help for distributed mode
distributed-help:
	@echo "Distributed AI Code Review"
	@echo "=========================="
	@echo ""
	@echo "Quick Start:"
	@echo "  make worker           Start a worker node (bootstrap runs automatically)"
	@echo "                        First worker creates tasks, others see existing tasks"
	@echo ""
	@echo "Monitoring:"
	@echo "  make coordinator      Show current status snapshot"
	@echo "  make status           Watch status continuously (updates every 10s)"
	@echo ""
	@echo "Maintenance:"
	@echo "  make deduplicate      Remove duplicate tasks (cleanup from older versions)"
	@echo ""
	@echo "Manual Control:"
	@echo "  bd ready              Show available tasks"
	@echo "  bd list               Show all tasks"
	@echo "  bd show <id>          Show task details"
	@echo "  bd update <id> --status pending   Reset failed task"
	@echo ""
	@echo "Architecture:"
	@echo "  - Each worker claims tasks from shared bd queue (.beads/issues.jsonl)"
	@echo "  - Tasks are synced via git (automatic with bd)"
	@echo "  - Workers run independently, no central coordination needed"
	@echo "  - Safe to run multiple workers on same or different machines"
	@echo ""
	@echo "Typical Workflow:"
	@echo "  1. Clone repo on multiple machines"
	@echo "  2. Configure each: 'make check-deps && vim config.yaml'"
	@echo "  3. Run 'make worker' on each machine (bootstrap runs automatically)"
	@echo "  4. Monitor with 'make status' from any machine"
	@echo ""

#
# Cleanup targets
#

# Clean logs and Python cache
clean:
	rm -rf ${SRCDIR}/../.angry-ai/logs/*.txt
	rm -rf ${SRCDIR}/__pycache__
	rm -rf ${SRCDIR}/*.pyc

# Deep clean - also remove any leftover model weights (they belong on Ollama server)
clean-all: clean
	@if [ -d "${SRCDIR}/Qwen2.5-Coder-32B-Instruct" ]; then \
		echo "Removing local model weights (these should be on Ollama server)..."; \
		rm -rf ${SRCDIR}/Qwen2.5-Coder-32B-Instruct; \
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
	@echo "Cross-platform: FreeBSD, macOS, Linux"
	@echo ""
	@echo "Setup:"
	@echo "  make check-deps   Auto-detect OS and install Python3, pip, PyYAML"
	@echo "                    (FreeBSD: pkg, macOS: brew, Linux: apt/yum/dnf)"
	@echo "  vim config.yaml   Configure Ollama server URL and model"
	@echo "  make validate     Test connection to Ollama server"
	@echo ""
	@echo "Single-Node Mode:"
	@echo "  make run          Start the review loop (auto-checks dependencies)"
	@echo "  make run-verbose  Run with verbose logging"
	@echo ""
	@echo "Distributed Mode (Multiple GPUs/Machines):"
	@echo "  make distributed-help   Show detailed distributed mode instructions"
	@echo "  make worker             Start a worker node (bootstrap runs automatically)"
	@echo "  make status             Monitor progress across all workers"
	@echo "  make bootstrap          Manually run bootstrap (optional, worker does this)"
	@echo "  make deduplicate        Remove duplicate tasks (cleanup utility)"
	@echo ""
	@echo "Testing:"
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
	@echo "  - bd (beads) for distributed mode (https://github.com/steveyegge/beads)"
	@echo ""
	@echo "Supported Platforms:"
	@echo "  - FreeBSD (pkg)"
	@echo "  - macOS (Homebrew)"
	@echo "  - Linux (apt-get, yum, dnf)"
	@echo ""
	@echo "Works with: C/C++ (make/cmake), Rust, Go, Python, Node.js, etc."
	@echo "Just configure your build command in config.yaml!"
