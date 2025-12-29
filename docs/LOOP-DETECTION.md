# Loop Detection and Recovery

**UPDATED**: Added critical pre-parse loop detection (commit a3597a5)

## Problem

The AI reviewer could get stuck in infinite loops where it:
1. Reads a file and detects an issue (e.g., merge conflict marker)
2. Says it will fix the issue
3. Response gets truncated mid-action
4. Only executes READ_FILE again instead of fixing
5. Repeats indefinitely

Example from real case:
```
STEP 1090: AI reads file, sees ">>>>>>>" marker
AI: "Let's fix this merge conflict. Here'"  [truncated]
ACTION: READ_FILE bin/hostname/hostname.c

STEP 1091: AI reads same file again
AI: "Let's fix this merge conflict. Here'"  [truncated]  
ACTION: READ_FILE bin/hostname/hostname.c

... repeats for 100+ steps ...
```

## Solution

Implemented multi-layered protection (TWO detection layers):

### Critical Fix (a3597a5): Pre-Parse Loop Detection

**THE PROBLEM**: Original detection only ran AFTER successful parsing. If ActionParser failed, loop detection never triggered.

**THE FIX**: Added detection BEFORE parsing:
- Tracks consecutive parse failures
- Detects same unparseable response 5+ times
- Forces recovery with format guidance
- Catches wrong format like `### Action:` instead of `ACTION:`

**Example that was missed**:
```
AI: "### Action: LIST_DIR bin/cpuset"  (wrong format)
Parser: [fails to extract] → returns None
Result: No loop detection, infinite loop
```

**Now catches it**:
```
AI: "### Action: LIST_DIR bin/cpuset"  (5th time)
Pre-parse: "Same unparseable response 5 times!"
Result: Recovery message sent, loop broken
```

### Original Protection Layers:

### 1. Action History Tracking

Added to `ReviewSession`:
- `action_history`: List of recent actions taken
- `last_action_hash`: Hash of last action for comparison
- `consecutive_identical_actions`: Counter for detecting loops

Each action is hashed with its key details:
- `READ_FILE:path/to/file.c`
- `EDIT_FILE:path/to/file.c`
- `SET_SCOPE:directory`

### 2. Loop Detection with Progressive Warnings

**Warning Phase (5 repetitions)**:
- Detects when same action repeated 5+ times
- Provides specific guidance on how to break the loop
- Special handling for READ_FILE loops (most common case)

**Recovery Phase (10 repetitions)**:
- Automatically triggers if AI ignores warnings
- Forces system intervention to break the loop

### 3. Automatic Recovery

When loop threshold reached, system automatically:
1. **Reverts uncommitted changes** - rolls back any broken edits
2. **Clears file state** - abandons chunked file if stuck
3. **Force-skips stuck file** - prevents re-reading same file
4. **Resets counters** - allows fresh start
5. **Provides mandatory next steps** - restricts AI to safe actions only

### 4. Response Validation

Detects truncated/incomplete responses before execution:
- Response too short (< 50 chars)
- Ends mid-word (`Here'`, `Let'`)
- Mismatched delimiters (`<<<` without `>>>`)
- `EDIT_FILE` without `OLD:` or `NEW:` blocks

If detected, asks AI to provide complete response.

## Configuration

Key parameters in `_check_for_loop()`:
```python
MAX_CONSECUTIVE_WARNING = 5   # Warn at 5 repetitions
MAX_CONSECUTIVE_RECOVERY = 10  # Force recovery at 10
```

Adjust these if you want to be more/less aggressive with intervention.

## How It Works

### Normal Operation
```
Step N:   ACTION: READ_FILE foo.c
Step N+1: ACTION: EDIT_FILE foo.c  [different action, counter resets]
```

### Loop Warning
```
Step N:   ACTION: READ_FILE foo.c  [count: 1]
Step N+1: ACTION: READ_FILE foo.c  [count: 2]
Step N+2: ACTION: READ_FILE foo.c  [count: 3]
Step N+3: ACTION: READ_FILE foo.c  [count: 4]
Step N+4: ACTION: READ_FILE foo.c  [count: 5]
>>> WARNING MESSAGE SENT TO AI <<<
```

### Automatic Recovery
```
Step N+9: ACTION: READ_FILE foo.c  [count: 10]
>>> AUTOMATIC RECOVERY TRIGGERED <<<
- Reverted changes
- Cleared file state
- Reset counters
>>> AI forced to choose different action <<<
```

## Benefits

1. **Prevents infinite loops** - No more running for hours making no progress
2. **Saves compute resources** - Stops wasted LLM calls
3. **Protects work** - Reverts broken changes before they accumulate
4. **Self-healing** - System can recover without human intervention
5. **Transparent** - Clear logging shows when/why recovery triggered

## Example Output

When loop detected:
```
⚠️  INFINITE LOOP WARNING (attempt 7/10) ⚠️
======================================================================

You have READ the same file 7 times in a row:
  bin/hostname/hostname.c

This suggests you are:
1. Detecting a problem in the file
2. Saying you'll fix it
3. But then just reading it again instead of fixing it

BREAKING THE LOOP:

If there's a merge conflict or error in the file:
  ACTION: EDIT_FILE bin/hostname/hostname.c
  OLD:
  <<<
  [copy the EXACT problematic section including context]
  >>>
  NEW:
  <<<
  [corrected version]
  >>>

WARNING: If you repeat this action 3 more times,
automatic recovery will be triggered and progress will be lost.
======================================================================
```

When recovery triggered:
```
⚠️  AUTOMATIC RECOVERY INITIATED
======================================================================
Step 1: Reverting uncommitted changes...
    Reverted: bin/hostname/hostname.c
Step 2: Abandoning chunked file: bin/hostname/hostname.c
Step 3: Force-skipping stuck file: bin/hostname/hostname.c
Step 4: Resetting loop detection counters
======================================================================
✓ RECOVERY COMPLETE - You must now take a different approach
======================================================================

🔄 AUTOMATIC RECOVERY COMPLETED

You were stuck in an infinite loop. The system has automatically:
  - Reverted all uncommitted changes
  - Abandoned chunked file: bin/hostname/hostname.c
  - Force-skipped file: bin/hostname/hostname.c

MANDATORY NEXT STEPS:

You MUST choose ONE of these actions (no other action will be accepted):

1. Move to a DIFFERENT file in the same directory
2. Skip to the next directory  
3. List files in current directory
4. If completely stuck, halt

DO NOT attempt to read the same file again.
======================================================================
```

## Related Files

- `reviewer.py`: Main implementation
  - `ReviewSession`: Added tracking fields
  - `_get_action_hash()`: Action hashing
  - `_check_for_loop()`: Detection logic
  - `_recover_from_loop()`: Recovery mechanism
  - `_validate_response()`: Response validation

## Testing

To test loop detection manually:
1. Run reviewer on a file with an issue
2. Manually make the AI repeat READ_FILE multiple times
3. Observe warning at 5 repetitions
4. Continue to 10 to trigger recovery

## Future Improvements

Potential enhancements:
- [ ] Machine learning to detect loop patterns earlier
- [ ] Per-action-type thresholds (some actions safe to repeat more)
- [ ] Persistent loop tracking across sessions
- [ ] Metrics/dashboard for loop frequency
- [ ] Integration with issue tracker (bd) to file bugs about loops


