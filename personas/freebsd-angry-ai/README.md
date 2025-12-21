# FreeBSD Angry AI Persona

**"The FreeBSD Commit Blocker"** - Ruthless security auditor persona

## What is a Persona?

A persona defines the AI's:
- **Behavior**: How it reviews code (aggressive, friendly, security-focused, etc.)
- **Knowledge**: Lessons learned from previous reviews
- **Memory**: Progress tracking and review history

## This Persona

**Character**: Unforgiving senior FreeBSD committer who blocks bad commits

**Focus**:
- Security vulnerabilities
- style(9) compliance
- POSIX correctness
- Buffer overflows, TOCTOU races, integer overflows
- Proper error handling

**Tone**: Professional but ruthless - "If it looks wrong, it IS wrong"

## Files in This Persona

### Core Files

- **AI_START_HERE.md**: Bootstrap instructions the AI sees first
- **PERSONA.md**: Full personality definition and review standards
- **HANDOVER.md**: Workflow protocol and handover instructions

### Learning & Memory

- **LESSONS.md**: Bug patterns discovered during reviews
  - Updated automatically when build fails
  - AI learns from mistakes

- **REVIEW-SUMMARY.md**: Progress tracking
  - Directories completed
  - Files fixed
  - Bugs found
  - Updated after each successful build

### Agent Hints

- **AGENTS.md**: Instructions for AI agents
- **@AGENTS.md**: Quick reference card

### Logs

- **logs/**: Conversation logs for each review session
  - One file per step
  - Includes prompts and responses

## Using This Persona

In `config.yaml`:
```yaml
review:
  persona: "personas/freebsd-angry-ai"
```

## Creating Your Own Persona

```bash
# Copy this persona as a template
cp -r personas/freebsd-angry-ai personas/my-persona

# Edit the core files
vim personas/my-persona/AI_START_HERE.md  # Bootstrap
vim personas/my-persona/PERSONA.md        # Personality

# Update config
vim config.yaml  # persona: "personas/my-persona"
```

### Persona Ideas

- **security-hawk**: Paranoid security auditor
- **performance-cop**: Obsessed with speed/efficiency
- **friendly-helper**: Encouraging mentor who teaches
- **refactor-bot**: Focuses on code quality/maintainability
- **test-enforcer**: Demands tests for everything

## Why Personas Live Here

**Keeps source tree clean!**

Without personas:
```
freebsd-src/
├── REVIEW-SUMMARY.md      ← Pollutes source
├── AI_START_HERE.md       ← Pollutes source  
├── .angry-ai/logs/        ← Pollutes source
└── bin/chmod/chmod.c
```

With personas:
```
freebsd-src/
└── bin/chmod/chmod.c      ← Only code changes!

angry-ai/personas/freebsd-angry-ai/
├── REVIEW-SUMMARY.md      ← Agent data
├── AI_START_HERE.md       ← Agent data
└── logs/                   ← Agent data
```

**Benefit**: Source tree git history shows ONLY code changes, not AI review metadata!

## Switching Personas

You can have multiple personas and switch between them:

```yaml
# config.yaml

# Angry auditor for security review
persona: "personas/freebsd-angry-ai"

# Friendly helper for mentoring
# persona: "personas/friendly-mentor"

# Performance focused for optimization
# persona: "personas/performance-hawk"
```

Each persona maintains its own:
- Learned lessons
- Progress tracking
- Conversation logs

## Sharing Personas

Personas are portable! Share them:

```bash
# Export
tar czf freebsd-angry-ai.tar.gz personas/freebsd-angry-ai/

# Import (in someone else's setup)
tar xzf freebsd-angry-ai.tar.gz -C angry-ai/personas/
```

Or version control them separately:

```bash
cd personas/freebsd-angry-ai
git init
git remote add origin https://github.com/you/freebsd-angry-persona.git
```

## Credits

Created by: AI reviewers running on the FreeBSD source tree
Purpose: Ruthless security audit of entire FreeBSD codebase
Success: Found and fixed buffer overflows, TOCTOU races, integer overflows

