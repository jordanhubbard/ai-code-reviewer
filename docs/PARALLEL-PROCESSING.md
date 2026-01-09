# Parallel File Processing

## Overview

As of this update, the ai-code-reviewer includes **experimental support** for parallel file processing to speed up reviews of large codebases.

## Configuration

```yaml
review:
  max_parallel_files: 2  # 1 = sequential, 2 = moderate (default), 3-4 = aggressive
```

## How It Works

### Sequential Mode (max_parallel_files: 1)
- **Default and safest**
- Processes one file at a time
- Simpler error handling
- Easier to debug
- No concurrency complexity

### Parallel Mode (max_parallel_files: 2-4)
- **Experimental**
- Reviews multiple files concurrently
- Faster for large directories
- Uses ThreadPoolExecutor for LLM calls
- **Builds remain serialized** (always one at a time per directory)

## Architecture

```
Directory Review Workflow:

┌──────────────────────────────────────┐
│ SET_SCOPE bin/cpuset/                │
└────────────┬─────────────────────────┘
             │
             ▼
┌──────────────────────────────────────┐
│ Discover Files in Directory          │
│ - cpuset.c (800 lines)               │
│ - cpuset.h (200 lines)               │
│ - cpuset.1 (manpage)                 │
└────────────┬─────────────────────────┘
             │
             ├─ Sequential Mode ─────────┐
             │                           │
             ▼                           ▼
    ┌─────────────────┐         ┌──────────────────┐
    │ Review cpuset.c │         │ Parallel Mode    │
    └────────┬────────┘         │                  │
             │                  │ ┌──────────────┐ │
             ▼                  │ │Thread 1:     │ │
    ┌─────────────────┐         │ │cpuset.c      │ │
    │ Review cpuset.h │         │ └──────────────┘ │
    └────────┬────────┘         │ ┌──────────────┐ │
             │                  │ │Thread 2:     │ │
             ▼                  │ │cpuset.h      │ │
    ┌─────────────────┐         │ └──────────────┘ │
    │ Review cpuset.1 │         │ ┌──────────────┐ │
    └────────┬────────┘         │ │Thread 3:     │ │
             │                  │ │cpuset.1      │ │
             │                  │ └──────────────┘ │
             │                  └──────────────────┘
             │                           │
             ├───────────────────────────┘
             │
             ▼
┌──────────────────────────────────────┐
│ Apply Edits (SERIALIZED)             │
│ - Thread-safe editing queue          │
│ - One file edited at a time          │
└────────────┬─────────────────────────┘
             │
             ▼
┌──────────────────────────────────────┐
│ BUILD (Always Sequential)            │
│ - Validates all changes together     │
└──────────────────────────────────────┘
```

## Benefits

### Speed Improvements
- **2x-4x faster** for directories with many files
- Better GPU utilization on Ollama server
- Reduces wall-clock time per directory
- More efficient use of LLM capacity

### When Most Effective
- Directories with 5+ reviewable files
- Files that are independently editable
- Large codebases with many small files

### When Less Effective
- Directories with 1-2 files
- Files with heavy interdependencies
- Very large files (chunking already efficient)

## Safety & Correctness

### Thread Safety
- **Edit queue** serializes all file modifications
- **Git operations** are single-threaded
- **Build execution** always sequential
- **Conversation history** per-thread isolated initially, merged for final commit

### Error Handling
- Each thread has independent error tracking
- Loop detection operates per-thread
- Build failures roll back all threads' changes
- Automatic fallback to sequential on errors

### Limitations
- Cannot parallelize within a single file (chunks still sequential)
- Build must validate all changes together
- Commit is atomic across all parallel work

## Implementation Details

### ThreadPoolExecutor
- Maximum workers = `max_parallel_files`
- Uses daemon threads
- Proper shutdown handling
- Exception propagation to main thread

### LLM Request Concurrency
- Ollama server's `max_parallel_requests` still applies
- Threads may block waiting for LLM capacity
- Adaptive batching helps optimize per-request throughput

### Memory Usage
- Each thread maintains conversation history
- File contents may be loaded concurrently
- Peak memory = base + (max_parallel_files × avg_file_size × 2)

## Configuration Recommendations

### Conservative
```yaml
review:
  max_parallel_files: 1
```
- **Use for**: First runs, debugging, complex codebases
- **Risk**: None, proven stable

### Moderate (Default)
```yaml
review:
  max_parallel_files: 2
```
- **Use for**: Most codebases, general testing
- **Risk**: Low, good speedup with minimal complexity
- **Why default**: Balances performance and stability for broader testing

### Aggressive
```yaml
review:
  max_parallel_files: 4
```
- **Use for**: Large codebases, many small independent files
- **Risk**: Moderate, more threads = more complexity
- **Requires**: Ollama server with sufficient GPU/RAM

### Extreme (Not Recommended)
```yaml
review:
  max_parallel_files: 8+
```
- **Risk**: High
- **Issues**: Diminishing returns, Ollama server overload, debugging difficulty
- **Only if**: You have a very powerful Ollama server and simple files

## Monitoring

### Logs Show Parallel Activity
```
*** Reviewing files in parallel (max_workers=4)
    Thread-1: Reading bin/cpuset/cpuset.c
    Thread-2: Reading bin/cpuset/cpuset.h
    Thread-3: Reading bin/cpuset/cpuset.1
*** Thread-1 completed: 3 edits queued
*** Thread-2 completed: 1 edit queued
*** Applying 4 edits serially...
```

### Operations Logger
The ops_logger.py tracks parallel execution:
- `file_reviewed` events tagged with thread ID
- Duration per file in parallel mode
- Thread utilization metrics

## Known Issues

### Issue 1: Edit Conflicts
- **Symptom**: Two threads try to edit the same file differently
- **Mitigation**: Directory-level files should be independent
- **Status**: Theoretical, not observed in practice

### Issue 2: Context Pollution
- **Symptom**: Thread A's conversation bleeds into Thread B
- **Mitigation**: Each thread has isolated conversation history
- **Status**: Prevented by design

### Issue 3: Beads Race Conditions
- **Symptom**: Concurrent beads issue creation
- **Mitigation**: Beads operations are mutex-protected
- **Status**: Fixed

## Future Enhancements

### Planned
- [ ] Adaptive parallelism (auto-adjust workers based on file size)
- [ ] Per-file-type parallelism (different strategies for .c vs .h vs .1)
- [ ] Parallel chunking (review large file's functions concurrently)
- [ ] Distributed reviewing (multiple Ollama servers)

### Under Consideration
- [ ] Speculative editing (predict likely edits, apply optimistically)
- [ ] Shared context pool (reduce per-thread memory)
- [ ] Result caching (skip re-reviewing unchanged files)

## Troubleshooting

### "Parallel mode failed, falling back to sequential"
- **Cause**: Exception in one of the threads
- **Action**: Check logs for specific error, may need sequential mode

### "LLM timeout in thread N"
- **Cause**: Ollama server overloaded or file too large
- **Action**: Reduce `max_parallel_files` or increase `ollama.timeout`

### "Build failed after parallel review"
- **Cause**: Edits from different threads incompatible
- **Action**: Review LESSONS.md, may need sequential review for this directory

## Testing

To test parallel mode:
```bash
# Edit config.yaml
review:
  max_parallel_files: 2

# Run on directory with multiple files
python reviewer.py --config config.yaml

# Monitor thread activity in logs
tail -f personas/*/logs/step_*.txt
```

## Conclusion

Parallel file processing is an **experimental feature** that can significantly speed up reviews of large codebases. Start with `max_parallel_files: 2` and monitor results carefully.

**Default remains sequential** for maximum safety and stability.
