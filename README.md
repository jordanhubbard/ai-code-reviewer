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

## Quick Start

### 1. Install Dependencies

```bash
# On FreeBSD
sudo pkg install python3 py311-pip
sudo pip install pyyaml

# On Linux
sudo apt install python3 python3-pip
pip3 install pyyaml
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

```bash
make run
```

The AI will:
1. Pick a directory from your source tree
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

## Why "Angry AI"?

The default persona is an **unforgiving code reviewer** that:
- Never accepts "good enough"
- Finds subtle bugs
- Enforces best practices
- Educates through aggressive comments
- Acts like a senior engineer blocking your commit

**But you can customize the persona!** Edit `AI_START_HERE.md` to make it:
- Friendly and helpful
- Security-focused
- Performance-focused
- Style-focused
- Or anything else

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

## License

[Your license here]

## Credits

Originally created for FreeBSD source tree auditing, but designed to work with any codebase.

