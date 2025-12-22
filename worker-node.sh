#!/usr/bin/env bash
#
# Worker Node Script for Distributed AI Code Review
#
# This script runs continuously, claiming tasks from the bd queue,
# processing them, and updating status.
#
# Usage:
#   ./worker-node.sh [--worker-id ID] [--config config.yaml] [--max-tasks N]

set -e

# Add common bd installation paths to PATH
export PATH="$HOME/gocode/bin:$HOME/go/bin:$PATH"

# Default values
WORKER_ID="${HOSTNAME:-worker-$$}"
CONFIG_FILE="config.yaml"
MAX_TASKS=0  # 0 = unlimited
TASKS_COMPLETED=0
TASKS_FAILED=0
START_TIME=$(date +%s)
SOURCE_ROOT=""  # Will be read from config.yaml
REVIEWER_DIR=$(pwd)  # Remember where ai-code-reviewer is

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --worker-id)
            WORKER_ID="$2"
            shift 2
            ;;
        --config)
            CONFIG_FILE="$2"
            shift 2
            ;;
        --max-tasks)
            MAX_TASKS="$2"
            shift 2
            ;;
        --help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --worker-id ID        Worker identifier (default: hostname or PID)"
            echo "  --config FILE         Configuration file (default: config.yaml)"
            echo "  --max-tasks N         Maximum tasks to process (default: 0 = unlimited)"
            echo "  --help                Show this help message"
            echo ""
            echo "Environment Variables:"
            echo "  WORKER_ID             Alternative way to set worker ID"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Allow environment variable override
WORKER_ID="${WORKER_ID:-${HOSTNAME:-worker-$$}}"

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

# Read source_root from config.yaml

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
cd "$REVIEWER_DIR"
SOURCE_ROOT=$(cd "$SOURCE_ROOT" && pwd)
if [ ! -d "$SOURCE_ROOT" ]; then
    echo "ERROR: Source root directory does not exist: $SOURCE_ROOT"
    exit 1
fi

echo "=========================================="
echo "AI Code Review - Worker Node"
echo "=========================================="
echo "Worker ID: $WORKER_ID"
echo "Reviewer Dir: $REVIEWER_DIR"
echo "Source Root: $SOURCE_ROOT"
echo "Config: $CONFIG_FILE"
echo "Max Tasks: $([ $MAX_TASKS -eq 0 ] && echo 'unlimited' || echo $MAX_TASKS)"
echo "Started: $(date)"
echo ""
echo "IMPORTANT: Working in source repository:"
echo "  $SOURCE_ROOT/.beads/"
echo ""

# Trap for clean shutdown
trap cleanup EXIT INT TERM

cleanup() {
    local END_TIME=$(date +%s)
    local ELAPSED=$((END_TIME - START_TIME))
    local HOURS=$((ELAPSED / 3600))
    local MINUTES=$(((ELAPSED % 3600) / 60))
    local SECONDS=$((ELAPSED % 60))
    
    echo ""
    echo "=========================================="
    echo "Worker Node Shutdown"
    echo "=========================================="
    echo "Worker ID: $WORKER_ID"
    echo "Tasks Completed: $TASKS_COMPLETED"
    echo "Tasks Failed: $TASKS_FAILED"
    echo "Runtime: ${HOURS}h ${MINUTES}m ${SECONDS}s"
    echo "Ended: $(date)"
    echo ""
}

# Worker loop
TASK_COUNT=0

while true; do
    # Check if we've hit max tasks
    if [ $MAX_TASKS -gt 0 ] && [ $TASK_COUNT -ge $MAX_TASKS ]; then
        echo "Reached maximum task limit ($MAX_TASKS), exiting."
        break
    fi
    
    # Sync source repository with remote before claiming work (CRITICAL)
    echo "[$(date '+%H:%M:%S')] Syncing source repository with remote..."
    cd "$SOURCE_ROOT"
    
    # Fetch first to see what's changed
    git fetch origin 2>/dev/null || echo "  Warning: Could not fetch from remote"
    
    # Pull and check if task queue changed
    PULL_OUTPUT=$(git pull --rebase 2>&1)
    if echo "$PULL_OUTPUT" | grep -q 'Already up to date'; then
        echo "  Already up to date"
    elif echo "$PULL_OUTPUT" | grep -q 'Fast-forward\|Applying'; then
        echo "  ✓ Updated from remote"
        # Check if .beads/issues.jsonl changed
        if echo "$PULL_OUTPUT" | grep -q '.beads/issues.jsonl'; then
            echo "  ✓ Task queue updated - re-importing..."
            bd sync --json >/dev/null 2>&1 || true
        fi
    else
        echo "  Warning: Pull had issues (may need manual intervention)"
        echo "  Output: $PULL_OUTPUT"
    fi
    
    # Get next available task (from SOURCE_ROOT/.beads/)
    echo "[$(date '+%H:%M:%S')] Checking for available work..."
    cd "$SOURCE_ROOT"
    TASK_JSON=$(bd ready --json 2>/dev/null | python3 -c "
import sys, json
tasks = json.load(sys.stdin)
if tasks:
    print(json.dumps(tasks[0]))
else:
    print('null')
" 2>/dev/null)
    
    if [ "$TASK_JSON" = "null" ] || [ -z "$TASK_JSON" ]; then
        echo "No tasks available. Waiting 30 seconds..."
        sleep 30
        
        # Check one more time before giving up
        TASK_JSON=$(bd ready --json 2>/dev/null | python3 -c "
import sys, json
tasks = json.load(sys.stdin)
if tasks:
    print(json.dumps(tasks[0]))
else:
    print('null')
" 2>/dev/null)
        
        if [ "$TASK_JSON" = "null" ] || [ -z "$TASK_JSON" ]; then
            echo "Still no work available. Exiting."
            break
        fi
    fi
    
    # Parse task details
    TASK_ID=$(echo "$TASK_JSON" | python3 -c "import sys, json; print(json.load(sys.stdin)['id'])" 2>/dev/null)
    TASK_TITLE=$(echo "$TASK_JSON" | python3 -c "import sys, json; print(json.load(sys.stdin)['title'])" 2>/dev/null)
    
    # Extract directory from title (format: "Review directory: path/to/dir")
    TARGET_DIR=$(echo "$TASK_TITLE" | sed 's/^Review directory: //')
    
    if [ -z "$TASK_ID" ] || [ -z "$TARGET_DIR" ]; then
        echo "ERROR: Could not parse task JSON"
        continue
    fi
    
    echo ""
    echo "=========================================="
    echo "Processing Task: $TASK_ID"
    echo "Directory: $TARGET_DIR"
    echo "=========================================="
    
    # Claim the task (in SOURCE_ROOT)
    echo "[$(date '+%H:%M:%S')] Claiming task..."
    cd "$SOURCE_ROOT"
    if ! bd update "$TASK_ID" --status in_progress --json >/dev/null 2>&1; then
        echo "ERROR: Failed to claim task (may have been claimed by another worker)"
        continue
    fi
    
    # Sync claim to remote (in SOURCE_ROOT)
    sleep 6  # Wait for bd auto-export
    if [ -f .beads/issues.jsonl ]; then
        git add .beads/issues.jsonl
        git commit -m "Worker $WORKER_ID: Claimed task $TASK_ID" 2>/dev/null || true
        
        # Push with retry
        for i in {1..3}; do
            if git push 2>/dev/null; then
                echo "✓ Task claim synced to remote"
                break
            else
                git pull --rebase 2>/dev/null || true
                sleep 2
            fi
        done
    fi
    
    echo "✓ Task claimed"
    
    # Run the review (from REVIEWER_DIR, targeting SOURCE_ROOT)
    echo ""
    echo "[$(date '+%H:%M:%S')] Starting review of $TARGET_DIR..."
    
    REVIEW_START=$(date +%s)
    
    cd "$REVIEWER_DIR"
    if python3 reviewer.py --config "$CONFIG_FILE" --directory "$TARGET_DIR" --task-id "$TASK_ID"; then
        REVIEW_END=$(date +%s)
        REVIEW_TIME=$((REVIEW_END - REVIEW_START))
        
        echo ""
        echo "✓ Review completed successfully in ${REVIEW_TIME}s"
        
        # Mark task as completed (in SOURCE_ROOT)
        cd "$SOURCE_ROOT"
        bd close "$TASK_ID" --reason "Review completed successfully by worker $WORKER_ID (${REVIEW_TIME}s)" 2>/dev/null || true
        TASKS_COMPLETED=$((TASKS_COMPLETED + 1))
        
    else
        REVIEW_END=$(date +%s)
        REVIEW_TIME=$((REVIEW_END - REVIEW_START))
        
        echo ""
        echo "✗ Review failed after ${REVIEW_TIME}s"
        
        # Mark task as failed (in SOURCE_ROOT)
        cd "$SOURCE_ROOT"
        bd update "$TASK_ID" --status failed --json 2>/dev/null || true
        # Add comment with failure details
        bd comment "$TASK_ID" "Review failed on worker $WORKER_ID after ${REVIEW_TIME}s. Check logs for details." 2>/dev/null || true
        TASKS_FAILED=$((TASKS_FAILED + 1))
    fi
    
    # Sync result to remote (in SOURCE_ROOT)
    cd "$SOURCE_ROOT"
    sleep 6  # Wait for bd auto-export
    if [ -f .beads/issues.jsonl ]; then
        git add .beads/issues.jsonl
        
        # Also add any changes made during review
        git add -A 2>/dev/null || true
        
        git commit -m "Worker $WORKER_ID: Completed task $TASK_ID ($([ $? -eq 0 ] && echo 'success' || echo 'failed'))" 2>/dev/null || true
        
        # Push with retry
        for i in {1..5}; do
            if git push 2>/dev/null; then
                echo "✓ Results synced to remote"
                break
            else
                echo "Push failed, pulling and retrying..."
                git pull --rebase 2>/dev/null || true
                sleep $((i * 2))
            fi
        done
    fi
    
    TASK_COUNT=$((TASK_COUNT + 1))
    
    # Brief pause before next task
    echo ""
    echo "Waiting 5 seconds before next task..."
    sleep 5
done

echo ""
echo "Worker node finished."

