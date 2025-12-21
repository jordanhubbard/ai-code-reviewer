# Handover Protocol

Instructions for continuing a review session.

## Session State

- Current directory: (tracked in REVIEW-SUMMARY.md)
- Files reviewed: (tracked in REVIEW-SUMMARY.md)
- Pending changes: (check git status)

## Resuming a Session

1. Read REVIEW-SUMMARY.md to see progress
2. Read LESSONS.md to see what patterns you've learned
3. Check `git status` to see if there are uncommitted changes
4. Continue from where you left off

## Workflow

```
For each directory:
  For each file in directory:
    1. READ_FILE
    2. Review thoroughly
    3. EDIT_FILE if issues found
    4. BUILD to verify
    5. If build fails: fix issues
    6. If build succeeds: UPDATE_SUMMARY
    7. Continue to next file
  
  When directory complete:
    1. Commit changes: git commit -m "[REVIEW] ..."
    2. NEXT_DIRECTORY
```

## Build/Commit Cycle

**Per directory** (not per file):
- Review multiple files
- Accumulate fixes
- BUILD once to verify all changes
- If build succeeds: commit entire directory
- If build fails: iterate until fixed

This is efficient and keeps git history clean.

## Important Notes

- Build command is in config.yaml (project-specific)
- Persona files stay in personas/ directory
- Source tree only gets code changes (no agent files)
- Progress tracking is automatic

## Creating Multiple Personas

You can run different personas on the same codebase:
```yaml
# Security audit
persona: "personas/security-hawk"

# Performance review  
persona: "personas/performance-cop"

# Beginner education
persona: "personas/friendly-mentor"
```

Each persona maintains separate:
- Progress tracking (REVIEW-SUMMARY.md)
- Learned lessons (LESSONS.md)
- Conversation logs (logs/)

