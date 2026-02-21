# File Chunking Configuration

## Overview

To prevent timeouts when reviewing large files, the tool automatically splits files into manageable chunks based on function boundaries.

## How It Works

1. **Small files** (< threshold) are reviewed in one pass
2. **Large files** (>= threshold) are split by function into chunks
3. Each chunk is reviewed separately with `ACTION: NEXT_CHUNK`
4. Context is maintained between chunks

## Default Settings

```yaml
review:
  chunk_threshold: 400    # Files >= 400 lines are chunked
  chunk_size: 250         # Maximum 250 lines per chunk
```

## Timeout Settings

```yaml
tokenhub:
  timeout: 600            # 10 minutes
```

## Customization

### For Fast GPUs (128GB+ VRAM)
If you have a powerful GPU and want larger chunks:

```yaml
review:
  chunk_threshold: 600    # Larger threshold
  chunk_size: 400         # Larger chunks

tokenhub:
  timeout: 900            # 15 minutes for larger chunks
```

### For Slower Models or Limited Resources
If you're still getting timeouts:

```yaml
review:
  chunk_threshold: 200    # Chunk sooner
  chunk_size: 150         # Smaller chunks

tokenhub:
  timeout: 300            # 5 minutes (faster to detect issues)
```

### For Very Large Files
If you have files with 5000+ lines:

```yaml
review:
  chunk_threshold: 300    # Chunk early
  chunk_size: 200         # Small chunks for consistency
```

## Troubleshooting Timeouts

### Symptom: "ERROR: Request timed out"

**Cause**: The model is taking too long to process the chunk.

**Solutions** (try in order):

1. **Reduce chunk size** (current: 250 lines)
   ```yaml
   review:
     chunk_size: 150
   ```

2. **Lower chunk threshold** (current: 400 lines)
   ```yaml
   review:
     chunk_threshold: 200
   ```

3. **Increase timeout** (current: 600s)
   ```yaml
   tokenhub:
     timeout: 900
   ```

4. **Check TokenHub / backend model health**
   ```bash
   make tokenhub-status
   ```

5. **Reduce context window**
   - Clear old beads: `bd close --all`
   - Delete old lessons: `rm .ai-code-reviewer/LESSONS.md`
   - Restart review session

### Symptom: Chunks too small, too many chunks

**Cause**: Settings are too aggressive for your hardware.

**Solution**: Increase chunk size:
```yaml
review:
  chunk_threshold: 600
  chunk_size: 400
```

### Symptom: AI asks to review same file multiple times

**Cause**: Chunk tracking might be confused.

**Solution**:
1. Use `ACTION: SKIP_FILE` to move on
2. Try different directory with `ACTION: SET_SCOPE`

## How Chunking Works Internally

### C File Chunking Strategy

1. **Parse file** for function boundaries using regex
2. **Split by functions** (each function = potential chunk)
3. **Group small functions** together (up to chunk_size)
4. **Split large functions** (>2x chunk_size) into sub-chunks
5. **Include context** (headers, globals, up to 20 lines between functions)

### Example

File with 1200 lines, 10 functions:
```
threshold: 400, chunk_size: 250

Chunk 1: Header + includes (50 lines)
Chunk 2: Function 1 (200 lines)
Chunk 3: Function 2 (150 lines)
Chunk 4: Function 3 + Function 4 (180 lines combined)
Chunk 5: Function 5 (part 1) (250 lines)
Chunk 6: Function 5 (part 2) (220 lines)
Chunk 7: Functions 6-8 (240 lines combined)
Chunk 8: Function 9 (190 lines)
Chunk 9: Function 10 + footer (120 lines)
```

## Migration

Existing users will automatically get new defaults via `config-update`:
- Timeout: 300s → 600s
- Threshold: 800 → 400 lines
- Chunk size: 500 → 250 lines

Run `make config-update` or `python scripts/config_update.py` to apply.

## Benefits of Smaller Chunks

1. **Faster responses** - Model processes less text
2. **Better focus** - AI reviews one function at a time
3. **Reduced memory** - Lower GPU VRAM usage
4. **Fewer timeouts** - More predictable processing time
5. **Better error messages** - Issues isolated to specific functions

## Trade-offs

**Smaller chunks (150-200 lines)**:
- ✅ Faster, fewer timeouts
- ❌ More chunks to review (more iterations)
- ❌ Less context between functions

**Larger chunks (400-500 lines)**:
- ✅ Fewer chunks (faster overall)
- ✅ More context visible
- ❌ Higher risk of timeouts
- ❌ Requires more GPU memory

## Recommended Settings by Use Case

### FreeBSD kernel (large C files, complex)
```yaml
review:
  chunk_threshold: 300
  chunk_size: 200
tokenhub:
  timeout: 600
```

### User-space utilities (smaller files)
```yaml
review:
  chunk_threshold: 500
  chunk_size: 300
tokenhub:
  timeout: 600
```

### Mixed codebase (safe defaults)
```yaml
review:
  chunk_threshold: 400
  chunk_size: 250
tokenhub:
  timeout: 600
```
