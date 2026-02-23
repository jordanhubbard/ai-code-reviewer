# Changelog

All notable changes to the AI Code Reviewer project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

## [0.31] - 2026-02-22

### Added
- **Build environment variables** - New `build_environment` config section under `source:` allows declarative environment variable configuration for build commands (e.g., `CC`, `CFLAGS`, `MAKEFLAGS`, `DESTDIR`). Variables are merged with the current environment and applied to both `build_command` and `pre_build_command` subprocesses. When empty (default), inherits the parent environment unmodified.

### Fixed
- Fix `test_forever_mode_halt` to work in sandboxed environments by mocking GitHelper instead of running subprocess git commands in temp directories

## [0.22] - 2026-02-10

### Fixed
- **CRITICAL**: Fix response validator rejecting valid short ACTION responses. `_validate_response()` flagged any response under 50 chars as "truncated", but valid ACTIONs like `ACTION: LIST_DIR bin/cpuset` are only 28 chars. This caused the AI to loop indefinitely: emit valid ACTION → rejected as incomplete → retry → rejected again. Short responses containing a parseable ACTION keyword now pass through.

## [0.21] - 2026-02-10

### Fixed
- **CRITICAL**: Fix context window overflow that caused "All hosts failed" after conversation history grew beyond 32K model context. History pruning now uses token-aware budgeting (chars/4 estimate) instead of a fixed 42-message limit.

### Changed
- Default lessons truncation reduced from 8K to 4K chars to leave room for conversation (configurable via `max_lessons_chars` in review config)
- History pruning budget defaults to 24K tokens (configurable via `max_history_tokens`)
- Added initial prompt size logging for context window diagnostics

## [0.20] - 2026-02-10

### Fixed
- **CRITICAL**: Fix startup hang on large repositories - `_init_beads_manager()` was running `bd create` as a subprocess for each of 8264 reviewable directories at startup, blocking for minutes before the review loop could start. Switched to lazy on-demand creation: beads issues are now created when the AI first SET_SCOPEs to a directory, making startup near-instant.

### Changed
- `BeadsManager.mark_in_progress/mark_open/mark_completed` now lazily create directory issues via `_ensure_directory_issue()` if they don't exist yet
- `ensure_directories()` retained only for `_rescan_for_new_directories()` (small batches during forever mode)

## [0.19] - 2026-02-10

### Fixed
- **CRITICAL**: Fix freebsd-angry-ai persona regression - persona was using Cursor-era bootstrap that lacked ACTION format documentation, causing the LLM to produce review text without ACTION: lines and the review loop to stall doing nothing
  - Rewrote `AI_START_HERE.md` with complete ACTION command documentation, inline personality, and review criteria
  - Updated `HANDOVER.md` to replace Cursor tool references (StrReplace, Write) with ACTION-based workflow
  - Preserved critical lessons learned (comment syntax, shell builtins, include ordering)
- Fix missing `shlex` import in timeout error handler (caused NameError masking real timeout message)
- Fix provider selection in vLLM client

### Changed
- Increase beads migration timeout to 1 hour (handles large repositories like freebsd-src)
- Clean up top-level directory: moved CHUNKING.md and FILE_LOCATIONS.md to docs/, removed temporary dev notes

## [0.18] - 2026-02-09

### Added
- **Model renegotiation fallback** - When a vLLM worker restarts with a different model, the client now detects the 404 "model does not exist" error, queries the server for available models, and transparently switches to the first one found instead of marking the host unhealthy
  - New `renegotiate_model()` method on `VLLMClient`
  - `VLLMModelNotFoundError` now raised (instead of `VLLMConnectionError`) for 404 model-not-found at runtime
  - Single automatic retry after renegotiation before falling through to next host

### Fixed
- Fix AttributeError in beads integration: use `mark_done` instead of `mark_completed`

## [0.17] - 2026-02-07

### Added
- **Loop Detection and Recovery System** - Prevents infinite loops when AI gets stuck
  - Action history tracking to detect repeated identical actions
  - Progressive warnings at 5 repetitions, automatic recovery at 10
  - Automatic rollback of uncommitted changes when loop detected
  - Response validation to catch truncated/incomplete AI responses
  - Special handling for READ_FILE loops (most common case)
  - **CRITICAL**: Pre-parse loop detection catches loops even when ActionParser fails
  - See `docs/LOOP-DETECTION.md` for full details

### Changed
- `ReviewSession` now tracks action history and parse failures for loop detection
- `ActionParser` now accepts both `ACTION:` and `### Action:` (markdown) formats
- `_execute_action()` now checks for loops before executing
- Main review loop validates responses AND catches unparseable loops before parsing
- Loop detection now has TWO layers: pre-parse and post-parse

### Fixed
- **CRITICAL**: AI no longer gets stuck when using wrong action format (e.g., `### Action:`)
- **CRITICAL**: AI no longer gets stuck in edit-read-edit loops (edit fails → read → edit fails)
- AI no longer gets stuck reading the same file repeatedly
- Truncated responses are detected and rejected
- System can recover automatically without human intervention
- Unparseable responses trigger loop detection after 5 repetitions (previously never caught)
- Edit failures trigger recovery after 3 attempts on same file (previously could loop indefinitely)

## [Previous Versions]

(Historical changes not yet documented)


