#!/usr/bin/env bash
#
# Bootstrap Script for Distributed AI Code Review
#
# This script is idempotent and can be run by multiple workers simultaneously.
# It performs:
#   1. Dependency checking (Python, PyYAML, Ollama connection)
#   2. bd (beads) initialization if needed
#   3. Task generation for unclaimed directories
#
# Usage:
#   ./bootstrap.sh [--source-root PATH] [--config config.yaml]

set -e

# Default values
CONFIG_FILE="config.yaml"
MAX_DEPTH=3
WORKER_ID="${HOSTNAME:-worker-$$}"
SOURCE_ROOT=""  # Will be read from config.yaml

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --config)
            CONFIG_FILE="$2"
            shift 2
            ;;
        --max-depth)
            MAX_DEPTH="$2"
            shift 2
            ;;
        --worker-id)
            WORKER_ID="$2"
            shift 2
            ;;
        --help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --config FILE         Configuration file (default: config.yaml)"
            echo "  --max-depth N         Maximum directory depth to scan (default: 3)"
            echo "  --worker-id ID        Worker identifier (default: hostname or PID)"
            echo "  --help                Show this help message"
            echo ""
            echo "Note: source_root is read from config.yaml"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Read source_root from config.yaml
if [ ! -f "$CONFIG_FILE" ]; then
    echo "ERROR: Config file not found: $CONFIG_FILE"
    exit 1
fi

SOURCE_ROOT=$(python3 -c "
import sys
try:
    import yaml
    with open('$CONFIG_FILE', 'r') as f:
        config = yaml.safe_load(f)
        print(config.get('source', {}).get('root', ''))
except Exception as e:
    print('', file=sys.stderr)
    sys.exit(1)
" 2>/dev/null)

if [ -z "$SOURCE_ROOT" ]; then
    echo "ERROR: source.root not found in $CONFIG_FILE"
    exit 1
fi

# Convert to absolute path
SOURCE_ROOT=$(cd "$SOURCE_ROOT" && pwd)
if [ ! -d "$SOURCE_ROOT" ]; then
    echo "ERROR: Source root directory does not exist: $SOURCE_ROOT"
    exit 1
fi

echo "=========================================="
echo "AI Code Review - Bootstrap Phase"
echo "=========================================="
echo "Worker ID: $WORKER_ID"
echo "Config File: $CONFIG_FILE"
echo "Source Root: $SOURCE_ROOT"
echo "Max Depth: $MAX_DEPTH"
echo ""
echo "IMPORTANT: .beads/ database will be created in:"
echo "  $SOURCE_ROOT/.beads/"
echo ""

# Phase 1: Check dependencies
echo "[1/5] Checking system dependencies..."
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found. Run 'make check-deps' first."
    exit 1
fi

if ! python3 -c "import yaml" >/dev/null 2>&1; then
    echo "ERROR: PyYAML not found. Run 'make check-deps' first."
    exit 1
fi

echo "✓ Python3 and PyYAML found"

# Phase 2: Validate Ollama connection
echo ""
echo "[2/5] Validating Ollama connection..."
if ! python3 reviewer.py --config "$CONFIG_FILE" --validate-only 2>/dev/null; then
    echo "ERROR: Ollama validation failed. Check your config.yaml"
    exit 1
fi
echo "✓ Ollama connection validated"

# Phase 3: Initialize bd (beads) in SOURCE_ROOT (not current directory!)
echo ""
echo "[3/5] Checking bd (beads) setup in source repository..."
if ! command -v bd >/dev/null 2>&1; then
    echo "ERROR: bd command not found."
    echo "Install from: https://github.com/steveyegge/beads"
    exit 1
fi

# Change to source root for all bd operations
cd "$SOURCE_ROOT"

# Check if bd database exists, if not initialize
if [ ! -d .beads ]; then
    echo "Initializing bd database in $SOURCE_ROOT/.beads/..."
    bd init
    echo "✓ bd database created in source repository"
elif ! bd list --json >/dev/null 2>&1; then
    echo "Found .beads/ but no database, initializing..."
    bd init
    echo "✓ bd database initialized"
else
    echo "✓ bd database found in source repository"
fi

# Phase 4: Sync with remote to get latest tasks (in SOURCE_ROOT)
echo ""
echo "[4/5] Syncing source repository with remote..."
cd "$SOURCE_ROOT"

git fetch origin 2>/dev/null || echo "Warning: Could not fetch from remote"

# Pull latest changes (including .beads/issues.jsonl)
if git pull --rebase 2>/dev/null; then
    echo "✓ Synced with remote"
    # Re-import JSONL if it changed
    if [ -f .beads/issues.jsonl ]; then
        bd sync --json >/dev/null 2>&1 || true
    fi
else
    echo "Warning: Could not pull from remote (may need to resolve conflicts)"
fi

# Phase 5: Generate tasks for unclaimed directories
echo ""
echo "[5/5] Discovering directories and creating tasks..."

cd "$SOURCE_ROOT"

# Get existing tasks
EXISTING_TASKS=$(bd list --json 2>/dev/null || echo "[]")

# Find all directories in source tree (relative to SOURCE_ROOT)
echo "Scanning source tree at: $SOURCE_ROOT"
DIRS=$(find . -type d -maxdepth "$MAX_DEPTH" 2>/dev/null | \
       grep -v -E '^\./\.(git|beads)' | \
       sed 's|^\./||' | \
       sort)

TOTAL_DIRS=$(echo "$DIRS" | wc -l | tr -d ' ')
NEW_TASKS=0
EXISTING_COUNT=0

echo "Found $TOTAL_DIRS directories to process"

for DIR in $DIRS; do
    # Skip if empty (current directory)
    if [ -z "$DIR" ] || [ "$DIR" = "." ]; then
        continue
    fi
    
    # Check if task already exists for this directory
    TASK_EXISTS=$(echo "$EXISTING_TASKS" | \
                  python3 -c "import sys, json; tasks=json.load(sys.stdin); print('yes' if any('$DIR' in t.get('title','') or '$DIR' in t.get('description','') for t in tasks) else 'no')" 2>/dev/null || echo "no")
    
    if [ "$TASK_EXISTS" = "yes" ]; then
        EXISTING_COUNT=$((EXISTING_COUNT + 1))
        continue
    fi
    
    # Create new task with relative path
    TASK_TITLE="Review directory: $DIR"
    TASK_DESC="AI code review of all files in $DIR directory (relative to source root: $SOURCE_ROOT)"
    
    echo "Creating task: $TASK_TITLE"
    bd create "$TASK_TITLE" \
       --description="$TASK_DESC" \
       --type=task \
       --priority=2 \
       --json >/dev/null 2>&1 && NEW_TASKS=$((NEW_TASKS + 1)) || echo "  Warning: Failed to create task"
done

echo ""
echo "Task creation summary:"
echo "  - Total directories found: $TOTAL_DIRS"
echo "  - Existing tasks: $EXISTING_COUNT"
echo "  - New tasks created: $NEW_TASKS"

# Phase 6: Sync tasks to remote (in SOURCE_ROOT)
echo ""
echo "[6/6] Syncing tasks to source repository remote..."

cd "$SOURCE_ROOT"

# bd automatically exports to JSONL, just need to commit and push
if [ "$NEW_TASKS" -gt 0 ]; then
    sleep 6  # Wait for bd auto-export (5s debounce + 1s buffer)
    
    if [ -f .beads/issues.jsonl ]; then
        git add .beads/issues.jsonl
        git commit -m "Bootstrap: Added $NEW_TASKS new review tasks (worker: $WORKER_ID)" 2>/dev/null || true
        
        # Try to push, retry if fails
        for i in {1..5}; do
            if git push 2>/dev/null; then
                echo "✓ Tasks synced to remote"
                break
            else
                echo "Push failed, pulling and retrying..."
                git pull --rebase 2>/dev/null || true
                sleep $((i * 2))
            fi
        done
    fi
fi

# Summary
echo ""
echo "=========================================="
echo "Bootstrap Complete!"
echo "=========================================="
echo ""
echo "Beads database location:"
echo "  $SOURCE_ROOT/.beads/"
echo ""
echo "Ready to start worker node:"
echo "  ./worker-node.sh --worker-id $WORKER_ID"
echo ""

# Show available work
cd "$SOURCE_ROOT"
READY_COUNT=$(bd ready --json 2>/dev/null | python3 -c "import sys, json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")
echo "Available tasks: $READY_COUNT"

if [ "$READY_COUNT" -gt 0 ]; then
    echo ""
    echo "Next available tasks:"
    bd ready --json 2>/dev/null | python3 -c "
import sys, json
tasks = json.load(sys.stdin)
for i, task in enumerate(tasks[:5]):
    print(f\"  {i+1}. [{task['id']}] {task['title']}\")
" 2>/dev/null || echo "  (use 'cd $SOURCE_ROOT && bd ready' to see tasks)"
fi

