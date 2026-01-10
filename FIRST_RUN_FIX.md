# First Run Detection and Beads Initialization Fix

## Problem

When running `make run` for the first time, the system would:
1. Create a default `config.yaml` with source.root pointing to `..` (incorrect)
2. Try to build without validating the source tree exists
3. Either fail with obscure errors or hang indefinitely
4. Not properly initialize beads for the source tree being reviewed

Additionally, if a `.beads/` directory existed from reviewing a different source tree, the old beads would reference wrong directories and cause confusion.

## Solution

Added comprehensive first-run detection and validation:

### 1. Source Tree Validation (`validate_source_tree()`)

Checks that the configured source root:
- Exists and is a directory
- Contains a Makefile or CMakeLists.txt
- Fails fast with clear error message and remediation steps

### 2. Beads Installation Check (`check_beads_installation()`)

Detects if the `bd` (beads) CLI is installed:
- Warns if missing with installation instructions
- Continues without beads if not available
- Gracefully degrades functionality

### 3. Wrong Source Tree Detection

BeadsManager now detects when existing beads reference directories from a different source tree:
- Checks if directory paths look external (`../` prefix or non-existent top-level dirs)
- Warns the user with clear remediation options
- Clears old beads and creates new ones for the current source tree

### 4. Improved Makefile `check-deps`

Enhanced dependency checking to:
- Check for beads (bd) CLI
- Warn if config.yaml is missing
- Provide actionable next steps

### 5. Better First-Run Experience

When config.yaml is created from defaults:
- Sets a flag (`created_new_config`)
- Provides detailed configuration instructions
- Explains what needs to be edited
- Fails fast if source tree is invalid with helpful error messages

## Files Changed

1. **reviewer.py**
   - Added `validate_source_tree()` function
   - Added `check_beads_installation()` function
   - Enhanced `BeadsManager.__init__()` to detect wrong source trees
   - Added `BeadsManager._check_for_wrong_source_tree()` method
   - Updated `_init_beads_manager()` to warn about wrong beads
   - Improved `main()` with validation and better error messages

2. **Makefile**
   - Enhanced `check-deps` target to check for beads
   - Added warnings for missing config.yaml
   - Better first-run guidance

## User Experience

### Before
```bash
$ make run
Checking dependencies...
✓ Python3 found
✓ pip found
✓ PyYAML found
All dependencies satisfied!
[... starts build on wrong source tree ...]
[... loops forever or fails with cryptic error ...]
```

### After
```bash
$ make run
Checking dependencies...
✓ Python3 found: Python 3.14.2
✓ pip found: pip 24.0
✓ PyYAML found
⚠  Beads (bd) CLI not found
   Install from: https://github.com/steveyegge/beads
   (Optional but recommended for issue tracking)

⚠  config.yaml not found - will be created on first run
   You'll need to edit it to set:
   - Ollama server URL
   - Source tree path
   - Build command
All required dependencies satisfied!

*** Created config.yaml from defaults
*** IMPORTANT: Edit config.yaml to configure:
***   1. Ollama server URL (ollama.url)
***   2. Source root path (source.root)
***   3. Build command (source.build_command)
***
***   vim config.yaml

======================================================================
ERROR: Invalid Source Tree Configuration
======================================================================
Source root does not appear to be a buildable project: /Users/jordanh/Src/AI
Expected to find Makefile or CMakeLists.txt but found neither.
Please set source.root in config.yaml to point to your source tree.

You just created a new config.yaml from defaults.
The default source.root setting is '..' which may not be correct.

Please fix config.yaml:
  1. Open: config.yaml
  2. Set source.root to your source tree path
  3. Example: source.root: "/Users/jordanh/freebsd-src"
  4. Set source.build_command to your build command

Current source.root: /Users/jordanh/Src/AI
======================================================================
```

## Benefits

1. **Fails Fast**: No more waiting for long builds on wrong source trees
2. **Clear Errors**: Actionable error messages explain exactly what to fix
3. **Guided Setup**: First-run experience guides users through configuration
4. **Beads Compatibility**: Handles existing beads databases gracefully
5. **Prevents Confusion**: Detects and warns about wrong source tree beads

## Testing

To test the fix:

```bash
# Test 1: First run without config.yaml
rm config.yaml
make run
# Should create config, detect invalid source tree, provide clear error

# Test 2: With valid config but no bd
# (assuming bd is not installed)
# Edit config.yaml to point to valid source tree
make run
# Should warn about missing bd, continue without beads

# Test 3: With wrong source tree beads
# (if .beads/ exists with old beads)
# Edit config.yaml to point to different source tree
make run
# Should detect wrong beads, warn, clear, and create new ones
```
