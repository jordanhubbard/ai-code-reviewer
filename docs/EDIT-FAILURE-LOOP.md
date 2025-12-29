# Edit Failure Loop Detection

**Added**: commit 33e45d6 (third loop detection layer)

## The Problem

AI gets stuck in an **alternating loop** that bypasses identical-action detection:

```
Step 30: EDIT_FILE bin/cpuset/cpuset.c
Result: EDIT_FILE_ERROR: OLD text not found

Step 31: READ_FILE bin/cpuset/cpuset.c
AI: "Let's re-read and identify the exact text"

Step 32: EDIT_FILE bin/cpuset/cpuset.c  
Result: EDIT_FILE_ERROR: OLD text not found (again)

Step 34: READ_FILE bin/cpuset/cpuset.c
AI: "Let's re-read..."

... continues for 20+ cycles ...
```

### Why Previous Detection Failed

**Consecutive action detection** only triggers when **same action** repeats:
- `READ_FILE` → `READ_FILE` → `READ_FILE` ✅ (caught)
- `EDIT_FILE` → `READ_FILE` → `EDIT_FILE` ❌ (not caught - alternating!)

Each action type counter resets when a different action occurs, so:
- `consecutive_identical_actions` for EDIT_FILE = 1
- `consecutive_identical_actions` for READ_FILE = 1  
- Never reaches threshold of 5 or 10!

## The Solution

**Track edit failures separately** - independent of action sequencing:
- Counter increments on each EDIT_FILE failure for **same file**
- Counter resets on success or different file
- Triggers after **3 failures** (lower threshold since this is expensive)

## Implementation

### New Session Fields

```python
# Edit failure loop detection
edit_failure_count: int = 0  # Consecutive EDIT_FILE failures
last_failed_edit_file: Optional[str] = None
```

### Detection Logic

In `_execute_action()` for EDIT_FILE:

```python
if success:
    # Reset on success
    self.session.edit_failure_count = 0
    self.session.last_failed_edit_file = None
else:
    # Track failures
    if rel_path == self.session.last_failed_edit_file:
        self.session.edit_failure_count += 1
    else:
        self.session.edit_failure_count = 1
        self.session.last_failed_edit_file = rel_path
    
    # Trigger at 3 failures
    if self.session.edit_failure_count >= 3:
        return [detailed recovery message]
```

## When It Triggers

**After 3rd consecutive failure** on same file:

```
⚠️  EDIT FAILURE LOOP DETECTED ⚠️

EDIT_FILE has failed 3 times on: bin/cpuset/cpuset.c
Error: OLD text not found in file

This usually means:
1. The file content doesn't match what you expect
2. You're trying to edit code that's already been changed
3. The OLD block has whitespace/tab mismatches
4. The file is too complex to edit reliably

BREAKING THE LOOP - Choose ONE:

A) Skip this file and move on:
   ACTION: SKIP_FILE

B) Move to a different file:
   ACTION: READ_FILE <different-file-in-directory>

C) If directory is problematic, move to next:
   ACTION: SET_SCOPE <different-directory>

D) If you have other changes ready, build them:
   ACTION: BUILD

DO NOT:
- Read the same file again (you've read it 3 times)
- Try to edit it again without a different approach
- Hallucinate code that doesn't exist in the file

The file may already be correct, or too complex for automated editing.
MOVE ON to make progress.
```

## Why 3 Failures (Not 5 or 10)?

1. **Expensive** - Each cycle includes:
   - Large context read (150KB+)
   - 50+ second AI response time
   - No progress made

2. **Clear failure** - If OLD text doesn't match after 3 tries with file reads, it won't match on try 4

3. **Faster recovery** - Gets AI unstuck in ~3 minutes instead of 10+ minutes

## Three-Layer Protection

We now have **THREE independent loop detectors**:

### Layer 1: Consecutive Identical Actions
- **Detects**: Same action repeated 5-10 times
- **Example**: `READ_FILE` 10 times in a row
- **Trigger**: 5 warnings, 10 recovery

### Layer 2: Unparseable Response Loop  
- **Detects**: Same unparseable response 5+ times
- **Example**: `### Action: LIST_DIR` (wrong format) 5 times
- **Trigger**: 5 repetitions

### Layer 3: Edit Failure Loop (NEW)
- **Detects**: EDIT_FILE fails 3+ times on same file
- **Example**: EDIT fails → READ → EDIT fails → READ (alternating)
- **Trigger**: 3 failures

All three run independently and catch different failure modes!

## Real-World Example

**bin/cpuset case (steps 30-51)**:

Without detection:
```
Step 30-51: 21 cycles of EDIT → READ → EDIT
Context: 160KB → 172KB (growing)
Time: ~20 minutes wasted
```

With detection:
```
Step 30: EDIT fails (1st)
Step 32: EDIT fails (2nd)  
Step 36: EDIT fails (3rd) → RECOVERY MESSAGE SENT
AI: Forced to choose different action
Loop: BROKEN ✅
```

## Testing

To verify:
1. Run reviewer on complex file
2. Let AI try to edit something that doesn't exist
3. Should trigger after 3rd failure
4. AI should move on instead of looping

## Related Files

- `reviewer.py`: Implementation
- `docs/LOOP-DETECTION.md`: Original detection layers
- `docs/EMERGENCY-LOOP-FIX.md`: Pre-parse detection

## Commit

**33e45d6** - CRITICAL: Add edit failure loop detection

