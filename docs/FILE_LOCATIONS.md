# File Location Architecture

## Overview

This document explains where different types of files are stored and why.

## Two Categories of Files

### 1. Agent Configurations - `personas/<name>/`

**Location**: `ai-code-reviewer/personas/<agent-name>/`  
**Purpose**: Reusable AI agent configurations  
**Git**: Committed to ai-code-reviewer repo  
**Shared**: YES - same agent can review multiple projects

Files:
- `agent.yaml` - Agent Spec configuration (required)
- `README.md` - Agent documentation (optional)

**Format**: [Oracle Agent Spec](https://oracle.github.io/agent-spec/26.1.0/)

```yaml
component_type: Agent
agentspec_version: "26.1.0"
name: "Agent Name"
description: "What this agent does"
metadata:
  focus_areas: [security, performance]
inputs:
  - title: "codebase_path"
    type: "string"
outputs:
  - title: "review_summary"
    type: "string"
system_prompt: |
  Your complete instructions...
llm_config:
  component_type: OpenAiCompatibleConfig
  name: "{{llm_name}}"
  url: "{{llm_url}}"
  model_id: "{{model_id}}"
```

**Why here?**: Agents are templates that define HOW to review, not WHAT was reviewed.

### 2. Source-Specific Files (Per-Project) - `<source-root>/.ai-code-reviewer/`

**Location**: `<your-source-tree>/.ai-code-reviewer/`  
**Purpose**: Per-project review history and lessons  
**Git**: In the source tree being reviewed  
**Shared**: NO - each project has its own

Files:
- `LESSONS.md` - Mistakes learned from THIS codebase
- `REVIEW-SUMMARY.md` - Progress and history for THIS project
- `REVIEW-INDEX.md` - Directory completion status
- `logs/` - Build logs and detailed output (gitignored)

**Why here?**: 
- Lessons are specific to the codebase
- Review progress is per-project
- Multiple projects can use the same agent but have separate histories

Example:
```
/usr/src/freebsd/.ai-code-reviewer/LESSONS.md       # FreeBSD-specific lessons
/home/user/linux/.ai-code-reviewer/LESSONS.md       # Linux-specific lessons
```

## Directory Structure

```
ai-code-reviewer/                    # Tool directory
├── personas/                        # Agent configurations
│   ├── freebsd-angry-ai/
│   │   ├── agent.yaml              # Agent Spec config
│   │   └── README.md               # Documentation
│   ├── security-hawk/
│   │   └── agent.yaml
│   ├── performance-cop/
│   │   └── agent.yaml
│   ├── friendly-mentor/
│   │   └── agent.yaml
│   └── example/
│       └── agent.yaml
├── reviewer.py                      # Main review loop
├── persona_validator.py             # Agent Spec validator
├── config.yaml                      # Your configuration
└── .reviewer-log/                   # Internal logs

/usr/src/freebsd/                    # Source tree being reviewed
├── .ai-code-reviewer/
│   ├── LESSONS.md                   # FreeBSD-specific lessons
│   ├── REVIEW-SUMMARY.md            # FreeBSD review history
│   └── logs/                        # Session logs
└── [source files...]
```

## Use Cases

### Single Project Review
Most common case - one project, one agent:
```yaml
# config.yaml
source:
  root: "/usr/src/freebsd"
review:
  persona: "personas/freebsd-angry-ai"
```

Lessons go to: `/usr/src/freebsd/.ai-code-reviewer/LESSONS.md`

### Multiple Projects, Same Agent
Review different codebases with the same review style:

**Config for FreeBSD**:
```yaml
source:
  root: "/usr/src/freebsd"
review:
  persona: "personas/security-hawk"
```

**Config for Linux**:
```yaml
source:
  root: "/home/user/linux"
review:
  persona: "personas/security-hawk"  # Same agent!
```

Result:
- Agent configuration is shared (AI behavior)
- Lessons are separate (different codebases)
- Progress tracking is separate

### Multiple Projects, Different Agents
Different review styles for different projects:

```yaml
# Aggressive security for production
source:
  root: "/usr/src/freebsd"
review:
  persona: "personas/security-hawk"
```

```yaml
# Friendly mentor for learning project
source:
  root: "/home/user/my-project"
review:
  persona: "personas/friendly-mentor"
```

## Validating Agents

```bash
# Validate agent configuration
python3 persona_validator.py personas/security-hawk

# Output:
# [OK] Agent validated: Security Hawk
#   Agent Spec version: 26.1.0
#   Description: A paranoid security auditor...
#   Focus areas: input-validation, memory-safety, ...
```

## Creating New Agents

1. Copy existing agent:
   ```bash
   cp -r personas/example personas/my-agent
   ```

2. Edit `personas/my-agent/agent.yaml`

3. Validate:
   ```bash
   python3 persona_validator.py personas/my-agent
   ```

4. Use in config.yaml:
   ```yaml
   review:
     persona: "personas/my-agent"
   ```

## Gitignore Recommendations

### In ai-code-reviewer repo
```gitignore
config.yaml              # User-specific
.reviewer-log/           # Internal logs
```

### In source trees being reviewed
```gitignore
.ai-code-reviewer/logs/  # Build logs (large)

# Keep these committed (if desired)
# .ai-code-reviewer/LESSONS.md       # Team lessons
# .ai-code-reviewer/REVIEW-SUMMARY.md # Review history
```

## Benefits of This Structure

1. **Clean Separation**: Agent configs vs. per-project data
2. **Reusable Agents**: Same review style across projects
3. **Project-Specific Learning**: Lessons stay with the codebase
4. **Multi-Project Support**: Review multiple codebases easily
5. **Standardized Format**: Oracle Agent Spec for interoperability
6. **Easy Sharing**: Agents are self-contained YAML files

## References

- [Oracle Agent Spec](https://oracle.github.io/agent-spec/26.1.0/)
- [README.md](../README.md) - Full documentation
- [AGENTS.md](../AGENTS.md) - AI agent instructions
