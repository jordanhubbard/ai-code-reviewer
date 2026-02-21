# Quick Setup Guide

## First Time Setup

### 1. Check Dependencies

```bash
make check-deps
```

This will check for:
- Python 3 ✓
- pip ✓
- PyYAML ✓

### 2. Create config.yaml

**Recommended (interactive):**
```bash
make config-init
```

**Or manual:**
```bash
cp config.yaml.defaults config.yaml
vim config.yaml
```

### 3. Edit config.yaml

```yaml
# 1. TokenHub connection (REQUIRED)
tokenhub:
  url: "http://localhost:8090"   # URL of your TokenHub instance
  api_key: "tokenhub_..."        # Bearer token (create via make config-init)

# 2. Source tree (REQUIRED)
source:
  root: "/path/to/your/source"  # CHANGE THIS!
  build_command: "make -j$(nproc)"  # YOUR build command
  build_timeout: 600

# 3. Agent selection (OPTIONAL)
review:
  persona: "personas/freebsd-angry-ai"  # See available agents below
```

### 4. Validate Connection

```bash
python3 reviewer.py --validate-only
```

### 5. Run

```bash
make run
```

Or with verbose logging:
```bash
make run-verbose
```

## Available Agents

Agents define the AI's review personality. Each is configured in [Oracle Agent Spec](https://oracle.github.io/agent-spec/) format.

| Agent | Focus | Use When |
|-------|-------|----------|
| `personas/freebsd-angry-ai` | Security, style(9) | Production audits (default) |
| `personas/security-hawk` | Vulnerabilities | Security-critical code |
| `personas/performance-cop` | Speed, algorithms | Performance optimization |
| `personas/friendly-mentor` | Learning | Training, onboarding |
| `personas/example` | Balanced | General code review |

### Agent Configuration

Each agent is defined in `personas/<name>/agent.yaml`:

```yaml
component_type: Agent
agentspec_version: "26.1.0"
name: "Agent Name"
description: "What this agent does"
system_prompt: |
  Your instructions and personality...
inputs:
  - title: "codebase_path"
    type: "string"
outputs:
  - title: "review_summary"
    type: "string"
```

### Validate Agent

```bash
python3 persona_validator.py personas/security-hawk
```

## Configuration Examples

### FreeBSD Source Tree
```yaml
source:
  root: "/usr/src"
  build_command: "sudo make -j$(sysctl -n hw.ncpu) buildworld"
  build_timeout: 7200
review:
  persona: "personas/freebsd-angry-ai"
```

### Linux Kernel
```yaml
source:
  root: "/usr/src/linux"
  build_command: "make -j$(nproc)"
  build_timeout: 1800
review:
  persona: "personas/security-hawk"
```

### Rust Project
```yaml
source:
  root: "/home/you/my-rust-project"
  build_command: "cargo build --release"
  build_timeout: 600
review:
  persona: "personas/performance-cop"
```

### Python Project
```yaml
source:
  root: "/home/you/my-python-project"
  build_command: "python -m pytest"
  build_timeout: 300
review:
  persona: "personas/friendly-mentor"
```

## Creating Custom Agents

1. Copy an existing agent:
   ```bash
   cp -r personas/example personas/my-agent
   ```

2. Edit `personas/my-agent/agent.yaml`:
   ```yaml
   component_type: Agent
   agentspec_version: "26.1.0"
   name: "My Custom Agent"
   description: "Specialized for my needs"
   
   system_prompt: |
     You are a code reviewer specialized in...
     
     ## Your Focus
     - Item 1
     - Item 2
     
     ## Your Personality
     - Trait 1
     - Trait 2
   
   inputs:
     - title: "codebase_path"
       type: "string"
   
   outputs:
     - title: "review_summary"
       type: "string"
   ```

3. Validate:
   ```bash
   python3 persona_validator.py personas/my-agent
   ```

4. Use in config.yaml:
   ```yaml
   review:
     persona: "personas/my-agent"
   ```

## Common Issues

### "Source root does not appear to be a buildable project"

**Solution**: Set `source.root` to your actual source directory:
```yaml
source:
  root: "/path/to/your/source"
```

### "No agent configuration found"

**Solution**: Each agent needs an `agent.yaml` file:
```bash
ls personas/freebsd-angry-ai/agent.yaml
```

### Build hangs or takes forever

**Solutions**:
1. Verify `source.root` points to the correct directory
2. Test your build command manually first
3. Adjust `build_timeout` in config.yaml
4. Use `--skip-preflight` to skip initial build check

### Agent validation fails

**Solution**: Check your agent.yaml format:
```bash
python3 persona_validator.py personas/your-agent
```

Required fields:
- `component_type: Agent`
- `name`
- `system_prompt`

## File Locations

### Agent Configurations
```
personas/<name>/
├── agent.yaml    # Agent Spec configuration (required)
└── README.md     # Agent documentation (optional)
```

### Per-Project Data (in source tree)
```
<source-root>/.ai-code-reviewer/
├── LESSONS.md        # Learned patterns
├── REVIEW-SUMMARY.md # Progress tracking
└── logs/             # Session logs
```

### Logs
- Session logs: `.reviewer-log/make-run-*.log`
- Operations log: `.reviewer-log/ops.jsonl`

## Next Steps

After successful setup:

1. The system generates a review index of your source tree
2. Runs a pre-flight build check
3. AI starts reviewing code directory by directory
4. Progress tracked in `<source>/.ai-code-reviewer/`

## Getting Help

- Check logs: `.reviewer-log/`
- Enable verbose mode: `make run-verbose`
- Validate agent: `python3 persona_validator.py personas/<name>`
- Check git status: `git status`

## References

- [Oracle Agent Spec](https://oracle.github.io/agent-spec/26.1.0/)
- [README.md](README.md) - Full documentation
- [AGENTS.md](AGENTS.md) - AI agent instructions
