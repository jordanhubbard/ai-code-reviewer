# Quick Start: Distributed Mode

Get multiple GPUs reviewing code in parallel in under 5 minutes.

## Prerequisites

- ✅ Multiple machines with GPUs (or one machine with multiple GPUs)
- ✅ Ollama running on each machine
- ✅ bd (beads) installed: https://github.com/jhutar/beads
- ✅ Git repository with code to review

## 5-Minute Setup

### Step 1: Clone on Each Machine

```bash
# On each worker machine
git clone <your-repo-url>
cd <repo>/ai-code-reviewer
```

### Step 2: Install Dependencies

```bash
# On each machine
make check-deps
```

This auto-detects your OS (FreeBSD/macOS/Linux) and installs Python, pip, and PyYAML.

### Step 3: Configure Ollama

```bash
# On each machine
vim config.yaml
```

Set your Ollama server URL:
```yaml
ollama:
  url: "http://localhost:11434"  # Or remote Ollama server
  model: "qwen2.5-coder:32b"
```

Test it:
```bash
make validate
```

### Step 4: Bootstrap (One Machine Only)

On **any one machine**:
```bash
make bootstrap
```

This creates review tasks for all directories in your source tree.

**Output:**
```
[5/5] Discovering directories and creating tasks...
Found 127 directories to process
Task creation summary:
  - New tasks created: 127
```

### Step 5: Start Workers

On **each machine**:
```bash
make worker
```

Workers will:
- Sync with git
- Claim available tasks
- Review assigned directories
- Push results
- Repeat until done

### Step 6: Monitor Progress

From **any machine**:
```bash
make status
```

**Output:**
```
==========================================
AI Code Review - Distributed Status
==========================================

Overall Progress:
  Total Tasks:      127
  Completed:        45 (35%)
  In Progress:      8
  Pending:          72
  Failed:           2

  [=================----------------------------------] 35%

Estimated Time to Completion:
  ~7h 30m (assuming 8 active workers)
```

## That's It!

Your distributed review system is now running. Workers will automatically:
- Claim work from the queue
- Review code
- Run builds
- Commit changes
- Sync via git

## Common Commands

```bash
# Check available work
bd ready

# Show all tasks
bd list

# Show specific task
bd show bd-42

# Reset a failed task
bd update bd-42 --status pending

# Stop a worker
Ctrl+C  # Graceful shutdown with statistics
```

## Scaling

### Add More Workers Anytime

```bash
# On new machine
git clone <repo-url>
cd ai-code-reviewer
make check-deps
vim config.yaml
make worker  # Automatically syncs and joins work queue
```

### Multiple Workers on One Machine

```bash
# Terminal 1
make worker

# Terminal 2
./worker-node.sh --worker-id gpu-2

# Terminal 3
./worker-node.sh --worker-id gpu-3
```

## Troubleshooting

### No Tasks Available

```bash
# Sync bd database
bd sync

# Check if bootstrap was run
bd list
```

### Worker Can't Push

```bash
# Pull latest changes
git pull --rebase

# Try again
git push
```

### Task Stuck "In Progress"

```bash
# Worker probably crashed, reset task
bd update <task-id> --status pending
```

## Performance

**Example: FreeBSD Source Tree (500 directories)**

| Workers | Time | Speedup |
|---------|------|---------|
| 1 | 40 hours | 1x |
| 4 | 10 hours | 4x |
| 8 | 5 hours | 8x |

Linear scaling with number of workers!

## Next Steps

- Read [DISTRIBUTED-MODE.md](DISTRIBUTED-MODE.md) for detailed documentation
- Customize priorities: `bd update <id> --priority 0` (critical first)
- Monitor from laptop: `make status` (no GPU needed)
- Review logs: `personas/*/logs/`

## Architecture Summary

```
Git Repo (.beads/issues.jsonl)
    ↑ push/pull    ↑ push/pull    ↑ push/pull
    │              │              │
Worker 1       Worker 2       Worker 3
GPU 1          GPU 2          GPU 3
```

- **No central server** - workers coordinate via git
- **Atomic claiming** - bd prevents race conditions
- **Fault tolerant** - workers can crash/restart
- **Git-synced** - all state in `.beads/issues.jsonl`

Happy parallel reviewing! 🚀

