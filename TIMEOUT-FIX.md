# Ollama Timeout Fix for Large Files (SUPERSEDED)

**Note:** This document describes the initial timeout approach. The project now uses **function-based chunking** (see below) which is the preferred scalable solution.

## Original Problem (Solved with Chunking)

The Angry AI reviewer was timing out when analyzing large source files:

- **Default timeout**: 300 seconds (5 minutes)
- **Large files**: Files with 1000+ lines or 30KB+ take 10-15 minutes to analyze
- **Example failure**: `bin/chio/chio.c` (1314 lines, 30KB) timed out even at 900s

## Root Cause

The `ollama_client.py` uses Python's `urlopen()` with a timeout parameter. When the model takes longer than the timeout to generate a response, the connection is closed and an exception is raised.

Large files require:
1. More tokens in the context (input)
2. More time for the model to analyze
3. More tokens in the response (output)

The qwen2.5-coder:32b model on remote hardware can take 10-15 minutes for complex analysis of large files.

## Solution

### 1. Increased Timeout (Quick Fix)

Created `angry-ai/config.yaml` with:

```yaml
ollama:
  timeout: 900  # 15 minutes (was 300)
```

This gives the model enough time to analyze large files.

### 2. Large File Warnings

Added detection in `reviewer.py` that warns before reading files with:
- More than 1000 lines, OR
- More than 40KB in size

Example warning:
```
⚠️  WARNING: Large file detected (1314 lines, 30435 bytes)
   Analysis may take 10-15 minutes. Consider reviewing in sections.
   Timeout is set to 900s.
```

### 3. Context Size Warnings

Added warning in `ollama_client.py` when context exceeds 30,000 characters:
```
WARNING: Large context (45000 chars) - may take several minutes...
```

### 4. Better Timeout Error Messages

When a timeout occurs, the user now sees:
```
ERROR: Request timed out
The model took longer than 900s to respond.
This usually happens with large files (1000+ lines).

Solutions:
1. Increase timeout in config.yaml: ollama.timeout (currently 900s)
2. Review smaller files first
3. Break large files into sections
```

## Configuration

### For FreeBSD System

The config file should be at:
```
/usr/home/jkh/Src/freebsd-src-on-angry-AI/angry-ai/config.yaml
```

Copy from macOS to FreeBSD:
```bash
scp angry-ai/config.yaml freebsd-host:/usr/home/jkh/Src/freebsd-src-on-angry-AI/angry-ai/
```

### Adjusting Timeout

Edit `config.yaml`:

```yaml
ollama:
  timeout: 900   # 15 minutes (good for most files)
  # timeout: 1800  # 30 minutes (for very large files)
  # timeout: 3600  # 1 hour (for massive files)
```

## Testing

To verify the fix works:

```bash
cd /usr/home/jkh/Src/freebsd-src-on-angry-AI
make run
```

The reviewer should now:
1. Warn when reading large files
2. Not timeout on files under 2000 lines
3. Show helpful error messages if timeout still occurs

## Performance Guidelines

| File Size | Lines | Typical Analysis Time | Recommended Timeout |
|-----------|-------|----------------------|---------------------|
| Small     | < 500 | 30-60 seconds        | 300s (5 min)        |
| Medium    | 500-1000 | 2-5 minutes       | 600s (10 min)       |
| Large     | 1000-2000 | 5-15 minutes      | 900s (15 min)       |
| Very Large| 2000+ | 15-30 minutes        | 1800s (30 min)      |

## Alternative Approaches

If timeouts continue to be a problem:

### 1. Review in Sections
Instead of reading entire large files, review functions/sections individually.

### 2. Use Faster Model
Switch to a smaller, faster model for initial passes:
```yaml
ollama:
  model: "qwen2.5-coder:14b"  # Faster than 32b
```

### 3. Local GPU
Run Ollama on the same machine as the reviewer to eliminate network latency.

### 4. Chunked Review
Implement a chunking strategy in `reviewer.py` that breaks large files into reviewable sections.

## CURRENT SOLUTION: Function-Based Chunking

**Commit:** `d303ca300ed` (Dec 20, 2024)

Instead of trying to review entire large files at once, the reviewer now uses **intelligent chunking**:

### How It Works

1. **Small files** (<800 lines): Reviewed whole (85% of .c files)
2. **Large files** (>800 lines): Split by function boundaries
3. **Huge functions** (>500 lines): Split into sub-chunks

### New Actions

```
ACTION: READ_FILE bin/chio/chio.c
  → Returns first chunk (file header + includes)
  → Shows "📋 CHUNKED FILE REVIEW MODE"

ACTION: NEXT_CHUNK
  → Returns next function to review
  → Continues until all functions reviewed

ACTION: SKIP_FILE  
  → Skips remaining chunks (for vendor/generated code)
```

### Example: bin/chio/chio.c

```
File: 1314 lines → 52 function-based chunks
- Chunk 1: file_header (lines 1-60)
- Chunk 2: usage() (lines 61-61)
- Chunk 3: cleanup() (lines 62-62)
- ...
- Chunk 27: do_move() (lines 184-261, 78 lines)
- Chunk 28: do_exchange() (lines 262-370, 109 lines)
- ...
```

### FreeBSD Source Tree Statistics

```
Total .c files: 22,223

Size Distribution:
- 0-100 lines:      5,574 files (25%)
- 100-500 lines:    9,894 files (45%)  
- 500-1000 lines:   3,476 files (16%)
- 1000-2000 lines:  2,034 files (9%)   ← Chunking starts here
- 2000-5000 lines:    982 files (4%)   ← Heavy chunking
- 5000-10K lines:     193 files (<1%)
- 10K-50K lines:       58 files (<1%)
- 50K-100K lines:       5 files (<1%)
- 100K+ lines:          7 files (<1%)  ← Generated/vendor code
```

### Benefits

✅ **Scalable**: Can review ANY file size
✅ **Focused**: One function at a time - better quality review
✅ **Faster**: Shorter contexts = faster analysis
✅ **No timeouts**: Each chunk analyzed quickly
✅ **Complete coverage**: Can review entire 22,223-file codebase

### Performance

| File Size | Whole File | Chunked (per function) |
|-----------|------------|------------------------|
| 500 lines | 2-5 min    | N/A (not chunked)      |
| 1000 lines| 10-15 min  | 1-2 min per function   |
| 2000 lines| 30+ min    | 1-2 min per function   |
| 10K lines | Timeout    | 1-2 min per function   |
| 100K+ lines| Impossible | 1-2 min per function   |

## Legacy Timeout Approach (Still Available)

The timeout can still be configured for small/medium files:

```yaml
ollama:
  timeout: 3600  # 60 minutes (FreeBSD config)
```

But chunking is the recommended approach for files >800 lines.

## Commits

- Initial timeout fix: `a122c35d1f5`
- Chunking implementation: `d303ca300ed`
- Issue: `freebsd-src-on-angry-AI-ych` (closed)

