# Angry AI - Universal Code Reviewer

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
- ✅ **Smart**: Uses LLM (via Ollama) for intelligent analysis

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

## Modes of Operation

### Single-Node Mode
One machine reviews the entire codebase sequentially. Simple setup, good for small to medium projects.

### Distributed Mode (NEW!)
Multiple machines with GPUs review code in parallel. Dramatically faster for large codebases.

**Features:**
- ✅ **Nx speedup** (N = number of workers)
- ✅ **No central server** - workers coordinate via git
- ✅ **Fault tolerant** - workers can crash/restart
- ✅ **Easy scaling** - add more workers anytime

See [docs/DISTRIBUTED-MODE.md](docs/DISTRIBUTED-MODE.md) for details.

---

## Quick Start (Single-Node Mode)

### 1. Install Dependencies

The Makefile automatically detects your OS (FreeBSD, macOS, Linux) and uses the appropriate package manager:

```bash
make check-deps
```

This will:
- **FreeBSD**: Use `pkg install` for python3 and pip
- **macOS**: Use Homebrew (`brew install`) for python3, then install pip
- **Linux**: Use `apt-get` (Debian/Ubuntu), `yum` (RHEL/CentOS), or `dnf` (Fedora)
- Install PyYAML via pip (cross-platform)

Or install manually if you prefer:

```bash
# FreeBSD
sudo pkg install python3 py311-pip
python3 -m pip install --user pyyaml

# macOS
brew install python3
python3 -m pip install --user pyyaml

# Linux (Debian/Ubuntu)
sudo apt-get install python3 python3-pip
python3 -m pip install --user pyyaml

# Linux (RHEL/CentOS/Fedora)
sudo dnf install python3 python3-pip
python3 -m pip install --user pyyaml
```

### 2. Set Up Ollama Server

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

```bash
cd angry-ai
cp config.yaml.defaults config.yaml
vim config.yaml
```

**Edit config.yaml:**
```yaml
ollama:
  url: "http://your-ollama-server:11434"
  model: "qwen2.5-coder:32b"

source:
  root: ".."  # Path to your source code
  
  # THIS IS THE KEY LINE - YOUR BUILD COMMAND
  build_command: "make -j$(nproc)"  # Change to YOUR build command!
  
  build_timeout: 600  # Seconds (10 minutes)
  pre_build_command: ""  # Optional setup command
```

### 4. Run

**Single-Node Mode:**
```bash
make run
```

**Distributed Mode (Multiple GPUs):**
```bash
# On each machine: Start worker (bootstrap runs automatically)
make worker

# Monitor progress (any machine)
make status
```

**Note:** Both `make run` and `make worker` automatically run bootstrap first (which is idempotent and safe to run multiple times).

The AI will:
1. Pick a directory from your source tree (or claim from queue in distributed mode)
2. Review all files in that directory
3. Make fixes
4. Run your build command
5. If build fails: iterate until it succeeds
6. Commit changes
7. Move to next directory

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
│ │ angry-ai/                                       │ │
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
│ Ollama Server (GPU machine, can be remote)         │
│ ├─ qwen2.5-coder:32b (or any model)                │
│ └─ Listens on 0.0.0.0:11434                        │
└─────────────────────────────────────────────────────┘
```

## Personas

The tool supports multiple **review personalities** via the persona system.

### Default: FreeBSD Angry AI

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
# Option 1: Copy the angry-ai/ directory
cp -r angry-ai /path/to/your/project/

# Option 2: Git submodule (if extracted to standalone repo)
cd /path/to/your/project
git submodule add https://github.com/you/angry-ai-reviewer.git reviewer
cd reviewer
cp config.yaml.defaults config.yaml
vim config.yaml  # Set your build_command
make run
```

## Requirements

- **Python 3.8+** with PyYAML
- **Ollama server** (local or remote) with a code model
- **Your project's build system** (make, cmake, cargo, etc.)
- **Git repository** (for tracking changes)
- **bd (beads)** (for distributed mode only) - https://github.com/jhutar/beads

## License

[Your license here]

## Credits

Originally created for FreeBSD source tree auditing, but designed to work with any codebase.

