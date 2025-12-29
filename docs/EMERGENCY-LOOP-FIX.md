# EMERGENCY: Infinite Loop Fix

## Immediate Problem (bin/cpuset case)

**Steps 1490-1599**: AI stuck repeating `### Action: LIST_DIR bin/cpuset` over 100 times.

### Root Cause

The original loop detection code I implemented had a **CRITICAL FLAW**:
- Loop detection only ran in `_execute_action()`
- But `_execute_action()` is only called AFTER successful action parsing
- AI was using wrong format: `### Action: LIST_DIR` (markdown)
- Parser couldn't extract it, so returned `None`
- Loop detection never ran → infinite loop

### The Fix (commit a3597a5)

**Three-layer protection added:**

1. **Flexible Parser** - Now accepts both formats:
   - `ACTION: LIST_DIR` (correct)
   - `### Action: LIST_DIR` (markdown variant)

2. **Pre-Parse Loop Detection** - Catches loops BEFORE parsing:
   - Tracks consecutive parse failures
   - Detects same unparseable response repeated 5+ times
   - Forces recovery with clear format guidance

3. **Post-Parse Loop Detection** - Original detection still works:
   - Catches loops after successful parsing
   - Handles READ_FILE, EDIT_FILE, etc. loops

## How to Recover Right Now

### If Reviewer is Currently Stuck:

1. **Stop it**: Press `Ctrl+C` (you already did this)

2. **Pull latest changes**:
   ```bash
   cd /path/to/ai-code-reviewer
   git pull
   ```

3. **Restart the reviewer**:
   ```bash
   make run
   ```

The new code will now catch this loop pattern immediately.

### What Happens with New Code:

**Step 5** (after same response 5 times):
```
⚠️  CRITICAL: UNPARSEABLE LOOP DETECTED ⚠️

You have provided the same response 5 times,
but the system cannot parse any valid ACTION from it.

Your response: "Let's proceed with listing the contents of the `bin/cpuset` directory..."

The problem is likely:
1. Using wrong format like '### Action:' instead of 'ACTION:'
2. Response is truncated mid-action
3. Action keyword is misspelled

CORRECT FORMAT:
  ACTION: READ_FILE path/to/file
  ACTION: EDIT_FILE path/to/file
  ACTION: LIST_DIR path/to/dir
  ACTION: SET_SCOPE directory
  ACTION: BUILD
  ACTION: HALT

Provide ONE valid action now using the correct format.
```

## Technical Details

### Changes Made

**File**: `reviewer.py`

**1. ActionParser - More Flexible**
```python
# Before:
ACTION_RE = re.compile(r'^ACTION:\s*([A-Z_]+)(.*)$', re.MULTILINE)

# After:
ACTION_RE = re.compile(r'^(?:###\s+)?ACTION:\s*([A-Z_]+)(.*)$', 
                       re.MULTILINE | re.IGNORECASE)
```

**2. ReviewSession - New Fields**
```python
consecutive_parse_failures: int = 0
last_failed_response: str = ""
```

**3. Main Loop - Pre-Parse Detection**
```python
# Added before "if not action:" handler
if not action:
    # Track consecutive parse failures
    if response.strip() == self.session.last_failed_response.strip():
        self.session.consecutive_parse_failures += 1
    else:
        self.session.consecutive_parse_failures = 1
        self.session.last_failed_response = response.strip()
    
    # Force recovery at 5 failures
    if self.session.consecutive_parse_failures >= 5:
        [send recovery message and continue]
```

## Why Original Detection Didn't Trigger

**Flow with Old Code:**
```
1. AI responds: "### Action: LIST_DIR bin/cpuset"
2. ActionParser.parse() → returns None (can't match pattern)
3. action = None
4. Hit "if not action:" block
5. Send "No valid ACTION found" feedback
6. Loop back to step 1
7. Loop detection in _execute_action() NEVER RUNS
```

**Flow with New Code:**
```
1. AI responds: "### Action: LIST_DIR bin/cpuset"
2. ActionParser.parse() → returns action dict (flexible regex)
3. action = {'action': 'LIST_DIR', ...}
4. _execute_action() called
5. Loop detection runs → warns at 5, recovers at 10
```

**OR if parser still fails:**
```
1. AI responds with unparseable text
2. ActionParser.parse() → returns None
3. action = None
4. NEW: Pre-parse loop detection checks
5. If same response 5 times → send recovery message
6. Break loop before wasting more resources
```

## Commits

- **bf7b477** - Original loop detection (only works after parsing)
- **a3597a5** - CRITICAL FIX (works even when parsing fails)

## Testing

To verify the fix works:
1. Pull latest code
2. Run reviewer on problematic directory
3. Should see loop detected within 5-10 steps
4. Should not run for 100+ steps like before

## Prevention

The new code prevents this specific failure mode:
- ✅ Handles wrong action format
- ✅ Catches loops before and after parsing
- ✅ Provides clear format guidance
- ✅ Forces recovery automatically

## Status

- **Original Issue**: Loop detection only worked after successful parsing
- **Fix Status**: DEPLOYED (commit a3597a5)
- **Testing**: Needs verification on next run
- **Risk**: Low - multiple safety layers now in place

