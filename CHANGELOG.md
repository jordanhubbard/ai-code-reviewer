# Changelog

All notable changes to the AI Code Reviewer project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Added
- **Loop Detection and Recovery System** - Prevents infinite loops when AI gets stuck
  - Action history tracking to detect repeated identical actions
  - Progressive warnings at 5 repetitions, automatic recovery at 10
  - Automatic rollback of uncommitted changes when loop detected
  - Response validation to catch truncated/incomplete AI responses
  - Special handling for READ_FILE loops (most common case)
  - See `docs/LOOP-DETECTION.md` for full details

### Changed
- `ReviewSession` now tracks action history for loop detection
- `_execute_action()` now checks for loops before executing
- Main review loop validates responses before parsing actions

### Fixed
- AI no longer gets stuck reading the same file repeatedly
- Truncated responses are detected and rejected
- System can recover automatically without human intervention

## [Previous Versions]

(Historical changes not yet documented)

