# File Location Architecture

## Overview

This document explains where different types of files are stored and why.

## Three Categories of Files

### 1. Persona Files (Templates) - `personas/<name>/`

**Location**: `ai-code-reviewer/personas/freebsd-angry-ai/`  
**Purpose**: Reusable AI behavior templates  
**Git**: Committed to ai-code-reviewer repo  
**Shared**: YES - same persona can review multiple projects

Files:
- `AI_START_HERE.md` - Bootstrap instructions for the AI
- `PERSONA.md` - Personality and behavior rules
- `HANDOVER.md` - Session handoff protocol
- `@AGENTS.md`, `AGENTS.md` - Agent instructions
- `logs/` - Per-session conversation logs

**Why here?**: Personas are templates that define HOW to review, not WHAT was reviewed.

### 2. Source-Specific Files (Per-Project) - `<source-root>/.ai-code-reviewer/`

**Location**: `<your-source-tree>/.ai-code-reviewer/`  
**Purpose**: Per-project review history and lessons  
**Git**: In the source tree being reviewed  
**Shared**: NO - each project has its own

Files:
- `LESSONS.md` - Mistakes learned from THIS codebase
- `REVIEW-SUMMARY.md` - Progress and history for THIS project
- `logs/` - Build logs and detailed output (gitignored)

**Why here?**: 
- Lessons are specific to the codebase (e.g., "FreeBSD uses strtonum, not atoi")
- Review progress is per-project
- Multiple projects can use the same persona but have separate histories
- Named after the tool (not persona-specific)

Example:
```
/usr/src/freebsd/.ai-code-reviewer/LESSONS.md       # FreeBSD-specific lessons
/home/user/linux/.ai-code-reviewer/LESSONS.md       # Linux-specific lessons
```

### 3. Issue Tracking (Per-Project) - `<source-root>/.beads/`

**Location**: `<your-source-tree>/.beads/`  
**Purpose**: Issue tracking database (if beads installed)  
**Git**: Usually gitignored, syncs to separate branch  
**Shared**: NO - each project has its own

**Why here?**: Beads tracks work items for the specific codebase being reviewed.

## Migration from Old Structure

### Before (WRONG)
```
ai-code-reviewer/
├── personas/
│   └── freebsd-angry-ai/
│       ├── AI_START_HERE.md  ✓ (template)
│       ├── LESSONS.md        ✗ (per-project data in template!)
│       └── REVIEW-SUMMARY.md ✗ (per-project data in template!)
```

**Problem**: If you review multiple projects with the same persona, lessons and summaries would get mixed together.

### After (CORRECT)
```
ai-code-reviewer/
├── personas/
│   └── freebsd-angry-ai/
│       ├── AI_START_HERE.md  ✓ (template)
│       ├── PERSONA.md        ✓ (template)
│       └── logs/             ✓ (session logs)

/usr/src/freebsd/              # Source tree being reviewed
├── .ai-code-reviewer/
│   ├── LESSONS.md             ✓ (FreeBSD-specific)
│   └── REVIEW-SUMMARY.md      ✓ (FreeBSD-specific)
└── .beads/                    ✓ (FreeBSD work items)

/home/user/linux/              # Another source tree
├── .ai-code-reviewer/
│   ├── LESSONS.md             ✓ (Linux-specific)
│   └── REVIEW-SUMMARY.md      ✓ (Linux-specific)
└── .beads/                    ✓ (Linux work items)
```

## Use Cases

### Single Project Review
Most common case - one project, one persona:
```yaml
# config.yaml
source:
  root: "/usr/src/freebsd"
review:
  persona: "personas/freebsd-angry-ai"
```

Lessons go to: `/usr/src/freebsd/.ai-code-reviewer/LESSONS.md`

### Multiple Projects, Same Persona
Review different codebases with the same review style:

**Config for FreeBSD**:
```yaml
source:
  root: "/usr/src/freebsd"
review:
  persona: "personas/freebsd-angry-ai"
```

**Config for Linux**:
```yaml
source:
  root: "/home/user/linux"
review:
  persona: "personas/freebsd-angry-ai"  # Same persona!
```

Result:
- Persona templates are shared (AI behavior)
- Lessons are separate (different codebases have different patterns)
- Progress tracking is separate

### Multiple Projects, Different Personas
Different review styles for different projects:

**Aggressive security review for FreeBSD**:
```yaml
source:
  root: "/usr/src/freebsd"
review:
  persona: "personas/security-hawk"
```

**Friendly mentor for your project**:
```yaml
source:
  root: "/home/user/my-project"
review:
  persona: "personas/friendly-mentor"
```

## Gitignore Recommendations

### In ai-code-reviewer repo
```gitignore
# Already configured
config.yaml              # User-specific
personas/*/logs/         # Session logs
.beads/                  # If you review ai-code-reviewer itself
.reviewer-log/           # Internal ops logs
```

### In source trees being reviewed
```gitignore
# Recommended additions
.ai-code-reviewer/logs/  # Build logs (large)
.beads/                  # If not syncing to branch

# Keep these committed (if desired)
# .ai-code-reviewer/LESSONS.md       # Team lessons learned
# .ai-code-reviewer/REVIEW-SUMMARY.md # Review history
```

## Benefits of New Structure

1. **Clean Separation**: Templates vs. per-project data
2. **Reusable Personas**: Same review style across projects
3. **Project-Specific Learning**: Lessons stay with the codebase
4. **Multi-Project Support**: Review multiple codebases easily
5. **Team Collaboration**: Commit lessons to share with team
6. **Source Tree Integrity**: Everything in `.ai-code-reviewer/` is self-contained
7. **Tool-Branded**: Named after the tool, not a specific persona

## Automatic Migration

The system automatically migrates files from legacy locations on first run:

**Legacy locations checked:**
1. `personas/<persona>/LESSONS.md` (oldest)
2. `personas/<persona>/REVIEW-SUMMARY.md` (oldest)
3. `.angry-ai/LESSONS.md` (previous)
4. `.angry-ai/REVIEW-SUMMARY.md` (previous)

**Migration behavior:**
- If file found in legacy location → copies to `.ai-code-reviewer/`
- If file already exists in new location → merges content with separator
- Removes legacy files after successful migration
- Shows migration summary on startup

**You don't need to do anything** - the tool handles migration automatically!
