# Changelog

All notable changes to the AI Code Reviewer project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

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


