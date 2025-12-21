# Example Persona

**"Helpful Code Reviewer"** - A balanced, educational persona

## What's in This Persona

This is a template showing the minimal required files:

- **AI_START_HERE.md**: Bootstrap instructions (REQUIRED)
- **PERSONA.md**: Personality definition
- **LESSONS.md**: Learned bug patterns (auto-updated)
- **HANDOVER.md**: Session continuation protocol
- **REVIEW-SUMMARY.md**: Progress tracking (auto-updated)

## Using This Persona

```yaml
# config.yaml
review:
  persona: "personas/example"
```

## Customizing

1. Copy this directory:
   ```bash
   cp -r personas/example personas/my-persona
   ```

2. Edit the files to match your needs:
   - **AI_START_HERE.md**: Define review focus and standards
   - **PERSONA.md**: Set tone and philosophy
   - **HANDOVER.md**: Customize workflow if needed

3. Update config:
   ```yaml
   review:
     persona: "personas/my-persona"
   ```

## Persona Ideas

**Security-Focused**:
- Paranoid about all inputs
- Checks for memory safety issues
- Validates crypto usage
- Looks for race conditions

**Performance-Oriented**:
- Spots O(n²) algorithms
- Identifies unnecessary allocations
- Suggests caching opportunities
- Reviews critical path code

**Beginner-Friendly**:
- Extra educational explanations
- Gentle suggestions
- Links to documentation
- Encourages good habits

**Style-Enforcer**:
- Strict formatting rules
- Naming conventions
- Comment requirements
- Project-specific patterns

## Example: Security Hawk

```markdown
# AI_START_HERE.md

You are a paranoid security auditor. TRUST NOTHING.

## Your Mission

Find security vulnerabilities:
- Buffer overflows
- Integer overflows
- TOCTOU races
- Injection attacks
- Cryptographic mistakes

## Your Personality

Assume all input is hostile. Assume all programmers make mistakes.
Better to be safe than sorry.

"If it CAN fail, it WILL fail in production."
```

## File Purposes

| File | Purpose | Updated By |
|------|---------|-----------|
| AI_START_HERE.md | AI bootstrap instructions | Manual |
| PERSONA.md | Personality definition | Manual |
| LESSONS.md | Learned bug patterns | AI (auto) |
| HANDOVER.md | Workflow protocol | Manual |
| REVIEW-SUMMARY.md | Progress tracking | AI (auto) |
| logs/ | Conversation logs | AI (auto) |

## Tips

1. **Start simple**: Use this example, modify gradually
2. **Be specific**: Clear instructions = better reviews
3. **Give examples**: Show the AI what you want
4. **Iterate**: Run on a small codebase first, refine

## Sharing Personas

Personas are portable! Share with:
```bash
tar czf my-persona.tar.gz personas/my-persona/
```

Or version control separately:
```bash
cd personas/my-persona
git init
git remote add origin https://github.com/you/my-persona.git
```

