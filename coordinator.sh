#!/usr/bin/env bash
#
# Coordinator Script for Distributed AI Code Review
#
# Monitors progress across all worker nodes and provides status reports.
#
# Usage:
#   ./coordinator.sh [--watch] [--interval SECONDS]

set -e

WATCH_MODE=false
INTERVAL=10

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --watch)
            WATCH_MODE=true
            shift
            ;;
        --interval)
            INTERVAL="$2"
            shift 2
            ;;
        --help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --watch               Continuous monitoring mode (refresh every N seconds)"
            echo "  --interval SECONDS    Update interval for watch mode (default: 10)"
            echo "  --help                Show this help message"
            echo ""
            echo "Commands:"
            echo "  Show status:          ./coordinator.sh"
            echo "  Continuous monitor:   ./coordinator.sh --watch"
            echo "  Fast updates:         ./coordinator.sh --watch --interval 5"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

show_status() {
    clear
    echo "=========================================="
    echo "AI Code Review - Distributed Status"
    echo "=========================================="
    echo "Updated: $(date)"
    echo ""
    
    # Check if bd is available
    if ! command -v bd >/dev/null 2>&1; then
        echo "ERROR: bd command not found"
        return 1
    fi
    
    # Get all tasks
    ALL_TASKS=$(bd list --json 2>/dev/null || echo "[]")
    
    if [ "$ALL_TASKS" = "[]" ]; then
        echo "No tasks found. Run ./bootstrap.sh to create tasks."
        return 0
    fi
    
    # Count by status
    TOTAL=$(echo "$ALL_TASKS" | python3 -c "import sys, json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")
    PENDING=$(echo "$ALL_TASKS" | python3 -c "import sys, json; tasks=json.load(sys.stdin); print(len([t for t in tasks if t.get('status') == 'pending']))" 2>/dev/null || echo "0")
    IN_PROGRESS=$(echo "$ALL_TASKS" | python3 -c "import sys, json; tasks=json.load(sys.stdin); print(len([t for t in tasks if t.get('status') == 'in_progress']))" 2>/dev/null || echo "0")
    COMPLETED=$(echo "$ALL_TASKS" | python3 -c "import sys, json; tasks=json.load(sys.stdin); print(len([t for t in tasks if t.get('status') == 'completed']))" 2>/dev/null || echo "0")
    FAILED=$(echo "$ALL_TASKS" | python3 -c "import sys, json; tasks=json.load(sys.stdin); print(len([t for t in tasks if t.get('status') == 'failed']))" 2>/dev/null || echo "0")
    
    # Calculate progress percentage
    if [ "$TOTAL" -gt 0 ]; then
        PROGRESS=$((COMPLETED * 100 / TOTAL))
    else
        PROGRESS=0
    fi
    
    echo "Overall Progress:"
    echo "  Total Tasks:      $TOTAL"
    echo "  Completed:        $COMPLETED ($PROGRESS%)"
    echo "  In Progress:      $IN_PROGRESS"
    echo "  Pending:          $PENDING"
    echo "  Failed:           $FAILED"
    echo ""
    
    # Progress bar
    BAR_WIDTH=50
    FILLED=$((PROGRESS * BAR_WIDTH / 100))
    EMPTY=$((BAR_WIDTH - FILLED))
    printf "  ["
    printf "%${FILLED}s" | tr ' ' '='
    printf "%${EMPTY}s" | tr ' ' '-'
    printf "] %d%%\n" "$PROGRESS"
    echo ""
    
    # Show tasks in progress
    if [ "$IN_PROGRESS" -gt 0 ]; then
        echo "Tasks In Progress:"
        echo "$ALL_TASKS" | python3 -c "
import sys, json
tasks = json.load(sys.stdin)
in_progress = [t for t in tasks if t.get('status') == 'in_progress']
for task in in_progress[:10]:
    title = task.get('title', 'Unknown')
    task_id = task.get('id', '?')
    print(f\"  [{task_id}] {title}\")
if len(in_progress) > 10:
    print(f\"  ... and {len(in_progress) - 10} more\")
" 2>/dev/null
        echo ""
    fi
    
    # Show next pending tasks
    if [ "$PENDING" -gt 0 ]; then
        echo "Next Pending Tasks:"
        echo "$ALL_TASKS" | python3 -c "
import sys, json
tasks = json.load(sys.stdin)
pending = [t for t in tasks if t.get('status') == 'pending']
for task in pending[:5]:
    title = task.get('title', 'Unknown')
    task_id = task.get('id', '?')
    priority = task.get('priority', 2)
    print(f\"  [{task_id}] (P{priority}) {title}\")
if len(pending) > 5:
    print(f\"  ... and {len(pending) - 5} more\")
" 2>/dev/null
        echo ""
    fi
    
    # Show failed tasks
    if [ "$FAILED" -gt 0 ]; then
        echo "Failed Tasks (need attention):"
        echo "$ALL_TASKS" | python3 -c "
import sys, json
tasks = json.load(sys.stdin)
failed = [t for t in tasks if t.get('status') == 'failed']
for task in failed:
    title = task.get('title', 'Unknown')
    task_id = task.get('id', '?')
    print(f\"  [{task_id}] {title}\")
    # Show last comment if available
    comments = task.get('comments', [])
    if comments:
        last_comment = comments[-1].get('text', '')
        if last_comment:
            print(f\"    └─ {last_comment[:60]}...\")
" 2>/dev/null
        echo ""
    fi
    
    # Estimate completion time
    if [ "$IN_PROGRESS" -gt 0 ] && [ "$PENDING" -gt 0 ]; then
        # Rough estimate: assume each task takes 5 minutes
        AVG_TIME_PER_TASK=5
        REMAINING_TIME=$((PENDING * AVG_TIME_PER_TASK / IN_PROGRESS))
        HOURS=$((REMAINING_TIME / 60))
        MINUTES=$((REMAINING_TIME % 60))
        
        echo "Estimated Time to Completion:"
        if [ "$HOURS" -gt 0 ]; then
            echo "  ~${HOURS}h ${MINUTES}m (assuming $IN_PROGRESS active workers)"
        else
            echo "  ~${MINUTES}m (assuming $IN_PROGRESS active workers)"
        fi
        echo "  (rough estimate: ${AVG_TIME_PER_TASK}min per task)"
        echo ""
    fi
    
    # Show worker activity (from git log)
    echo "Recent Worker Activity:"
    git log --oneline --grep="Worker" -10 2>/dev/null | head -5 || echo "  No recent activity"
    echo ""
    
    echo "=========================================="
    echo "Commands:"
    echo "  View task:        bd show <task-id>"
    echo "  Reset failed:     bd update <task-id> --status pending"
    echo "  Start worker:     ./worker-node.sh --worker-id \$HOSTNAME"
    echo "  Watch progress:   ./coordinator.sh --watch"
    echo "=========================================="
}

if [ "$WATCH_MODE" = true ]; then
    echo "Starting watch mode (Ctrl+C to exit)..."
    echo "Update interval: ${INTERVAL}s"
    sleep 2
    
    while true; do
        show_status
        sleep "$INTERVAL"
    done
else
    show_status
fi

