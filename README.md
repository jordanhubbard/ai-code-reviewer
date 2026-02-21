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
- ✅ **Smart**: Uses LLM via [TokenHub](https://github.com/jordanhubbard/tokenhub) — handles all provider routing, model selection, and cost management
- ✅ **Parallel**: Optional concurrent file processing for faster reviews
- ✅ **Self-Healing**: Auto-detects loops, learns from build failures
- ✅ **Configurable**: Multiple agent personalities via Oracle Agent Spec

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
make check-deps
```

### 2. Start TokenHub

All LLM provider selection, model routing, and cost management is delegated to
[TokenHub](https://github.com/jordanhubbard/tokenhub) (`~/Src/tokenhub`).  The smart launcher
picks the best available option automatically:

```bash
make tokenhub-start
```

The launcher tries, in order:
1. **Remote instance** already reachable at the configured URL
2. **Existing Docker container** — restarts it if stopped
3. **New Docker container** — Linux/macOS with Docker available
4. **Native binary** — built from `~/Src/tokenhub` (works on FreeBSD and any platform without Docker)

### 3. Configure

```bash
make config-init
```

Or manually:
```bash
cp config.yaml.defaults config.yaml
vim config.yaml
```

**Minimal config.yaml:**
```yaml
tokenhub:
  url: "http://localhost:8090"   # URL of your TokenHub instance
  api_key: "tokenhub_..."        # Bearer token (create via make config-init)

source:
  root: ".."
  build_command: "make -j$(nproc)"  # YOUR build command
  build_timeout: 600

review:
  persona: "personas/freebsd-angry-ai"  # Choose your agent
```

### 4. Run

```bash
make run
```

## Agents (Oracle Agent Spec)

Agents define the AI's review personality and focus. Each agent is configured in [Oracle Agent Spec](https://oracle.github.io/agent-spec/26.1.0/) format.

### Available Agents

| Agent | Focus | Best For |
|-------|-------|----------|
| **freebsd-angry-ai** | Security, style(9), POSIX | Production audits (default) |
| **security-hawk** | Vulnerabilities, exploits | Security-critical code |
| **performance-cop** | Speed, algorithms, cache | Performance optimization |
| **friendly-mentor** | Learning, best practices | Training, onboarding |
| **example** | Balanced, educational | General code review |

### Agent Configuration

Each agent is defined in `personas/<name>/agent.yaml`:

```yaml
component_type: Agent
agentspec_version: "26.1.0"
name: "Security Hawk"
description: "Paranoid security auditor"

inputs:
  - title: "codebase_path"
    type: "string"

outputs:
  - title: "review_summary"
    type: "string"
  - title: "critical_vulnerabilities"
    type: "integer"

system_prompt: |
  You are a paranoid security auditor...
  
llm_config:
  component_type: OpenAiCompatibleConfig
  name: "{{llm_name}}"
  url: "{{llm_url}}"
  model_id: "{{model_id}}"
```

### Creating Custom Agents

```bash
# Copy an existing agent
cp -r personas/example personas/my-agent

# Edit the agent configuration
vim personas/my-agent/agent.yaml

# Validate
python3 persona_validator.py personas/my-agent

# Use in config.yaml
review:
  persona: "personas/my-agent"
```

## How It Works

### Hierarchical Review

```
Source Tree (entire codebase)
  └─ Directory (e.g., src/network/)  ← BUILD + COMMIT HERE
      └─ File (e.g., tcp.c)
          └─ Chunks (individual functions)
```

### Workflow

1. **SET_SCOPE** - Select directory to review
2. **READ_FILE** - Review files (chunked if large)
3. **EDIT_FILE** - Fix issues found
4. **BUILD** - Validate changes with your build command
5. **Iterate** - If build fails, fix and rebuild
6. **Commit** - When build succeeds, commit changes
7. **Next** - Move to next directory

### Large File Handling

Files over 400 lines are automatically chunked by function:
- Reviews one function at a time
- No timeouts or memory issues
- Handles files of any size

## Architecture

```
┌─────────────────────────────────────────────────────┐
│ ai-code-reviewer/                                   │
│ ├─ reviewer.py        (main loop)                  │
│ ├─ tokenhub_client.py (TokenHub HTTP client)       │
│ ├─ persona_validator.py (Agent Spec validation)    │
│ ├─ personas/          (agent configurations)       │
│ │   ├─ freebsd-angry-ai/agent.yaml                │
│ │   ├─ security-hawk/agent.yaml                   │
│ │   └─ ...                                        │
│ └─ config.yaml        (your configuration)        │
└─────────────────────────────────────────────────────┘
          ↓ HTTP /v1/chat/completions        ↓ subprocess
         TokenHub (provider routing)    (your build command)
         ├─ Docker container
         ├─ Native binary (FreeBSD, etc.)
         └─ Remote instance
```

## Project Structure

```
ai-code-reviewer/
├── personas/                 # Agent configurations (Agent Spec)
│   ├── freebsd-angry-ai/
│   │   ├── agent.yaml       # Agent Spec configuration
│   │   └── README.md        # Agent documentation
│   ├── security-hawk/
│   ├── performance-cop/
│   ├── friendly-mentor/
│   └── example/
├── reviewer.py              # Main review loop
├── persona_validator.py     # Agent Spec validator
├── tokenhub_client.py      # TokenHub HTTP client
├── build_executor.py       # Build system integration
├── chunker.py              # Large file handling
├── config.yaml.defaults    # Configuration template
├── AGENTS.md               # AI agent instructions
└── docs/                   # Additional documentation
```

## Configuration Examples

### Linux Kernel
```yaml
source:
  root: "/usr/src/linux"
  build_command: "make -j$(nproc) bzImage modules"
  build_timeout: 1800
```

### Rust Project
```yaml
source:
  root: "/home/user/my-rust-project"
  build_command: "cargo build --release && cargo test"
  build_timeout: 300
```

### Python Project
```yaml
source:
  root: "/home/user/my-python-project"
  build_command: "python -m pytest tests/"
  build_timeout: 120
```

## Make Targets

| Target | Description |
|--------|-------------|
| `make help` | Show all targets |
| `make check-deps` | Install dependencies |
| `make config-init` | Interactive setup |
| `make validate` | Validate LLM connection |
| `make run` | Run code review |
| `make run-verbose` | Run with verbose logging |
| `make test` | Run tests |

## Requirements

- **Python 3.8+** with PyYAML
- **TokenHub** (`~/Src/tokenhub`) — Docker container, native binary, or remote instance
- **Your project's build system** (make, cmake, cargo, etc.)
- **Git repository** (for tracking changes)

## Documentation

- [SETUP_GUIDE.md](SETUP_GUIDE.md) - Detailed installation guide
- [AGENTS.md](AGENTS.md) - AI agent instructions
- [docs/](docs/) - Additional documentation

## License

MIT License - See [LICENSE](LICENSE)
