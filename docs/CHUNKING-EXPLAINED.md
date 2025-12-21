# Hierarchical Chunking System Explained

## 🎯 Most Important Concept

**BUILD and COMMIT happen at the DIRECTORY level, not per-file or per-function!**

```
Directory (bin/chio)
  ├─ Review ALL files
  ├─ Make edits across ALL files  
  ├─ BUILD once (tests everything together)  ← HERE
  ├─ If build FAILS: Stay in directory, fix, rebuild
  ├─ If build SUCCEEDS: COMMIT                ← HERE
  └─ Move to next directory
```

**Why?**
- FreeBSD's build system works at directory level (each has a Makefile)
- Testing per-function would require ~1000s of builds per directory
- Changes often span multiple files (e.g., .c + .h + Makefile)
- One directory = one logical unit = one commit

**Build Failure = Rewind to Directory Level**
- If BUILD fails, AI stays in current directory
- Can re-review any file (READ_FILE)
- Make additional fixes (EDIT_FILE)
- Try BUILD again
- Repeat until build succeeds
- **Cannot move to next directory until build succeeds**

---

## The Four-Level Hierarchy

The reviewer operates on a **nested hierarchy**:

```
Level 1: Source Tree
  ├─ 830 reviewable directories
  └─ Level 2: Directory (bin/chio)  ← BUILD + COMMIT HERE
      ├─ 5 files to review
      ├─ Level 3: File (chio.c)
      │   ├─ 1,314 lines
      │   └─ Level 4: Chunks (52 functions)
      │       ├─ Chunk 1: file_header (60 lines)
      │       ├─ Chunk 2: usage() (1 line)
      │       ├─ ...
      │       └─ Chunk 52: print_designator() (60 lines)
      └─ Level 3: File (chio.8)
          └─ Level 4: Single chunk (man page)
```

## Key Concepts

### 1. **Steps** (AI Actions - Horizontal Progress)

**What they are:**
- Each "step" is one action the AI performs
- Example actions: SET_SCOPE, READ_FILE, NEXT_CHUNK, EDIT_FILE, BUILD

**Configuration:**
```yaml
review:
  max_iterations: 200  # Maximum steps per session
```

**Why 200?**
- One directory with 5 files × 50 chunks each = ~250+ steps
- Need room for: reading chunks + edits + building + fixes
- 200 allows reviewing a medium-sized directory completely

### 2. **Hierarchy Levels** (Vertical Structure)

#### **Level 1: Source Tree**
- **What:** Entire FreeBSD source
- **Size:** 830 reviewable directories
- **When reviewed:** Over many sessions

#### **Level 2: Directory** ← **BUILD + COMMIT BOUNDARY**
- **What:** One source directory (e.g., bin/chio)
- **Size:** Typically 1-10 files
- **When reviewed:** One directory per build cycle
- **Critical:** BUILD happens at this level, not per-file!

#### **Level 3: File**
- **What:** One source file (e.g., chio.c)
- **Size:** 1-50,000 lines
- **Chunking:** Files >800 lines split into functions

#### **Level 4: Chunk**
- **What:** One function or file section
- **Size:** Typically 10-200 lines
- **Purpose:** Focused review without overwhelming AI

### 3. **Chunks** (File Sections)

**What they are:**
- Sections of a large file split by function boundaries
- Each chunk is typically one function
- Large functions (>500 lines) split into sub-chunks

**Chunking threshold:**
- Files < 800 lines: NOT chunked (reviewed whole)
- Files ≥ 800 lines: Chunked by functions

**Example:**
```
bin/chio/chio.c (1,314 lines) → 52 chunks
  - Chunk 1:  file_header (lines 1-60, 60 lines)
  - Chunk 2:  usage() (lines 61-61, 1 line)
  - Chunk 3:  cleanup() (lines 62-62, 1 line)
  - ...
  - Chunk 27: do_move() (lines 184-261, 78 lines)
  - Chunk 28: do_exchange() (lines 262-370, 109 lines)
  - ...
  - Chunk 52: print_designator() (lines 1255-1314, 60 lines)
```

---

## Review Flow Example

### Reviewing bin/chio Directory (5 files, 1 with 52 chunks)

```
==================== LEVEL 2: DIRECTORY ====================

Step 1:
  AI → ACTION: SET_SCOPE bin/chio
  System → Shows hierarchy:
           "Level 1: Source tree (830 directories)"
           "Level 2: bin/chio ← YOU ARE HERE"
           "Level 3: 5 reviewable files"
           "Level 4: Functions (auto-chunked)"
           
           FILES TO REVIEW:
           - bin/chio/Makefile
           - bin/chio/chio.c
           - bin/chio/chio.8
           
           WORKFLOW:
           1. Review each file
           2. Make edits as needed
           3. When ALL files reviewed: BUILD
           4. If build succeeds: Commit entire directory

==================== LEVEL 3: FILE #1 ====================

Step 2:
  AI → ACTION: READ_FILE bin/chio/Makefile
  System → "📄 File: bin/chio/Makefile (small, no chunking)"
           Returns full file content
           
           PROGRESS:
           📁 Directory: bin/chio
              Files: 1/5
           📄 File: bin/chio/Makefile

Step 3:
  AI reviews Makefile, no issues found

==================== LEVEL 3: FILE #2 ====================

Step 4:
  AI → ACTION: READ_FILE bin/chio/chio.c
  System → "📋 CHUNKED FILE REVIEW MODE"
           "Total chunks: 52"
           Returns: Chunk 1/52 (file_header)
           
           PROGRESS:
           📁 Directory: bin/chio
              Files: 1/5 (reviewing)
           📄 File: bin/chio/chio.c
              Chunks: 1/52

==================== LEVEL 4: CHUNKS ====================

Step 5:
  AI → ACTION: NEXT_CHUNK
  System → Returns: Chunk 2/52 (usage function)
           PROGRESS:
           📁 Directory: bin/chio
              Files: 1/5
           📄 File: bin/chio/chio.c
              Chunks: 2/52

Step 6-56:
  AI continues: NEXT_CHUNK → review each function → fix bugs
  
  Step 15:
    AI → ACTION: EDIT_FILE bin/chio/chio.c
    Fixes bug in parse_element_type()
    PROGRESS shows: ✏️ Edits: 1 files modified
  
  Step 56:
    System → "NEXT_CHUNK_COMPLETE: All chunks of chio.c reviewed"
    PROGRESS:
    📁 Directory: bin/chio
       Files: 2/5 (file complete!)
    ✏️ Edits: 1 files modified

==================== LEVEL 3: FILE #3 ====================

Step 57:
  AI → ACTION: READ_FILE bin/chio/chio.8
  System → Returns man page content (no chunking)

Step 58:
  AI reviews man page, no issues

Step 59-65:
  AI reviews remaining files in directory
  Makes additional fixes to 2 more files

==================== LEVEL 2: DIRECTORY BUILD ====================

Step 66:
  All files in bin/chio reviewed and fixed
  AI → ACTION: BUILD
  System → Runs: make buildworld
           Tests ALL changes in bin/chio together
           Result: Build succeeds ✓

Step 67:
  System → Generates commit message
           Commits ALL changes for bin/chio
           Pushes to repository
           
           COMPLETE:
           📁 Directory: bin/chio ✓
              Files reviewed: 5/5
              Files fixed: 3
              Build: Success
              Committed: Yes

==================== LEVEL 1: NEXT DIRECTORY ====================

Step 68:
  AI → ACTION: SET_SCOPE bin/chmod
  System → "Now reviewing bin/chmod (7 files)"
  
(Session continues until max_iterations or all work complete)
```

---

## Calculations

### Steps Needed for Various File Sizes

| File Lines | Chunks | Steps to Read All | Steps for Edits | Total Steps | Within 200? |
|------------|--------|-------------------|-----------------|-------------|-------------|
| 500        | 1      | 1                 | ~2-5            | ~10         | ✅ Yes      |
| 1,314      | 52     | 52                | ~10-15          | ~70         | ✅ Yes      |
| 5,000      | ~200   | 200               | ~20-30          | ~230        | ⚠️ Tight   |
| 10,000     | ~400   | 400               | ~40-50          | ~450        | ❌ No       |
| 50,000     | ~2000  | 2000              | ~200-300        | ~2300       | ❌ No       |

### Overhead Steps (Every File)
- Setup: 2-3 steps (SET_SCOPE, LIST_DIR)
- Build: 1 step (BUILD)
- Commit: 1 step (AI generates message, system commits)
- **Total overhead: ~5 steps**

### For Very Large Files (>5K lines)

**Option 1: Multiple Sessions**
- Session 1: Review chunks 1-100, make fixes, BUILD
- Session 2: Review chunks 101-200, make fixes, BUILD
- Commit after each successful build

**Option 2: Increase max_iterations**
```yaml
review:
  max_iterations: 500  # For very large files
```

**Option 3: Skip or Split**
- Use `ACTION: SKIP_FILE` for generated/vendor code
- Or manually split file into smaller files

---

## What You See in Logs

### New Hierarchical Display

```
======================================================================
STEP 5/200

📁 Directory: bin/chio
   Files: 1/5
📄 File: bin/chio/chio.c
   Chunks: 2/52
======================================================================
```

This means:
- **Step**: 5th AI action out of max 200
- **Directory**: Currently reviewing bin/chio
- **Files**: Reviewed 1 out of 5 files (currently on file 1)
- **File**: Currently reading bin/chio/chio.c
- **Chunks**: On chunk 2 of 52 in this file

### After Making Edits

```
======================================================================
STEP 15/200

📁 Directory: bin/chio
   Files: 1/5
📄 File: bin/chio/chio.c
   Chunks: 10/52
✏️  Edits: 1 files modified
======================================================================
```

Shows that edits have been made (accumulating until BUILD)

### After Completing a File

```
======================================================================
STEP 56/200

📁 Directory: bin/chio
   Files: 2/5
✏️  Edits: 1 files modified
======================================================================
```

File counter incremented (2/5), no longer showing chunks (file complete)

---

## 🔄 Build Failure Loop (Stay in Directory)

### What Happens When BUILD Fails

```
Step 66: AI → ACTION: BUILD
         System → "BUILD_FAILED: Build errors detected"
         
         CURRENT STATE:
         📁 Directory: bin/chio
            Files: 5/5
         ✏️  Edits: 3 files modified
         
         BUILD ERROR REPORT:
         [Compiler errors shown]
         
         RECOVERY ACTIONS:
         1. Analyze the build errors above
         2. Re-read affected files if needed (READ_FILE)
         3. Make additional fixes (EDIT_FILE)
         4. Try building again (BUILD)
         5. Repeat until build succeeds
         
         IMPORTANT: You are still in bin/chio
         Do NOT move to next directory until build succeeds!
```

### AI Can Iterate

```
Step 67: AI → ACTION: READ_FILE bin/chio/chio.c
         (Re-reads file to understand error)

Step 68: AI → ACTION: NEXT_CHUNK
         (Navigates to function with error)

Step 69: AI → ACTION: EDIT_FILE bin/chio/chio.c
         (Makes additional fix)

Step 70: AI → ACTION: BUILD
         System → "BUILD_FAILED: Different error"
         (Still has issues, iterate again)

Step 71-75: More fixes...

Step 76: AI → ACTION: BUILD
         System → "BUILD_SUCCESS!"
         Directory complete, committed, move to next
```

### Safeguard: Cannot Leave Directory

If AI tries to move to next directory with uncommitted changes:

```
Step 50: AI → ACTION: SET_SCOPE bin/chmod

System → "SET_SCOPE_ERROR: Cannot change directory with uncommitted changes

Current directory: bin/chio
Pending changes: 3 files modified

You MUST complete the current directory first:
1. Review all files in bin/chio
2. Run ACTION: BUILD to test changes
3. If build fails: fix errors and BUILD again
4. If build succeeds: changes will be committed automatically
5. THEN you can move to: bin/chmod

BUILD and COMMIT happen at directory level!
Each directory is one logical unit."
```

### This Enforces the Hierarchy

- **Level 2 (Directory)** is the boundary for BUILD/COMMIT
- Cannot escape directory until work is complete
- AI must iterate until build succeeds
- This prevents:
  - Leaving broken code behind
  - Committing untested changes
  - Fragmenting related changes across commits

---

## Key Takeaways

✅ **Steps** = AI conversation turns (max_iterations in config)

✅ **Chunks** = File sections (automatic, based on file size)

✅ **200 steps** = Can review files with ~150 chunks (~7,500 lines)

✅ **One step per action** = One action (read chunk, edit file, build, etc.)

✅ **Many steps per file** = Large files need many steps to review all chunks

---

## Troubleshooting

### "Hit max_iterations limit before completing file"

**Symptom:**
```
Step 200/200
HALT_ACKNOWLEDGED
(File only 30% reviewed)
```

**Solution:**
Increase max_iterations in config.yaml:
```yaml
review:
  max_iterations: 500  # or higher
```

### "Why is it taking so long?"

Each step includes:
1. AI analyzes context (1-5 minutes)
2. AI generates response (1-5 minutes)  
3. System executes action (seconds)

**Total:** 2-10 minutes per step

For 52-chunk file: 52 chunks × 3 min/step = **2-3 hours**

This is normal and expected for large files!

---

## Performance Optimization Tips

1. **Trust the AI** - Let it use SKIP_FILE for obvious vendor code
2. **Don't review generated code** - sqlite3.c doesn't need review
3. **Start with small directories** - bin/yes/ has tiny files, good for testing
4. **Use screen/tmux** - Large files take hours, don't lose session
5. **Monitor progress** - Check Step X/200 to estimate completion

