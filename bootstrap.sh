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
FORCE_DEDUPE=false  # Force deduplication even if no duplicates found

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
        --deduplicate|--dedup)
            FORCE_DEDUPE=true
            shift
            ;;
        --help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --config FILE         Configuration file (default: config.yaml)"
            echo "  --max-depth N         Maximum directory depth to scan (default: 3)"
            echo "  --worker-id ID        Worker identifier (default: hostname or PID)"
            echo "  --deduplicate         Force deduplication check (automatic on startup)"
            echo "  --help                Show this help message"
            echo ""
            echo "Note: source_root is read from config.yaml"
            echo ""
            echo "Deduplication:"
            echo "  Bootstrap automatically checks for and merges duplicate tasks."
            echo "  This fixes issues from older versions that may have created duplicates."
            echo "  Use --deduplicate to force deduplication without creating new tasks."
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

# Early validation: config.yaml must exist
if [ ! -f "$CONFIG_FILE" ]; then
    echo "=========================================="
    echo "ERROR: Configuration File Not Found"
    echo "=========================================="
    echo ""
    echo "Config file does not exist: $CONFIG_FILE"
    echo ""
    echo "Please create it:"
    echo "  cp config.yaml.defaults config.yaml"
    echo "  vim config.yaml"
    echo ""
    echo "Required settings:"
    echo "  - ollama.url: Your Ollama server URL"
    echo "  - ollama.model: Model to use (e.g., qwen2.5-coder:32b)"
    echo "  - source.root: Path to code repository to review"
    echo "  - source.build_command: Your build command"
    echo ""
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

# Phase 4: Sync source repository with remote (CRITICAL for multi-worker coordination)
echo ""
echo "[4/5] Syncing source repository with remote..."
cd "$SOURCE_ROOT"

echo "  Fetching from remote..."
if ! git fetch origin 2>/dev/null; then
    echo "  Warning: Could not fetch from remote (continuing anyway)"
fi

echo "  Pulling latest changes (including .beads/issues.jsonl)..."
if git pull --rebase 2>&1; then
    if git diff --quiet HEAD@{1} HEAD -- .beads/issues.jsonl 2>/dev/null; then
        echo "✓ Already up to date"
    else
        echo "✓ Synced with remote (task queue updated)"
        # Re-import JSONL if it changed
        if [ -f .beads/issues.jsonl ]; then
            bd sync --json >/dev/null 2>&1 || true
        fi
    fi
else
    EXIT_CODE=$?
    echo "  Warning: Could not pull from remote (exit code: $EXIT_CODE)"
    echo "  This may indicate:"
    echo "    - No remote configured"
    echo "    - Merge conflicts"
    echo "    - Network issues"
    echo "  Continuing anyway, but coordination may be affected..."
fi

# Phase 5: Deduplicate any existing tasks (from older script versions)
echo ""
echo "[5/5] Checking for duplicate tasks..."

cd "$SOURCE_ROOT"

# One final sync before checking existing tasks (catch any just created by other workers)
echo "  Final sync check before deduplication..."
git pull --rebase --quiet 2>/dev/null || true
if [ -f .beads/issues.jsonl ]; then
    bd sync --json >/dev/null 2>&1 || true
fi

# Get all existing tasks
ALL_TASKS=$(bd list --json 2>/dev/null || echo "[]")

# Find and merge duplicates
DUPLICATES_FOUND=$(echo "$ALL_TASKS" | python3 -c "
import sys, json
from collections import defaultdict

tasks = json.load(sys.stdin)
if not tasks:
    print('0')
    sys.exit(0)

# Group by title
by_title = defaultdict(list)
for task in tasks:
    title = task.get('title', '')
    if title:
        by_title[title].append(task)

# Find titles with duplicates
duplicates = {title: task_list for title, task_list in by_title.items() if len(task_list) > 1}

if duplicates:
    print(f'{len(duplicates)}')
    for title, task_list in duplicates.items():
        # Sort by status priority: in_progress > pending > completed > failed
        status_priority = {'in_progress': 0, 'pending': 1, 'completed': 2, 'failed': 3}
        sorted_tasks = sorted(task_list, key=lambda t: status_priority.get(t.get('status', 'pending'), 99))
        
        keeper = sorted_tasks[0]
        dupes = sorted_tasks[1:]
        
        print(f'MERGE|{keeper[\"id\"]}|{keeper.get(\"status\", \"unknown\")}|{\"|\".join([d[\"id\"] for d in dupes])}', file=sys.stderr)
else:
    print('0')
" 2>&1)

DUPE_COUNT=$(echo "$DUPLICATES_FOUND" | head -1)
DUPE_DETAILS=$(echo "$DUPLICATES_FOUND" | grep '^MERGE|' || true)

if [ "$DUPE_COUNT" -gt 0 ] || [ "$FORCE_DEDUPE" = true ]; then
    if [ "$DUPE_COUNT" -gt 0 ]; then
        echo "  Found $DUPE_COUNT duplicate task titles - merging..."
    elif [ "$FORCE_DEDUPE" = true ]; then
        echo "  Force deduplication requested (no duplicates found)"
    fi
    
    if [ "$DUPE_COUNT" -gt 0 ]; then
        echo "$DUPE_DETAILS" | while IFS='|' read -r MERGE_CMD KEEPER_ID KEEPER_STATUS DUPE_IDS; do
        if [ "$MERGE_CMD" = "MERGE" ]; then
            echo "    Keeping: $KEEPER_ID (status: $KEEPER_STATUS)"
            
            # Close duplicate tasks
            IFS='|' read -ra DUPES <<< "$DUPE_IDS"
            for DUPE_ID in "${DUPES[@]}"; do
                if [ -n "$DUPE_ID" ]; then
                    echo "      Closing duplicate: $DUPE_ID"
                    bd close "$DUPE_ID" --reason "Duplicate task - merged into $KEEPER_ID" 2>/dev/null || \
                        bd update "$DUPE_ID" --status closed --json 2>/dev/null || \
                        echo "        Warning: Could not close $DUPE_ID"
                fi
            done
        fi
        done
        
        # Sync the cleaned-up task list
        echo "  Syncing deduplicated tasks to remote..."
        sleep 6  # Wait for bd auto-export
        if [ -f .beads/issues.jsonl ]; then
            git add .beads/issues.jsonl
            git commit -m "Bootstrap: Deduplicated $DUPE_COUNT duplicate tasks (worker: $WORKER_ID)" 2>/dev/null || true
            
            for i in {1..3}; do
                if git push 2>/dev/null; then
                    echo "  ✓ Deduplicated tasks synced to remote"
                    break
                else
                    git pull --rebase 2>/dev/null || true
                    sleep 2
                fi
            done
        fi
    fi
else
    echo "  ✓ No duplicate tasks found"
fi

# Phase 6: Generate tasks for unclaimed directories
echo ""
echo "[6/6] Discovering directories and creating tasks..."

cd "$SOURCE_ROOT"

# Refresh task list after deduplication
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
    
    # Create task title (exact format we'll check against)
    TASK_TITLE="Review directory: $DIR"
    
    # Check if task with this EXACT title already exists
    TASK_EXISTS=$(echo "$EXISTING_TASKS" | \
                  python3 -c "
import sys, json
tasks = json.load(sys.stdin)
target_title = '$TASK_TITLE'
# Check for exact title match (not substring)
exists = any(t.get('title', '') == target_title for t in tasks)
print('yes' if exists else 'no')
" 2>/dev/null || echo "no")
    
    if [ "$TASK_EXISTS" = "yes" ]; then
        EXISTING_COUNT=$((EXISTING_COUNT + 1))
        # Optionally show which statuses exist
        STATUS=$(echo "$EXISTING_TASKS" | \
                 python3 -c "
import sys, json
tasks = json.load(sys.stdin)
target = '$TASK_TITLE'
for t in tasks:
    if t.get('title', '') == target:
        print(t.get('status', 'unknown'))
        break
" 2>/dev/null || echo "unknown")
        # Only log if verbose or not completed
        if [ "$STATUS" != "completed" ]; then
            echo "  Task exists: $TASK_TITLE (status: $STATUS)"
        fi
        continue
    fi
    
    # Create new task with relative path
    TASK_DESC="AI code review of all files in $DIR directory (relative to source root: $SOURCE_ROOT)"
    
    echo "Creating task: $TASK_TITLE"
    if bd create "$TASK_TITLE" \
       --description="$TASK_DESC" \
       --type=task \
       --priority=2 \
       --json >/dev/null 2>&1; then
        NEW_TASKS=$((NEW_TASKS + 1))
    else
        echo "  ERROR: Failed to create task (may already exist)"
        # Double-check if it now exists (race condition with another worker)
        RECHECK=$(bd list --json 2>/dev/null | \
                  python3 -c "
import sys, json
tasks = json.load(sys.stdin)
target = '$TASK_TITLE'
exists = any(t.get('title', '') == target for t in tasks)
print('yes' if exists else 'no')
" 2>/dev/null || echo "no")
        if [ "$RECHECK" = "yes" ]; then
            echo "  (Task now exists - created by another worker)"
            EXISTING_COUNT=$((EXISTING_COUNT + 1))
        fi
    fi
done

echo ""
echo "Task creation summary:"
echo "  - Total directories found: $TOTAL_DIRS"
echo "  - Existing tasks: $EXISTING_COUNT"
echo "  - New tasks created: $NEW_TASKS"

# Phase 7: Sync tasks to remote (in SOURCE_ROOT)
echo ""
echo "[7/7] Syncing tasks to source repository remote..."

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

