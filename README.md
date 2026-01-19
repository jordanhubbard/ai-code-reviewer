# AI Code Reviewer

**AI-powered code reviewer with build validation for ANY codebase**

## What It Does

1. **Reviews code hierarchically**: Directory → File → Function
2. **Makes fixes automatically**: Edits source files based on analysis  
3. **Validates with build**: Runs your project's build command to test changes
4. **Iterates until success**: If build fails, fixes errors and rebuilds
5. **Commits working code**: Only commits when build succeeds

## Key Features

- ✅ **Generic**: Works with any language/build system
- ✅ **Scalable**: Function-by-function chunking handles files of any size
- ✅ **Safe**: Tests every change with your build system
- ✅ **Autonomous**: Runs for hours reviewing entire directories
- ✅ **Smart**: Uses LLM via OpenAI-compatible vLLM and/or Ollama
- ✅ **Parallel**: Optional concurrent file processing for faster reviews (experimental)
- ✅ **Self-Healing**: Auto-detects loops, learns from build failures, files systemic issues
- ✅ **Secure**: Scans commits for secrets before pushing

## Works With

| Language/Project | Build Command | Example |
|------------------|---------------|---------|
| C/C++ (Make) | `make -j$(nproc)` | Linux kernel |
| C/C++ (CMake) | `cmake --build build -j$(nproc)` | LLVM, Qt |
| FreeBSD | `sudo make -j$(sysctl -n hw.ncpu) buildworld` | FreeBSD source |
| Rust | `cargo build --release` | Rustc, ripgrep |
| Go | `go build ./...` | Kubernetes, Docker |
| Python | `python -m pytest` | Django, Flask |
| Node.js | `npm test` | React, Vue |

**Any project with a build/test command works!**

## Quick Start

### 1. Install Dependencies

The recommended setup is to let the Makefile manage a project-local virtualenv.

```bash
make check-deps
```

Notes:
- `scripts/config-init.sh` requires `bash` (on FreeBSD it is typically `/usr/local/bin/bash`).
- If `make check-deps` reports missing `bd` (beads), issue tracking is disabled but the reviewer can still run.

### 2. Set Up an LLM Server (vLLM or Ollama)

#### Option A: vLLM (OpenAI-compatible, high performance)

On a machine with GPU:
```bash
docker run -it --gpus all -p 8000:8000   --ipc=host --ulimit memlock=-1 --ulimit stack=67108864   -v ~/.cache/huggingface:/root/.cache/huggingface   nvcr.io/nvidia/vllm:25.12.post1-py3   vllm serve "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16"     --trust-remote-code
```

#### Option B: Ollama

On a machine with GPU:
```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Pull a code-focused model
ollama pull qwen2.5-coder:32b

# Run Ollama with external access
OLLAMA_HOST=0.0.0.0:11434 ollama serve
```

### 3. Configure

**Recommended (interactive):**

```bash
make config-init
```

**Or manual:**

```bash
cd ai-code-reviewer
cp config.yaml.defaults config.yaml
vim config.yaml
```

**Edit config.yaml:**
```yaml
llm:
  hosts:
    - "http://your-llm-server"  # vLLM (:8000) or Ollama (:11434)
  models:
    - "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16"  # vLLM model name
    - "qwen2.5-coder:32b"                           # Ollama fallback

source:
  root: ".."  # Path to your source code
  
  # THIS IS THE KEY LINE - YOUR BUILD COMMAND
  build_command: "make -j$(nproc)"  # Change to YOUR build command!
  
  build_timeout: 600  # Seconds (10 minutes)
  pre_build_command: ""  # Optional setup command
```

You can list **multiple hosts** (vLLM and/or Ollama) and **multiple models**; the tool probes each host and uses the first available model in order.

### 4. Run

```bash
make run
```

For more output while debugging:

```bash
make run-verbose
```

The AI will:
1. Pick a directory from your source tree
2. Review all files in that directory
3. Make fixes
4. Run your build command
5. If build fails: iterate until it succeeds
6. Commit changes
7. Move to next directory

## Make Targets

Run `make help` for the authoritative list. Common targets:

| Target | What it does |
|--------|--------------|
| `make help` | Show available targets |
| `make check-deps` | Create `.venv/` and install required Python deps (auto-run by `make run`) |
| `make deps` | Alias for `check-deps` |
| `make config-init` | Interactive setup wizard to create `config.yaml` |
| `make config-update` | Merge new defaults into an existing `config.yaml` |
| `make validate` | Validate LLM connectivity/model availability (`--validate-only`) |
| `make run` | Run the full review loop (auto-runs `check-deps` and `config-init` when needed) |
| `make run-verbose` | Run the reviewer with verbose logging (uses existing `config.yaml`) |
| `make test` | Fast local checks (syntax/import/config migration) |
| `make test-all` | Extra component tests that require a running LLM server |
| `make release` | Run tests, tag, and create a GitHub release |
| `make clean` | Remove caches/logs (local) |
| `make clean-all` | `clean` plus any leftover local model weights |

### Logs

- `make run` session log: `.reviewer-log/make-run-*.log`
- Internal ops log: `.reviewer-log/ops.jsonl`
- Per-step conversation logs: `personas/<persona>/logs/step_*.txt`
- Source-tree metadata (per audited project): `<source.root>/.ai-code-reviewer/`

## Configuration Examples

### Linux Kernel

```yaml
source:
  root: "/usr/src/linux"
  build_command: "make -j$(nproc) bzImage modules"
  build_timeout: 1800  # 30 minutes
```

### Rust Project

```yaml
source:
  root: "/home/user/my-rust-project"
  build_command: "cargo build --release && cargo test"
  build_timeout: 300  # 5 minutes
```

### CMake Project

```yaml
source:
  root: "/home/user/my-cmake-project"
  build_command: "cmake --build build --target all -j$(nproc)"
  build_timeout: 600  # 10 minutes
  pre_build_command: "cmake -B build -S ."  # Run once at startup
```

### Python Project

```yaml
source:
  root: "/home/user/my-python-project"
  build_command: "python -m pytest tests/"
  build_timeout: 120  # 2 minutes
```

### Go Project

```yaml
source:
  root: "/home/user/my-go-project"
  build_command: "go build ./... && go test ./..."
  build_timeout: 180  # 3 minutes
```

## How It Works

### Hierarchical Review

```
Level 1: Source Tree (entire codebase)
  └─ Level 2: Directory (e.g., src/network/)  ← BUILD + COMMIT HERE
      └─ Level 3: File (e.g., tcp.c)
          └─ Level 4: Chunks (individual functions)
```

**Build and commit happen at directory level**, not per-file or per-function.

### Workflow

```
Step 1:  SET_SCOPE src/network/
Step 2:  READ_FILE tcp.c (review function-by-function if large)
Step 3:  EDIT_FILE tcp.c (fix bug in send_packet function)
Step 4:  READ_FILE udp.c
Step 5:  EDIT_FILE udp.c (fix bug in recv_packet function)
...
Step 10: BUILD (run your build_command)
         → Build fails: "undefined symbol recv_packet"
Step 11: EDIT_FILE udp.c (fix the error)
Step 12: BUILD
         → Build succeeds! ✓
Step 13: Commit all changes for src/network/
Step 14: SET_SCOPE src/storage/ (next directory)
```

### Large File Handling

Files over 800 lines are automatically chunked by function:

- `tcp.c` (2000 lines) → 40 chunks (functions)
- Reviews one function at a time
- No timeouts, no memory issues
- Can handle files of **any size**

## Architecture

```
┌─────────────────────────────────────────────────────┐
│ Your Machine (FreeBSD/Linux/macOS)                 │
│ ┌─────────────────────────────────────────────────┐ │
│ │ ai-code-reviewer/                               │ │
│ │ ├─ reviewer.py        (main loop)              │ │
│ │ ├─ ollama_client.py   (AI communication)       │ │
│ │ ├─ build_executor.py  (runs YOUR build cmd)    │ │
│ │ ├─ chunker.py         (file splitting)         │ │
│ │ └─ config.yaml        (YOUR configuration)     │ │
│ └─────────────────────────────────────────────────┘ │
│          ↓ HTTP                    ↓ subprocess     │
│    (to Ollama)              (your build command)    │
└─────────────────────────────────────────────────────┘
          ↓                                            
┌─────────────────────────────────────────────────────┐
│ LLM Server (vLLM or Ollama, can be remote)         │
│ ├─ OpenAI-compatible vLLM on :8000                 │
│ └─ Ollama on :11434 (or any model)                 │
└─────────────────────────────────────────────────────┘
```

## Personas

The tool supports multiple **review personalities** via the persona system.

### Default: FreeBSD Commit Blocker

The default persona is **"The FreeBSD Commit Blocker"** - a ruthless security auditor that:
- ✓ Battle-tested on FreeBSD source tree
- ✓ Found 8+ critical bugs (buffer overflows, TOCTOU races, integer overflows)
- ✓ 16 files reviewed with full history
- ✓ Learned lessons from real security issues
- ✓ Enforces style(9) and POSIX compliance
- ✓ Never accepts "good enough"

**Why this persona?** It's proven effective at finding subtle security vulnerabilities.

### Available Personas

Located in `personas/` directory:

| Persona | Focus | Tone | Best For |
|---------|-------|------|----------|
| **freebsd-angry-ai** ⚡ | Security (default) | Ruthless | Production audits, proven on FreeBSD |
| **security-hawk** 🦅 | Security (extreme) | Paranoid | High-value targets, compliance |
| **performance-cop** 🚀 | Speed/Efficiency | Demanding | Hot paths, scalability |
| **friendly-mentor** 🌟 | Learning | Supportive | Training, onboarding |
| **example** 📝 | Template | Balanced | Creating custom personas |

### Creating Your Own Persona

Copy and customize:
```bash
cp -r personas/example personas/my-persona
vim personas/my-persona/AI_START_HERE.md  # Define behavior
vim personas/my-persona/PERSONA.md        # Set personality

# Update config.yaml
review:
  persona: "personas/my-persona"
```

Make it:
- Friendly and encouraging
- Security-paranoid
- Performance-obsessed
- Style-enforcing
- Domain-specific (embedded, web, systems)

## Extracting to Standalone Repo

This tool is **completely generic**. To use it with your project:

```bash
# Option 1: Copy the ai-code-reviewer/ directory
cp -r ai-code-reviewer /path/to/your/project/

# Option 2: Git submodule (if extracted to standalone repo)
cd /path/to/your/project
git submodule add https://github.com/you/ai-code-reviewer.git reviewer
cd reviewer
cp config.yaml.defaults config.yaml
vim config.yaml  # Set your build_command
make run
```

## Requirements

- **Python 3.8+** with PyYAML
- **LLM server** (OpenAI-compatible vLLM and/or Ollama) with a code model
- **Your project's build system** (make, cmake, cargo, etc.)
- **Git repository** (for tracking changes)

## License

[Your license here]

## Credits

Originally created for FreeBSD source tree auditing, but designed to work with any codebase.

