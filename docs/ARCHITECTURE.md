# Architecture: Beads Database Location

## The Correct Model

The `ai-code-reviewer` is a **generic tool** (like a compiler or linter). The beads database lives in the **project being reviewed** (like build artifacts or test results).

```
ai-code-reviewer/          ← Generic tool (NO .beads/ here!)
├── reviewer.py
├── bootstrap.sh
├── worker-node.sh
└── config.yaml
      ↓ points to
freebsd-src/               ← Project being reviewed
├── .beads/                ← Beads database HERE!
│   ├── issues.jsonl       ← Task queue
│   └── beads.db           ← Local cache (not git-tracked)
├── bin/
├── lib/
└── sys/
```

## Why This Matters

### Problem: Beads in Tool Directory (WRONG)

```
❌ ai-code-reviewer/.beads/
   → Tasks like "Review directory: ../"
   → Different workers have different "../"
   → Path conflicts, duplicate work
```

**Example:**
- Worker 1: `../` = `/home/user1/freebsd-src/`
- Worker 2: `../` = `/home/user2/freebsd/`  
- **Result:** Two different task queues, no coordination!

### Solution: Beads in Source Repository (CORRECT)

```
✅ freebsd-src/.beads/
   → Tasks like "Review directory: bin/cat"
   → All workers share same absolute paths
   → One source of truth, perfect coordination
```

**Example:**
- Worker 1: `source_root = /home/user1/freebsd-src/`
- Worker 2: `source_root = /home/user2/freebsd-src/`
- Both point to `freebsd-src/.beads/` with relative paths
- **Result:** Same task queue, perfect coordination!

## Configuration

### config.yaml Structure

```yaml
source:
  root: "/absolute/path/to/freebsd-src"  # Where your code lives
  build_command: "make buildworld"
```

### What Happens

1. **Bootstrap reads config.yaml:**
   ```bash
   SOURCE_ROOT=$(yaml_parse config.yaml 'source.root')
   cd "$SOURCE_ROOT"
   bd init  # Creates .beads/ HERE
   ```

2. **Creates tasks with relative paths:**
   ```bash
   cd "$SOURCE_ROOT"
   bd create "Review directory: bin/cat"  # Relative to SOURCE_ROOT
   bd create "Review directory: lib/libc"
   ```

3. **Workers claim from SOURCE_ROOT:**
   ```bash
   cd "$SOURCE_ROOT"
   bd ready  # Find available tasks
   bd update task-123 --status in_progress
   ```

4. **Reviewer.py operates on SOURCE_ROOT:**
   ```bash
   python3 reviewer.py --directory "bin/cat"
   # Expands to: $SOURCE_ROOT/bin/cat
   ```

## Multi-Repository Support

The `ai-code-reviewer` tool can work on **multiple projects simultaneously**:

```
ai-code-reviewer/           ← One tool
├── config-freebsd.yaml     ← Points to freebsd-src
├── config-linux.yaml       ← Points to linux-src
└── config-rust.yaml        ← Points to rust-project

freebsd-src/                ← Project 1
└── .beads/                 ← Its own task queue

linux-src/                  ← Project 2  
└── .beads/                 ← Its own task queue

rust-project/               ← Project 3
└── .beads/                 ← Its own task queue
```

**Usage:**
```bash
# Review FreeBSD
make worker CONFIG=config-freebsd.yaml

# Review Linux (different terminal)
make worker CONFIG=config-linux.yaml

# Review Rust (different terminal)
make worker CONFIG=config-rust.yaml
```

## Submodule Scenario

This is why the architecture matters for your case:

```
freebsd-on-angry-AI/        ← Parent repo
├── .beads/                 ← For tracking work on THIS repo
├── README.md
└── ai-code-reviewer/       ← Submodule (generic tool)
    ├── config.yaml
    │   └── source.root: "../"  ← Points to parent!
    └── (no .beads/ here)
```

**What happens:**
1. `config.yaml` sets `source.root: "../"`
2. Bootstrap resolves to absolute: `/path/to/freebsd-on-angry-AI/`
3. Creates `.beads/` in parent directory
4. Tasks use paths relative to parent
5. **Works correctly!**

## Path Resolution

### Bootstrap Phase

```bash
# Read from config
SOURCE_ROOT=$(read_yaml source.root)  # Could be "../" or "/abs/path"

# Resolve to absolute path
SOURCE_ROOT=$(cd "$SOURCE_ROOT" && pwd)  # /abs/path

# Everything operates here
cd "$SOURCE_ROOT"
bd init
find . -type d | while read dir; do
    bd create "Review directory: $dir"  # Relative to SOURCE_ROOT
done
```

### Worker Phase

```bash
# Read from config
SOURCE_ROOT=$(read_yaml source.root)
SOURCE_ROOT=$(cd "$SOURCE_ROOT" && pwd)

# Claim task
cd "$SOURCE_ROOT"
TASK=$(bd ready | head -1)  # "Review directory: bin/cat"

# Extract directory
DIR="bin/cat"  # Relative path

# Run reviewer
python3 ai-code-reviewer/reviewer.py --directory "$DIR"
# Reviewer knows SOURCE_ROOT from config, operates on:
# $SOURCE_ROOT/bin/cat
```

## Git Operations

All git operations happen in **SOURCE_ROOT**, not in `ai-code-reviewer/`:

```bash
cd "$SOURCE_ROOT"

# Sync beads database
git pull --rebase
git add .beads/issues.jsonl
git commit -m "Worker claimed task"
git push

# Sync code changes
git add bin/cat/cat.c  # Changes from review
git commit -m "Fix buffer overflow"
git push
```

## Benefits

1. **Generic Tool:** `ai-code-reviewer` works on any project
2. **Multiple Projects:** Can review different codebases simultaneously
3. **No Path Conflicts:** All paths relative to single source root
4. **Clean Separation:** Tool artifacts stay with tool, project artifacts stay with project
5. **Submodule Friendly:** Works whether tool is submodule or standalone

## Anti-Pattern: Tool-Centric Beads

```
❌ WRONG:
ai-code-reviewer/
├── .beads/
│   └── issues.jsonl
│       └── "Review ../bin/cat"  ← Ambiguous path!
└── config.yaml

Different workers, different "../" → Chaos!
```

## Correct Pattern: Source-Centric Beads

```
✅ CORRECT:
freebsd-src/
├── .beads/
│   └── issues.jsonl
│       └── "Review bin/cat"     ← Clear, relative path
├── bin/
└── lib/

All workers share same freebsd-src/.beads/ → Harmony!
```

## Summary

| Aspect | Tool Directory | Source Directory |
|--------|---------------|------------------|
| Location | `ai-code-reviewer/.beads/` | `freebsd-src/.beads/` |
| Paths | Ambiguous ("../") | Clear ("bin/cat") |
| Multi-worker | Conflicts | Coordinated |
| Multi-project | Broken | Supported |
| Correctness | ❌ WRONG | ✅ CORRECT |

**Rule:** Beads database lives in the **source repository being reviewed**, not in the **tool repository**.

