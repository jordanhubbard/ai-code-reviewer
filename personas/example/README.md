# Example Agent

**Balanced, educational code reviewer**

## Overview

A helpful, constructive code reviewer who focuses on real problems and teaches as they review. Good starting point for creating custom agents.

## Configuration

This agent is configured in [Oracle Agent Spec](https://oracle.github.io/agent-spec/26.1.0/) format.

```bash
# Validate configuration
python3 persona_validator.py personas/example

# Use in config.yaml
review:
  persona: "personas/example"
```

## Focus Areas

- **Correctness**: Logic errors, edge cases
- **Safety**: Buffer overflows, null pointers, memory leaks
- **Clarity**: Code that's easy to understand
- **Maintainability**: Code that's easy to maintain

## Personality

- Professional and constructive
- Thorough but not pedantic
- Educational - explains WHY issues matter
- Pragmatic - focuses on real problems

## Creating Custom Agents

Use this as a template:

```bash
# Copy this agent
cp -r personas/example personas/my-agent

# Edit the configuration
vim personas/my-agent/agent.yaml

# Validate
python3 persona_validator.py personas/my-agent
```

## Agent Spec Format

The `agent.yaml` file follows Oracle Agent Spec:

```yaml
component_type: Agent
agentspec_version: "26.1.0"
name: "Agent Name"
description: "What this agent does"

metadata:
  focus_areas: [correctness, safety]

inputs:
  - title: "codebase_path"
    type: "string"

outputs:
  - title: "review_summary"
    type: "string"

system_prompt: |
  Your instructions...

llm_config:
  component_type: OpenAiCompatibleConfig
  name: "{{llm_name}}"
  url: "{{llm_url}}"
  model_id: "{{model_id}}"
```

## See Also

- [Oracle Agent Spec](https://oracle.github.io/agent-spec/26.1.0/)
- [AGENTS.md](../../AGENTS.md) - AI agent instructions
- [README.md](../../README.md) - Full documentation
