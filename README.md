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
- ✅ **Smart**: Works with any OpenAI-compatible LLM provider (vLLM, TokenHub, OpenAI, etc.)
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

### 2. Start an LLM Provider

You need at least one running OpenAI-compatible LLM server.  Any of these work:

| Provider | Example URL | Notes |
|----------|-------------|-------|
| **vLLM** | `http://localhost:8000` | GPU inference, no key needed |
| **TokenHub** | `http://localhost:8090` | Multi-provider router, optional key |
| **OpenAI** | `https://api.openai.com` | Requires API key |
| **Ollama** (OpenAI mode) | `http://localhost:11434` | Local models |
| **llama.cpp server** | `http://localhost:8080` | CPU/GPU inference |

If you use TokenHub, there's a convenience launcher: `make tokenhub-start`

### 3. Configure

```bash
make config-init
```

Or manually:
```bash
cp config.yaml.sample config.yaml
vim config.yaml
```

**Minimal config.yaml:**
```yaml
llm:
  providers:
    - url: "http://localhost:8000"          # local vLLM — tried first
    - url: "http://my-server:8090"
      api_key: "my-api-key"                # failover with auth
    - url: "https://api.openai.com"
      api_key: "sk-..."                    # cloud fallback

source:
  root: ".."
  build_command: "make -j$(nproc)"  # YOUR build command
  build_timeout: 600

review:
  workflow: "review"                 # review or rewrite
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

## Workflow Modes

The runner has two workflow modes. Personas still provide behavior and taste,
but the workflow mode controls the system prompt, progress index, summary file,
and success criteria.

| Mode | Metadata | Use For |
|------|----------|---------|
| `review` | `REVIEW-INDEX.md`, `REVIEW-SUMMARY.md` | Defect-finding, fixes, audits |
| `rewrite` | `REWRITE-INDEX.md`, `REWRITE-SUMMARY.md` | Translation, refactors, API migrations, decomposition, hardening rewrites |

Rewrite mode is broader than translation. Configure it with an objective and
constraints:

```yaml
review:
  workflow: "rewrite"
  persona: "personas/friendly-mentor"
  rewrite:
    preflight_build: false
    selection_policy: "small_first"  # Use "bottom_up" for normal long runs
    objective: "Rewrite small userland utilities into Rust side-by-side."
    strategy: "Complete one buildable directory at a time."
    output_policy: "Create replacement files beside the original implementation."
    constraints:
      - "Preserve CLI behavior and exit statuses."
      - "Do not start kernel rewrites."
    success_criteria:
      - "The active work-unit build command succeeds."
      - "Existing tests still pass."
```

Rewrite indexes are work-unit graphs, not just flat directory checklists. During
index generation the tool infers units such as FreeBSD commands/libraries,
Rust packages, Rust test units, bootstrap tools, and validation stages. Each
unit can carry:

- `kind` - command, library, Rust package, tests, bootstrap component, etc.
- `stage` - foundation, bootstrap, application, validation, integration, kernel
- `depends_on` - earlier units that should be completed first
- `files` - related source, test, manifest, and build-glue files
- `build_command` / `test_command` - unit-sized validation commands when known

Rewrite mode processes these units bottom-up by stage and dependency by default.
For smoke/e2e runs, set `review.rewrite.selection_policy: "small_first"` to
prefer quick buildable commands and packages before large foundational headers.
`SET_SCOPE` shows the selected unit metadata, and `BUILD` uses the unit-specific
build command when the index can infer one.

## How It Works

### Hierarchical Workflow

```
Source Tree (entire codebase)
  └─ Rewrite Unit (library, command, crate, bootstrap tool)  ← BUILD + COMMIT HERE
      └─ Related files (source, tests, manifests, build glue)
          └─ File (e.g., tcp.c)
              └─ Chunks (individual functions)
```

### Workflow

1. **SET_SCOPE** - Select directory/work-unit key to work
2. **READ_FILE** - Inspect files (chunked if large)
3. **EDIT_FILE** / **WRITE_FILE** - Apply fixes or rewrites
4. **BUILD** - Validate changes with the unit or configured build command
5. **Iterate** - If build fails, fix and rebuild
6. **Commit** - When build succeeds, commit changes
7. **Next** - Move to next work unit

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
│ ├─ llm_client.py     (OpenAI-compat LLM client)    │
│ ├─ persona_validator.py (Agent Spec validation)    │
│ ├─ personas/          (agent configurations)       │
│ │   ├─ freebsd-angry-ai/agent.yaml                │
│ │   ├─ security-hawk/agent.yaml                   │
│ │   └─ ...                                        │
│ └─ config.yaml        (your configuration)        │
└─────────────────────────────────────────────────────┘
          ↓ HTTP /v1/chat/completions        ↓ subprocess
         LLM Provider(s)               (your build command)
         ├─ vLLM, TokenHub, OpenAI
         ├─ Ollama, llama.cpp, etc.
         └─ Any OpenAI-compatible server
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
├── llm_client.py           # OpenAI-compatible LLM client (multi-provider)
├── build_executor.py       # Build system integration
├── chunker.py              # Large file handling
├── config.yaml.sample    # Configuration template
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
- **At least one OpenAI-compatible LLM provider** (vLLM, TokenHub, OpenAI, Ollama, etc.)
- **Your project's build system** (make, cmake, cargo, etc.)
- **Git repository** (for tracking changes)

## Documentation

- [SETUP_GUIDE.md](SETUP_GUIDE.md) - Detailed installation guide
- [AGENTS.md](AGENTS.md) - AI agent instructions
- [docs/](docs/) - Additional documentation

## License

MIT License - See [LICENSE](LICENSE)
