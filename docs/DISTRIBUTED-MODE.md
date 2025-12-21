# Distributed Mode - Multi-GPU Parallel Code Review

The AI Code Reviewer supports **distributed mode**, allowing multiple worker nodes with GPUs to review code in parallel. This dramatically speeds up review of large codebases.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ Shared Git Repository                                       │
│ ├─ .beads/issues.jsonl  (task queue, git-tracked)          │
│ └─ source code                                              │
└─────────────────────────────────────────────────────────────┘
         ↑ git pull/push         ↑ git pull/push         ↑
         │                       │                       │
┌────────┴────────┐     ┌────────┴────────┐     ┌────────┴────────┐
│ Worker Node 1   │     │ Worker Node 2   │     │ Worker Node 3   │
│ ├─ Ollama+GPU   │     │ ├─ Ollama+GPU   │     │ ├─ Ollama+GPU   │
│ ├─ reviewer.py  │     │ ├─ reviewer.py  │     │ ├─ reviewer.py  │
│ └─ bd (beads)   │     │ └─ bd (beads)   │     │ └─ bd (beads)   │
└─────────────────┘     └─────────────────┘     └─────────────────┘
```

**Key Features:**
- **No central server** - Workers coordinate via git
- **Atomic task claiming** - bd prevents race conditions
- **Fault tolerant** - Workers can crash/restart without losing work
- **Scalable** - Add more workers anytime
- **Progress tracking** - Monitor from any node

## How It Works

### 1. Task Queue (bd/beads)

All review tasks are stored in `.beads/issues.jsonl` (git-tracked):

```json
{"id":"bd-1","title":"Review directory: src/bin/","status":"pending","priority":2}
{"id":"bd-2","title":"Review directory: src/lib/","status":"in_progress","priority":2}
{"id":"bd-3","title":"Review directory: src/net/","status":"completed","priority":2}
```

### 2. Worker Workflow

Each worker node:
1. **Syncs** with remote (`git pull`)
2. **Claims** next available task (`bd ready`, `bd update --status in_progress`)
3. **Pushes** claim to remote (`git push`)
4. **Reviews** the assigned directory (`reviewer.py --directory`)
5. **Updates** task status (`bd close` or `bd update --status failed`)
6. **Pushes** results (`git push`)
7. **Repeats** until no tasks remain

### 3. Conflict Resolution

bd handles conflicts automatically:
- Each worker has local SQLite database (`.beads/beads.db`, not tracked)
- JSONL file (`.beads/issues.jsonl`) is the source of truth
- bd auto-exports to JSONL every 5 seconds
- On `git pull`, bd auto-imports JSONL changes
- Git conflicts are rare (each task update is atomic)

## Setup

### Prerequisites

**On each worker node:**
- Python 3.8+ with PyYAML
- Ollama with a code model (e.g., qwen2.5-coder:32b)
- bd (beads) - Install from: https://github.com/steveyegge/beads
- Git access to shared repository

### Installation

```bash
# On each worker node
git clone <your-repo-url>
cd <repo>/ai-code-reviewer

# Install dependencies
make check-deps

# Configure Ollama server (each worker can use its own GPU)
vim config.yaml
# Set: ollama.url: "http://localhost:11434"

# Test connection
make validate
```

## Running Workers

### Start a Worker

On each worker node (bootstrap runs automatically first):

```bash
make worker
```

Or with custom worker ID:

```bash
./worker-node.sh --worker-id gpu-node-01
```

**Worker Output:**

First, bootstrap runs (idempotent):
```
==========================================
AI Code Review - Bootstrap Phase
==========================================
...
Task creation summary:
  - Total directories found: 127
  - Existing tasks: 127  ← Already exist, creates 0 new
  - New tasks created: 0
```

Then worker starts:
```
==========================================
AI Code Review - Worker Node
==========================================
Worker ID: gpu-node-01
Config: config.yaml
Max Tasks: unlimited
Started: Sun Dec 21 15:30:00 2025

[15:30:05] Syncing with remote...
[15:30:06] Checking for available work...

==========================================
Processing Task: bd-42
Directory: src/bin/cat
==========================================
[15:30:10] Claiming task...
✓ Task claimed
✓ Task claim synced to remote

[15:30:15] Starting review of src/bin/cat...
...
✓ Review completed successfully in 180s
✓ Results synced to remote

Waiting 5 seconds before next task...
```

### Worker Options

```bash
./worker-node.sh --help

Options:
  --worker-id ID        Worker identifier (default: hostname or PID)
  --config FILE         Configuration file (default: config.yaml)
  --max-tasks N         Maximum tasks to process (default: 0 = unlimited)
```

**Examples:**
```bash
# Process only 10 tasks then exit
./worker-node.sh --max-tasks 10

# Use custom config
./worker-node.sh --config config-gpu2.yaml

# Specific worker ID
./worker-node.sh --worker-id freebsd-gpu-01
```

## Monitoring

### One-Time Status

```bash
make coordinator
```

**Output:**
```
==========================================
AI Code Review - Distributed Status
==========================================
Updated: Sun Dec 21 15:45:00 2025

Overall Progress:
  Total Tasks:      127
  Completed:        45 (35%)
  In Progress:      8
  Pending:          72
  Failed:           2

  [=================----------------------------------] 35%

Tasks In Progress:
  [bd-23] Review directory: src/bin/ls
  [bd-45] Review directory: src/lib/libc
  ...

Next Pending Tasks:
  [bd-67] (P2) Review directory: src/net/tcp
  [bd-68] (P2) Review directory: src/fs/ufs
  ...

Estimated Time to Completion:
  ~7h 30m (assuming 8 active workers)
  (rough estimate: 5min per task)
```

### Continuous Monitoring

```bash
make status
```

This runs `coordinator.sh --watch`, updating every 10 seconds.

**Keyboard shortcuts:**
- `Ctrl+C` - Exit watch mode

### Manual bd Commands

```bash
# Show all tasks
bd list

# Show ready (available) tasks
bd ready

# Show specific task
bd show bd-42

# Show completed tasks
bd list --status completed

# Show failed tasks
bd list --status failed

# Reset a failed task
bd update bd-42 --status pending

# Show task history
bd show bd-42 --json | jq '.comments'
```

## Typical Workflows

### Scenario 1: Multiple GPUs on One Machine

```bash
# Terminal 1: Start first worker
cd /path/to/repo/ai-code-reviewer
make worker

# Terminal 2: Start second worker
cd /path/to/repo/ai-code-reviewer
./worker-node.sh --worker-id gpu-2

# Terminal 3: Monitor progress
cd /path/to/repo/ai-code-reviewer
make status
```

### Scenario 2: Multiple Machines

**Machine 1 (GPU server 1):**
```bash
git clone <repo-url>
cd ai-code-reviewer
make bootstrap  # Creates tasks
make worker
```

**Machine 2 (GPU server 2):**
```bash
git clone <repo-url>
cd ai-code-reviewer
make worker  # Automatically syncs tasks from Machine 1
```

**Machine 3 (Laptop, no GPU):**
```bash
git clone <repo-url>
cd ai-code-reviewer
make status  # Just monitor, don't run worker
```

### Scenario 3: Adding Workers Mid-Run

Workers can join anytime:

```bash
# On new machine
git clone <repo-url>
cd ai-code-reviewer
make check-deps
vim config.yaml  # Point to this machine's Ollama
make worker  # Joins existing work queue
```

### Scenario 4: Handling Failures

If a worker crashes:

```bash
# Check for stuck tasks (in_progress but worker died)
bd list --status in_progress

# Reset stuck task
bd update bd-42 --status pending

# Restart worker
make worker
```

## Performance

### Speedup Examples

| Codebase Size | Single Worker | 4 Workers | 8 Workers | Speedup |
|---------------|---------------|-----------|-----------|---------|
| 50 directories | 4 hours | 1 hour | 30 min | 8x |
| 200 directories | 16 hours | 4 hours | 2 hours | 8x |
| 500 directories | 40 hours | 10 hours | 5 hours | 8x |

**Assumptions:**
- ~5 minutes per directory
- Linear scaling (no bottlenecks)
- All workers have similar GPU performance

### Bottlenecks

**Git Push Conflicts:**
- Rare, but can happen with many workers
- Workers automatically retry with backoff
- Typically resolves in 1-2 retries

**Ollama Server:**
- Each worker needs access to Ollama
- Can use local Ollama on each machine
- Or shared Ollama server (if network is fast)

**Build System:**
- Each worker runs build command
- Ensure build system can handle parallel builds
- Consider using `ccache` or similar

## Troubleshooting

### Worker Can't Claim Tasks

**Symptom:** Worker says "No tasks available" but tasks exist

**Solution:**
```bash
# Sync bd database
bd sync

# Check task status
bd list --json | jq '.[] | {id, status}'

# Reset stuck tasks
bd list --status in_progress | while read task; do
    bd update $task --status pending
done
```

### Git Push Failures

**Symptom:** Worker can't push results

**Solution:**
```bash
# Pull latest changes
git pull --rebase

# Resolve any conflicts in .beads/issues.jsonl
# (bd usually handles this automatically)

# Push again
git push
```

### Worker Crashes

**Symptom:** Worker exits unexpectedly

**Solution:**
1. Check logs in persona directory: `personas/*/logs/`
2. Check Ollama server is running
3. Verify build command works: `make test`
4. Reset task if needed: `bd update <task-id> --status pending`
5. Restart worker: `make worker`

### Duplicate Work

**Symptom:** Two workers reviewing same directory

**Solution:**
- bd prevents this with atomic task claiming
- If it happens, check bd database: `bd list --status in_progress`
- Likely a git sync issue - ensure all workers can push/pull

## Advanced Configuration

### Priority-Based Review

Create high-priority tasks for critical directories:

```bash
# After bootstrap, update priorities
bd update bd-42 --priority 0  # Critical (security dirs)
bd update bd-43 --priority 1  # High (core functionality)
# Default is priority 2
```

Workers automatically pick highest priority tasks first.

### Custom Task Creation

Instead of using bootstrap.sh, create tasks manually:

```bash
# Review specific directories
bd create "Review directory: src/auth/" \
   --description="Security-critical authentication code" \
   --type=task \
   --priority=0

bd create "Review directory: src/crypto/" \
   --description="Cryptographic implementations" \
   --type=task \
   --priority=0
```

### Worker-Specific Configuration

Each worker can use different config:

```bash
# Worker 1: Use local Ollama on GPU 1
./worker-node.sh --config config-gpu1.yaml

# Worker 2: Use local Ollama on GPU 2
./worker-node.sh --config config-gpu2.yaml

# Worker 3: Use remote Ollama
./worker-node.sh --config config-remote.yaml
```

## Best Practices

1. **Run bootstrap first** - Ensure all tasks are created before starting workers
2. **Monitor regularly** - Use `make status` to track progress
3. **Handle failures** - Check for failed tasks and reset them
4. **Sync often** - Workers auto-sync, but manual `git pull` helps
5. **Use priorities** - Mark critical directories as high priority
6. **Log everything** - Keep worker logs for debugging
7. **Test first** - Run `make validate` and `make test` before starting workers
8. **Scale gradually** - Start with 2-3 workers, add more as needed

## Comparison: Single vs Distributed

| Feature | Single Node | Distributed |
|---------|-------------|-------------|
| Setup | Simple | Moderate |
| Speed | 1x | Nx (N = workers) |
| Fault Tolerance | Low | High |
| Resource Usage | 1 GPU | N GPUs |
| Monitoring | Built-in | Coordinator script |
| Best For | Small codebases | Large codebases |

## See Also

- [Platform Support](PLATFORM-SUPPORT.md) - OS-specific setup
- [Chunking Explained](CHUNKING-EXPLAINED.md) - How large files are handled
- [bd (beads) Documentation](https://github.com/steveyegge/beads) - Task queue system

