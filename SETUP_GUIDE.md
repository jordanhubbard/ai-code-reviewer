# Quick Setup Guide

## First Time Setup

### 1. Check Dependencies

```bash
make check-deps
```

This will check for:
- Python 3 ✓
- pip ✓
- PyYAML ✓
- Beads (bd) CLI ⚠️ (optional but recommended)

### 2. Install Beads (Optional but Recommended)

Beads provides issue tracking and progress management for AI agents.

```bash
# Install beads
# Visit: https://github.com/steveyegge/beads
# Follow the installation instructions there

# After installation, initialize beads in your source tree
cd /path/to/your/source/tree
bd onboard
```

### 3. Create config.yaml

On first run, the system will create `config.yaml` from defaults:

```bash
make run
```

You'll see:
```
*** Created config.yaml from defaults
*** IMPORTANT: Edit config.yaml to configure:
***   1. Ollama server URL (ollama.url)
***   2. Source root path (source.root)
***   3. Build command (source.build_command)
```

The system will exit with an error because the default source tree is not configured.

### 4. Edit config.yaml

Open `config.yaml` and configure:

```yaml
# 1. Ollama server (REQUIRED)
ollama:
  url: "http://your-ollama-server:11434"
  model: "qwen2.5-coder:32b"

# 2. Source tree (REQUIRED)
source:
  # Path to your source code
  # Use absolute path or relative to this config file
  root: "/Users/you/freebsd-src"  # CHANGE THIS!
  
  # Build command to validate changes
  # This runs in the source.root directory
  build_command: "sudo make -j$(sysctl -n hw.ncpu) buildworld"
  
  # Build timeout (adjust for your project)
  build_timeout: 7200  # 2 hours for FreeBSD buildworld

# 3. Review settings (OPTIONAL)
review:
  persona: "personas/freebsd-angry-ai"
  target_directories: 10
  max_iterations_per_directory: 200
```

### 5. Validate Ollama Connection

```bash
python3 reviewer.py --validate-only
```

This will check:
- Can connect to Ollama server
- Model is available
- Configuration is valid

### 6. Run the Reviewer

```bash
make run
```

Or with verbose logging:

```bash
make run-verbose
```

## Common Issues

### "Source root does not appear to be a buildable project"

**Problem**: The `source.root` in config.yaml doesn't point to a valid source tree.

**Solution**: Edit config.yaml and set source.root to your actual source code directory:

```yaml
source:
  root: "/Users/you/freebsd-src"  # Must contain a Makefile or CMakeLists.txt
```

### "Beads (bd) CLI not found"

**Problem**: The beads command-line tool is not installed.

**Solution**: 
- Option 1: Install beads from https://github.com/steveyegge/beads
- Option 2: Continue without beads (issue tracking will be disabled)

### "WARNING: Existing beads appear to be for a different source tree"

**Problem**: The `.beads/` directory contains issues for a different source tree.

**Solution**: Choose one:
1. Clear old beads: `bd close --all`
2. Start fresh: `rm -rf .beads && bd onboard`
3. Point config.yaml to the correct source tree

### Build hangs or takes forever

**Problem**: The build command is running on the wrong source tree or build is actually very slow.

**Solutions**:
1. Verify source.root points to the correct directory
2. Test your build command manually first:
   ```bash
   cd /your/source/root
   make buildworld  # or whatever your build command is
   ```
3. Adjust build_timeout in config.yaml if your build is legitimately slow
4. Use --skip-preflight to skip the initial build check (not recommended)

## Configuration Examples

### FreeBSD Source Tree

```yaml
source:
  root: "/usr/src"
  build_command: "sudo make -j$(sysctl -n hw.ncpu) buildworld"
  build_timeout: 7200
```

### Linux Kernel

```yaml
source:
  root: "/usr/src/linux"
  build_command: "make -j$(nproc)"
  build_timeout: 1800
```

### Rust Project

```yaml
source:
  root: "/home/you/my-rust-project"
  build_command: "cargo build --release"
  build_timeout: 600
```

### CMake Project

```yaml
source:
  root: "/home/you/my-cmake-project"
  build_command: "cmake --build build --parallel"
  build_timeout: 900
```

### Python Project

```yaml
source:
  root: "/home/you/my-python-project"
  build_command: "python -m pytest"
  build_timeout: 300
```

## Next Steps

After successful setup:

1. The system will generate a review index of your source tree
2. It will run a pre-flight build check to ensure the source builds
3. The AI will start reviewing code directory by directory
4. Progress is tracked in:
   - `.ai-code-reviewer/REVIEW-INDEX.md` - Directory completion status
   - `personas/*/REVIEW-SUMMARY.md` - Detailed review history
   - `personas/*/LESSONS.md` - Patterns learned from mistakes
   - `.beads/` - Issue tracking database (if beads is installed)

## Getting Help

- Check the logs: `.ai-code-reviewer/logs/`
- Enable verbose mode: `make run-verbose`
- Review the persona files in `personas/freebsd-angry-ai/`
- Check for git conflicts: `git status`

## Advanced Configuration

See `config.yaml.defaults` for all available options including:
- Ollama batching and GPU settings
- Parallel file processing
- Custom persona directories
- Operations logging
- Skip patterns for files to ignore
