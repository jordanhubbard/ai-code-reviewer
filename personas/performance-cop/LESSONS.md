# Lessons Learned

This file automatically records bug patterns discovered during reviews.

## Format

Each lesson should include:
- What was found
- Why it was wrong
- How to fix it
- How to prevent it

The AI will add entries here when it discovers issues and successfully fixes them.

## Example Lesson

**Date**: 2024-12-20

**Issue**: Unchecked malloc() return value

**Location**: src/parser.c:45

**Problem**:
```c
char *buffer = malloc(size);
strcpy(buffer, input);  // Segfault if malloc failed!
```

**Fix**:
```c
char *buffer = malloc(size);
if (buffer == NULL) {
    return -ENOMEM;
}
strcpy(buffer, input);
```

**Lesson**: Always check malloc() return value. NULL dereference causes immediate crash.

**Prevention**: Search for `malloc(` and verify every call is checked.

---

*The AI will add new lessons below as it discovers patterns...*

