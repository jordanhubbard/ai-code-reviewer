#!/usr/bin/env python3
"""
Internal Operations Logger for AI Code Reviewer

Logs successes, failures, and metrics for debugging and improving the tool.
Data is stored in .reviewer-log/ (gitignored) or optionally synced to a
dedicated branch in the audited project.

Log Format: JSONL (one JSON object per line)
- Append-only for durability
- Easy to parse and analyze
- Can be synced to git branch if desired
"""

import datetime
import json
import os
import subprocess
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class EventType(str, Enum):
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    DIRECTORY_START = "directory_start"
    DIRECTORY_COMPLETE = "directory_complete"
    FILE_REVIEWED = "file_reviewed"
    EDIT_SUCCESS = "edit_success"
    EDIT_FAILURE = "edit_failure"
    BUILD_START = "build_start"
    BUILD_SUCCESS = "build_success"
    BUILD_FAILURE = "build_failure"
    COMMIT_SUCCESS = "commit_success"
    COMMIT_FAILURE = "commit_failure"
    PREFLIGHT_PASS = "preflight_pass"
    PREFLIGHT_FAIL = "preflight_fail"
    PREFLIGHT_RECOVERY = "preflight_recovery"
    AI_TIMEOUT = "ai_timeout"
    AI_ERROR = "ai_error"
    ERROR = "error"


@dataclass
class LogEvent:
    """A single log event."""
    event_type: EventType
    timestamp: str = field(default_factory=lambda: datetime.datetime.now().isoformat())
    session_id: Optional[str] = None
    directory: Optional[str] = None
    file_path: Optional[str] = None
    message: Optional[str] = None
    duration_seconds: Optional[float] = None
    error_count: Optional[int] = None
    warning_count: Optional[int] = None
    files_changed: Optional[List[str]] = None
    commit_hash: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    
    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d['event_type'] = self.event_type.value
        return {k: v for k, v in d.items() if v is not None}
    
    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(',', ':'))


class OpsLogger:
    """
    Operations logger for the AI code reviewer.
    
    Logs to .reviewer-log/ops.jsonl by default.
    Can optionally sync logs to a branch in the audited project.
    """
    
    def __init__(
        self,
        log_dir: Optional[Path] = None,
        session_id: Optional[str] = None,
        sync_to_branch: Optional[str] = None,
        source_root: Optional[Path] = None,
    ):
        self.log_dir = Path(log_dir) if log_dir else Path(".reviewer-log")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        self.log_file = self.log_dir / "ops.jsonl"
        self.session_id = session_id or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        
        self.sync_to_branch = sync_to_branch
        self.source_root = source_root
        
        self._current_directory: Optional[str] = None
        self._session_start: Optional[datetime.datetime] = None
        self._directory_start: Optional[datetime.datetime] = None
    
    def _write(self, event: LogEvent) -> None:
        """Append event to log file."""
        if event.session_id is None:
            event.session_id = self.session_id
        
        with open(self.log_file, 'a') as f:
            f.write(event.to_json() + '\n')
    
    def session_start(self, details: Optional[Dict[str, Any]] = None) -> None:
        """Log session start."""
        self._session_start = datetime.datetime.now()
        self._write(LogEvent(
            event_type=EventType.SESSION_START,
            message="Review session started",
            details=details,
        ))
    
    def session_end(
        self,
        directories_completed: int = 0,
        files_fixed: int = 0,
        build_failures: int = 0,
    ) -> None:
        """Log session end with summary."""
        duration = None
        if self._session_start:
            duration = (datetime.datetime.now() - self._session_start).total_seconds()
        
        self._write(LogEvent(
            event_type=EventType.SESSION_END,
            message="Review session ended",
            duration_seconds=duration,
            details={
                "directories_completed": directories_completed,
                "files_fixed": files_fixed,
                "build_failures": build_failures,
            },
        ))
        
        if self.sync_to_branch and self.source_root:
            self._sync_to_branch()
    
    def directory_start(self, directory: str) -> None:
        """Log start of directory review."""
        self._current_directory = directory
        self._directory_start = datetime.datetime.now()
        self._write(LogEvent(
            event_type=EventType.DIRECTORY_START,
            directory=directory,
            message=f"Started reviewing {directory}",
        ))
    
    def directory_complete(
        self,
        directory: str,
        files_changed: Optional[List[str]] = None,
        commit_hash: Optional[str] = None,
    ) -> None:
        """Log successful directory completion."""
        duration = None
        if self._directory_start:
            duration = (datetime.datetime.now() - self._directory_start).total_seconds()
        
        self._write(LogEvent(
            event_type=EventType.DIRECTORY_COMPLETE,
            directory=directory,
            message=f"Completed {directory}",
            duration_seconds=duration,
            files_changed=files_changed,
            commit_hash=commit_hash,
        ))
        self._directory_start = None
    
    def file_reviewed(self, file_path: str, chunks: int = 1) -> None:
        """Log file review completion."""
        self._write(LogEvent(
            event_type=EventType.FILE_REVIEWED,
            directory=self._current_directory,
            file_path=file_path,
            details={"chunks": chunks},
        ))
    
    def edit_success(self, file_path: str, change_summary: Optional[str] = None) -> None:
        """Log successful file edit."""
        self._write(LogEvent(
            event_type=EventType.EDIT_SUCCESS,
            directory=self._current_directory,
            file_path=file_path,
            message=change_summary,
        ))
    
    def edit_failure(self, file_path: str, error: str) -> None:
        """Log failed file edit."""
        self._write(LogEvent(
            event_type=EventType.EDIT_FAILURE,
            directory=self._current_directory,
            file_path=file_path,
            message=error,
        ))
    
    def build_start(self, build_command: Optional[str] = None) -> None:
        """Log build start with timestamp."""
        self._write(LogEvent(
            event_type=EventType.BUILD_START,
            directory=self._current_directory,
            message=f"Build started: {build_command}" if build_command else "Build started",
        ))

    def build_success(
        self,
        duration_seconds: float,
        warning_count: int = 0,
    ) -> None:
        """Log successful build."""
        self._write(LogEvent(
            event_type=EventType.BUILD_SUCCESS,
            directory=self._current_directory,
            duration_seconds=duration_seconds,
            warning_count=warning_count,
        ))
    
    def build_failure(
        self,
        duration_seconds: float,
        error_count: int = 0,
        warning_count: int = 0,
        error_summary: Optional[str] = None,
    ) -> None:
        """Log build failure."""
        self._write(LogEvent(
            event_type=EventType.BUILD_FAILURE,
            directory=self._current_directory,
            duration_seconds=duration_seconds,
            error_count=error_count,
            warning_count=warning_count,
            message=error_summary,
        ))
    
    def commit_success(self, commit_hash: str, files_changed: List[str]) -> None:
        """Log successful commit."""
        self._write(LogEvent(
            event_type=EventType.COMMIT_SUCCESS,
            directory=self._current_directory,
            commit_hash=commit_hash,
            files_changed=files_changed,
        ))
    
    def commit_failure(self, error: str) -> None:
        """Log commit failure."""
        self._write(LogEvent(
            event_type=EventType.COMMIT_FAILURE,
            directory=self._current_directory,
            message=error,
        ))
    
    def preflight_pass(self, duration_seconds: float, warning_count: int = 0) -> None:
        """Log preflight check pass."""
        self._write(LogEvent(
            event_type=EventType.PREFLIGHT_PASS,
            duration_seconds=duration_seconds,
            warning_count=warning_count,
        ))
    
    def preflight_fail(
        self,
        error_count: int,
        warning_count: int,
        error_summary: Optional[str] = None,
    ) -> None:
        """Log preflight check failure."""
        self._write(LogEvent(
            event_type=EventType.PREFLIGHT_FAIL,
            error_count=error_count,
            warning_count=warning_count,
            message=error_summary,
        ))
    
    def preflight_recovery(
        self,
        commits_reverted: int,
        recovered_commit: str,
    ) -> None:
        """Log preflight recovery success."""
        self._write(LogEvent(
            event_type=EventType.PREFLIGHT_RECOVERY,
            commit_hash=recovered_commit,
            details={"commits_reverted": commits_reverted},
            message=f"Recovered after reverting {commits_reverted} commits",
        ))
    
    def ai_timeout(self, timeout_seconds: float, context: Optional[str] = None) -> None:
        """Log AI timeout."""
        self._write(LogEvent(
            event_type=EventType.AI_TIMEOUT,
            directory=self._current_directory,
            duration_seconds=timeout_seconds,
            message=context,
        ))
    
    def ai_error(self, error: str) -> None:
        """Log AI error."""
        self._write(LogEvent(
            event_type=EventType.AI_ERROR,
            directory=self._current_directory,
            message=error,
        ))
    
    def error(self, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        """Log generic error."""
        self._write(LogEvent(
            event_type=EventType.ERROR,
            directory=self._current_directory,
            message=message,
            details=details,
        ))
    
    def _sync_to_branch(self) -> None:
        """
        Sync logs to a dedicated branch in the audited project.
        Uses git worktree similar to beads sync.
        """
        if not self.sync_to_branch or not self.source_root:
            return
        
        branch = self.sync_to_branch
        worktree_path = self.source_root / ".git" / "reviewer-log-worktree"
        
        try:
            if not worktree_path.exists():
                subprocess.run(
                    ['git', 'worktree', 'add', '--orphan', '-b', branch, str(worktree_path)],
                    cwd=str(self.source_root),
                    capture_output=True,
                    check=True,
                )
            
            import shutil
            dest = worktree_path / "ops.jsonl"
            shutil.copy2(self.log_file, dest)
            
            subprocess.run(
                ['git', 'add', 'ops.jsonl'],
                cwd=str(worktree_path),
                capture_output=True,
            )
            subprocess.run(
                ['git', 'commit', '-m', f'Sync reviewer log - {self.session_id}'],
                cwd=str(worktree_path),
                capture_output=True,
            )
            subprocess.run(
                ['git', 'push', '-u', 'origin', branch],
                cwd=str(worktree_path),
                capture_output=True,
            )
        except Exception as e:
            print(f"Warning: Could not sync logs to branch {branch}: {e}")
    
    @classmethod
    def read_log(cls, log_file: Path) -> List[Dict[str, Any]]:
        """Read and parse a log file."""
        events = []
        if log_file.exists():
            with open(log_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        return events
    
    @classmethod
    def get_summary(cls, log_file: Path) -> Dict[str, Any]:
        """Get summary statistics from a log file."""
        events = cls.read_log(log_file)
        
        summary = {
            "total_events": len(events),
            "sessions": 0,
            "directories_completed": 0,
            "build_starts": 0,
            "build_successes": 0,
            "build_failures": 0,
            "edit_successes": 0,
            "edit_failures": 0,
            "ai_timeouts": 0,
            "ai_errors": 0,
        }
        
        for e in events:
            event_type = e.get("event_type", "")
            if event_type == "session_start":
                summary["sessions"] += 1
            elif event_type == "directory_complete":
                summary["directories_completed"] += 1
            elif event_type == "build_start":
                summary["build_starts"] += 1
            elif event_type == "build_success":
                summary["build_successes"] += 1
            elif event_type == "build_failure":
                summary["build_failures"] += 1
            elif event_type == "edit_success":
                summary["edit_successes"] += 1
            elif event_type == "edit_failure":
                summary["edit_failures"] += 1
            elif event_type == "ai_timeout":
                summary["ai_timeouts"] += 1
            elif event_type == "ai_error":
                summary["ai_errors"] += 1
        
        return summary


def create_logger_from_config(
    config: Dict[str, Any],
    session_id: Optional[str] = None,
    source_root: Optional[Path] = None,
) -> OpsLogger:
    """Create an OpsLogger from configuration."""
    ops_config = config.get('ops_logging', {})
    
    log_dir = ops_config.get('log_dir', '.reviewer-log')
    if not Path(log_dir).is_absolute():
        log_dir = Path.cwd() / log_dir
    
    sync_to_branch = ops_config.get('sync_to_branch')
    
    return OpsLogger(
        log_dir=Path(log_dir),
        session_id=session_id,
        sync_to_branch=sync_to_branch,
        source_root=source_root,
    )


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "summary":
        log_file = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(".reviewer-log/ops.jsonl")
        summary = OpsLogger.get_summary(log_file)
        print(json.dumps(summary, indent=2))
    else:
        print("Usage: python ops_logger.py summary [log_file]")
        print("\nThis module provides internal operations logging for the AI code reviewer.")
        print("It logs successes, failures, and metrics for debugging and improvement.")
