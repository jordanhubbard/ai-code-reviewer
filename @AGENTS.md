# AI Code Reviewer

**AI-powered code review tool with configurable agents.**

## For AI Agents

Your configuration is in `personas/<name>/agent.yaml` (Oracle Agent Spec format).

### Quick Commands

```bash
# Validate your agent config
python3 persona_validator.py personas/security-hawk

# Run tests
python3 -m unittest discover -s tests -p "test_*.py"

# Run review
python3 reviewer.py --config config.yaml
```

### Available Agents

- `freebsd-angry-ai` - Ruthless security auditor
- `security-hawk` - Paranoid vulnerability hunter  
- `performance-cop` - Speed optimization expert
- `friendly-mentor` - Educational, encouraging
- `example` - Balanced template

### Session Rules

1. Work is NOT complete until `git push` succeeds
2. Run tests before committing
3. Use `bd` for issue tracking: `bd ready`, `bd close <id>`

See [AGENTS.md](AGENTS.md) for full instructions.
