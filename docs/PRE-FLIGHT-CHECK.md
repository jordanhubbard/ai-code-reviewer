# Pre-Flight Build Sanity Check

## Overview

The ai-code-reviewer now includes a **pre-flight sanity check** that runs before starting any review work. This safety feature prevents the AI from making a bad situation worse by first verifying that the source code actually builds.

## Problem This Solves

In previous runs, the AI might have:
- Introduced breaking changes that weren't caught by the build
- Left the source in a non-building state due to errors or edge cases
- Created subtle bugs that only manifest under certain build configurations

Without a pre-flight check, a new worker starting up would:
1. Load the broken source
2. Pick up where the previous AI left off
3. Make more changes to already-broken code
4. Compound the problem further

## How It Works

### Phase 1: Initial Build Test

When `reviewer.py` starts, it:

1. Checks for uncommitted changes (aborts if found)
2. Runs the configured build command
3. Evaluates the result:
   - ✅ **Build succeeds** → Proceed with normal workflow
   - ❌ **Build fails** → Enter recovery mode

### Phase 2: Automatic Recovery (if build fails)

If the initial build fails:

1. **Identify the problem:**
   ```
   ✗ PRE-FLIGHT CHECK FAILED
   Build failed with 5 errors
   This suggests previous AI review runs introduced breaking changes.
   ```

2. **Revert commits one by one:**
   - Gets commit info: `git log -1 --oneline HEAD`
   - Reverts it: `git revert --no-edit HEAD`
   - Tests build again
   - Repeats until build succeeds (up to 10 commits)

3. **Report recovery:**
   ```
   ✓ BUILD RECOVERED
   Reverted 3 commit(s):
     1. abc1234 bin/chmod: Fix integer parsing
     2. def5678 bin/cat: Add error checking
     3. ghi9012 bin/ls: Security improvements
   
   Source now builds successfully in 450.2s
   Proceeding with review workflow from this point...
   ```

4. **Continue normal workflow** from the now-working state

### Phase 3: Failure Handling

If 10 commits are reverted and the source still doesn't build:

```
✗ RECOVERY FAILED
Reverted 10 commits but source still doesn't build.
Manual intervention required.

To restore original state:
  git reset --hard abc1234
```

The program exits with error code 1, requiring human intervention.

## Usage

### Default Behavior (Recommended)

Pre-flight check runs automatically:

```bash
make run
# or
python3 reviewer.py
```

Output:
```
======================================================================
PRE-FLIGHT SANITY CHECK
======================================================================
Testing if source builds with configured build command...
Command: sudo make -j$(sysctl -n hw.ncpu) buildworld
======================================================================

[... build output ...]

======================================================================
✓ PRE-FLIGHT CHECK PASSED
======================================================================
Source builds successfully in 450.2s
Warnings: 23
Proceeding with review workflow...
======================================================================
```

### Skip Pre-Flight Check (Not Recommended)

For debugging or special cases:

```bash
python3 reviewer.py --skip-preflight
```

Output:
```
⚠️  WARNING: Pre-flight check skipped!
   If source doesn't build, AI may make things worse.
```

Use this ONLY when:
- You're debugging the pre-flight check itself
- You know the source is broken for unrelated reasons
- You want to test other parts of the system

## Configuration

The pre-flight check uses settings from `config.yaml`:

```yaml
source:
  root: ".."
  build_command: "sudo make -j$(sysctl -n hw.ncpu) buildworld"
  build_timeout: 7200  # 2 hours
```

### Maximum Reverts

Currently hardcoded to 10 commits. To change:

```python
if not preflight_sanity_check(builder, source_root, git_helper, max_reverts=20):
    # ...
```

Future enhancement: Make this configurable in `config.yaml`.

## Integration with Workflow

The pre-flight check is a **preamble** to the existing workflow:

```
┌─────────────────────────────────────┐
│ START: reviewer.py                  │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│ Pre-Flight Sanity Check             │
│ ├─ Test build                       │
│ ├─ Revert if needed                 │
│ └─ Verify recovery                  │
└──────────────┬──────────────────────┘
               │
               ├─ SUCCESS ──────────┐
               │                    │
               └─ FAILED            │
                  (exit 1)          ▼
                           ┌─────────────────────────────────┐
                           │ Existing Review Loop (unchanged)│
                           │ ├─ Load bootstrap              │
                           │ ├─ Generate index              │
                           │ ├─ Review directories          │
                           │ ├─ Make edits                  │
                           │ ├─ Run builds                  │
                           │ └─ Commit changes              │
                           └─────────────────────────────────┘
```

**Key Points:**
- Existing workflow is **completely unchanged**
- Pre-flight check is a pure **prefix** to the workflow
- No changes to review logic, chunking, or AI interactions
- If pre-flight passes, behavior is identical to before

## Error Scenarios

### Scenario 1: Uncommitted Changes

```
WARNING: Uncommitted changes detected:
 M reviewer.py
 M config.yaml

Cannot run pre-flight check with uncommitted changes.
Please commit or stash changes first.
```

**Solution:** Commit or stash your changes.

### Scenario 2: Git Revert Fails

```
Reverting: abc1234 bin/chmod: Fix integer parsing
ERROR: Git revert failed: error: could not revert abc1234...
Manual intervention required.
```

**Common causes:**
- Merge conflicts during revert
- Commit doesn't have a parent (orphan commit)
- Repository corruption

**Solution:** Fix git state manually, then re-run.

### Scenario 3: Build System Error

```
✗ PRE-FLIGHT CHECK ERROR: Command timed out after 7200s
Cannot verify build status. Proceeding with caution...
```

**Common causes:**
- Build timeout too short for your system
- Build command incorrect
- System resources exhausted

**Solution:** Check `config.yaml` settings and system health.

## Benefits

### Safety

- **Prevents compounding errors:** Won't make bad code worse
- **Early detection:** Catches problems before AI makes changes
- **Automatic recovery:** Fixes common issues without human intervention

### Reliability

- **Reproducible builds:** Always starts from a known-good state
- **Git history integrity:** Broken commits are automatically reverted
- **Clear audit trail:** Shows exactly what was reverted and why

### Developer Experience

- **Transparent:** Clear output showing what's happening
- **Automatic:** No manual intervention for common cases
- **Informative:** Detailed error messages when manual intervention needed

## Limitations

### What It Catches

✅ Broken builds due to:
- Syntax errors introduced by AI
- Missing includes/imports
- Type errors
- Linker errors

### What It Doesn't Catch

❌ Runtime errors:
- Segmentation faults
- Logic errors
- Race conditions
- Memory leaks

❌ Test failures:
- Unit test regressions
- Integration test failures
- Performance regressions

Future enhancements could add these checks.

## Future Enhancements

1. **Configurable max_reverts:** Add to `config.yaml`
2. **Test suite execution:** Run tests in addition to build
3. **Selective reversion:** Only revert commits in specific directories
4. **Revert strategies:** Try different recovery approaches
5. **Build caching:** Skip pre-flight if git HEAD matches last successful build
6. **Parallel workers:** Coordinate pre-flight checks across multiple workers

## Examples

### Example 1: Clean Build (Common Case)

```bash
$ make run
======================================================================
PRE-FLIGHT SANITY CHECK
======================================================================
Testing if source builds with configured build command...
Command: sudo make -j8 buildworld
======================================================================

[... 450 seconds of build output ...]

======================================================================
✓ PRE-FLIGHT CHECK PASSED
======================================================================
Source builds successfully in 450.2s
Warnings: 23
Proceeding with review workflow...
======================================================================

*** Loading review index...
    Found 156 reviewable directories
[... normal workflow continues ...]
```

### Example 2: Broken Build, Automatic Recovery

```bash
$ make run
======================================================================
PRE-FLIGHT SANITY CHECK
======================================================================
Testing if source builds with configured build command...
Command: sudo make -j8 buildworld
======================================================================

[... build fails ...]

======================================================================
✗ PRE-FLIGHT CHECK FAILED
======================================================================
Build failed with 5 errors

This suggests previous AI review runs introduced breaking changes.
Attempting to recover by reverting recent commits...

--- Revert Attempt 1/10 ---
Reverting: abc1234 bin/chmod: Fix integer parsing
Testing build after revert...
Build still fails (5 errors). Trying another revert...

--- Revert Attempt 2/10 ---
Reverting: def5678 bin/cat: Add error checking  
Testing build after revert...
Build still fails (3 errors). Trying another revert...

--- Revert Attempt 3/10 ---
Reverting: ghi9012 bin/ls: Security improvements
Testing build after revert...

======================================================================
✓ BUILD RECOVERED
======================================================================
Reverted 3 commit(s):
  1. abc1234 bin/chmod: Fix integer parsing
  2. def5678 bin/cat: Add error checking
  3. ghi9012 bin/ls: Security improvements

Source now builds successfully in 450.2s
Proceeding with review workflow from this point...
======================================================================

*** Loading review index...
    Found 156 reviewable directories
[... normal workflow continues ...]
```

### Example 3: Unrecoverable Failure

```bash
$ make run
======================================================================
PRE-FLIGHT SANITY CHECK
======================================================================
[... attempts to revert 10 commits, all fail ...]

======================================================================
✗ RECOVERY FAILED
======================================================================
Reverted 10 commits but source still doesn't build.
Manual intervention required.

Reverted commits:
  1. abc1234 bin/chmod: Fix integer parsing
  2. def5678 bin/cat: Add error checking
  [... 8 more ...]

To restore original state:
  git reset --hard abc1234
======================================================================

$ echo $?
1
```

## Testing

To test the pre-flight check:

```bash
# 1. Introduce a breaking change manually
echo "SYNTAX ERROR" >> bin/chmod/chmod.c
git add bin/chmod/chmod.c
git commit -m "TEST: Intentional break"

# 2. Run reviewer
python3 reviewer.py

# Expected: Pre-flight check should revert the commit and proceed

# 3. Verify the bad commit was reverted
git log -3 --oneline
# Should show: Revert "TEST: Intentional break"
```

## Summary

The pre-flight sanity check is a critical safety feature that:
- ✅ Verifies source builds before making changes
- ✅ Automatically recovers from broken builds
- ✅ Prevents compounding errors
- ✅ Maintains git history integrity
- ✅ Requires zero configuration (works out of the box)

It's a simple but powerful addition that makes the ai-code-reviewer much more robust in production use.

