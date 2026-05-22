# AI Code Reviewer - Agent Instructions

This repository is an AI-powered code review and rewrite tool. The core design
is data-driven:

- Agent behavior lives in `personas/<agent-name>/agent.yaml` using Oracle Agent
  Spec format.
- Workflow behavior is configured in `config.yaml`.
- Build validation is configured with `source.build_command`.
- Rewrite acceptance belongs in `review.rewrite.contract`, not hard-coded
  language, OS, or build-tool assumptions in the runner.

## Quick Start

Validate an agent:

```bash
python3 persona_validator.py personas/security-hawk
```

Run the reviewer:

```bash
cp config.yaml.sample config.yaml
# Edit config.yaml: set source.root, source.build_command, review.workflow, review.persona
python3 reviewer.py --config config.yaml
```

Run forever mode:

```bash
python3 reviewer.py --config config.yaml --forever
```

## Available Agents

Load agent configuration from `personas/<agent-name>/agent.yaml`.

| Agent | Focus | Use When |
|-------|-------|----------|
| `freebsd-angry-ai` | Security, style(9), POSIX | FreeBSD-oriented production audits |
| `freebsd-rust-rewriter` | C/C++ to Rust, FreeBSD build integration | FreeBSD-specific rewrite persona |
| `security-hawk` | Vulnerabilities, exploits | Security audits and penetration-test style review |
| `performance-cop` | Speed, algorithms, cache | Performance optimization |
| `friendly-mentor` | Learning, best practices | Training, onboarding, open source |
| `example` | Balanced, educational | General code review |

## Workflow Modes

`review.workflow` controls runner protocol and metadata files:

- `review` uses `REVIEW-INDEX.md` and `REVIEW-SUMMARY.md` for audits and fixes.
- `rewrite` uses `REWRITE-INDEX.md` and `REWRITE-SUMMARY.md` for scoped source
  transformations such as refactors, API migrations, decomposition,
  simplification, hardening rewrites, or translations.

Personas provide project-specific taste and instructions. Do not add
language-specific rewrite policy to `reviewer.py` when the requirement can live
in the persona or `review.rewrite.contract`.

## Project Structure

```text
ai-code-reviewer/
├── personas/                 # Agent configurations
├── reviewer.py               # Main review/rewrite loop
├── persona_validator.py      # Validates Agent Spec format
├── config.yaml.sample        # Configuration template
├── tests/                    # Unit tests
└── AGENTS.md                 # This file
```

## Issue Tracking with bd

This project may use `bd` (beads) for issue tracking, but local checkouts can
have a missing, locked, or incompatible beads database. Use bd when it works;
do not block unrelated code work on broken local issue-tracker state.

Current `bd` command notes:

- There is no `bd sync` command in the installed CLI. Do not use it.
- Use `bd ready --json` to inspect ready work.
- Use `bd ready --claim --json` or `bd update <id> --claim --json` to claim work.
- Use `bd close <id> --reason "Completed" --json` to close finished work.
- If Dolt-backed issue sync is configured and healthy, use `bd dolt pull` and
  `bd dolt push`. If those fail due to local database errors, report that
  clearly and continue with normal git handoff.

Useful commands:

```bash
bd ready --json
bd show <id> --json
bd update <id> --claim --json
bd create "Issue title" --description "Details" -t bug|feature|task -p 0-4 --json
bd close <id> --reason "Completed" --json
bd dolt pull
bd dolt push
```

## Quality Gates

Before committing code changes, run the focused tests needed for the change. For
general runner changes, run:

```bash
python3 -m unittest discover -s tests -p "test_*.py"
python3 persona_validator.py personas/freebsd-angry-ai
python3 persona_validator.py personas/freebsd-rust-rewriter
```

For documentation-only changes, tests are optional; still run `git diff --check`.

## Session Completion

Work is complete when the relevant changes are committed and pushed.

Recommended final workflow:

```bash
git status --short --branch
git diff --check
# Run relevant tests/validators for code changes.
git pull --rebase
# Optional only if bd/Dolt is configured and healthy:
#   bd dolt pull
#   bd dolt push
git push
git status
```

Final `git status` should show a clean worktree and the branch up to date with
its remote. If issue tracking could not be updated because `bd` is unavailable
or broken locally, mention that in the handoff.

## Creating New Agents

1. Copy an existing agent:
   ```bash
   cp -r personas/example personas/my-agent
   ```

2. Edit `personas/my-agent/agent.yaml`:
   - Set `name` and `description`
   - Define `system_prompt`
   - Configure `inputs`, `outputs`, and `metadata`

3. Validate:
   ```bash
   python3 persona_validator.py personas/my-agent
   ```

4. Use it in `config.yaml`:
   ```yaml
   review:
     persona: "personas/my-agent"
   ```

## References

- [Oracle Agent Spec](https://oracle.github.io/agent-spec/26.1.0/)
- [README.md](README.md)
- [SETUP_GUIDE.md](SETUP_GUIDE.md)
