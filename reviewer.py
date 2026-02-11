#!/usr/bin/env python3
"""
Angry AI Reviewer

Main application for the FreeBSD code review agent.
Implements the review → edit → build → fix loop using a remote Ollama server.

Usage:
    python reviewer.py [--config config.yaml]
    
Architecture:
    1. Load configuration from config.yaml
    2. Connect to remote Ollama server and validate model
    3. Load persona and review instructions
    4. Run review loop:
       - AI reviews code and suggests edits
       - Apply edits to source files
       - Show git diff to confirm changes
       - Run build (make buildworld) with live output
       - If errors: feed back to AI for fixing, update LESSONS.md
       - If success: AI generates commit message, update REVIEW-SUMMARY.md, push
"""

import argparse
import datetime
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from difflib import SequenceMatcher

from index_generator import generate_index
from build_executor import BuildResult
from chunker import get_chunker, format_chunk_for_review
from dataclasses import dataclass, field
from ops_logger import OpsLogger, create_logger_from_config
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple, Set
import json

# Import new validation and metrics modules
from persona_validator import PersonaValidator
from build_validator import BuildValidator
from persona_metrics import PersonaMetricsTracker

logger = logging.getLogger(__name__)

# File types considered "text" for review workflows
# IMPORTANT: Only include ACTUAL SOURCE CODE file types here
# Test data files (.in, .ok, .out, .err, .txt) should NOT be reviewed
REVIEWABLE_SUFFIXES = {
    # C/C++ source and headers
    '.c', '.h', '.cc', '.cpp', '.cxx', '.hpp', '.hxx',
    # Assembly
    '.s', '.S',
    # Shell scripts
    '.sh', '.bash', '.ksh', '.zsh',
    # Python
    '.py',
    # Scripting languages
    '.awk', '.sed', '.perl', '.pl',
    # Build system files
    '.mk', '.cmake',
    # Template files
    '.m4',
    # Rust
    '.rs',
    # Go
    '.go',
    # Objective-C
    '.m', '.mm',
    # Man pages
    '.1', '.2', '.3', '.4', '.5', '.6', '.7', '.8', '.9', '.mdoc',
    # Lex/Yacc
    '.l', '.y', '.ll', '.yy',
    # Documentation (only markdown for inline docs)
    '.md',
}

# Test data and output files that should NEVER be reviewed
EXCLUDED_SUFFIXES = {
    '.in', '.ok', '.out', '.err', '.txt', '.log', '.dat', '.data',
    '.expected', '.actual', '.diff', '.orig', '.rej', '.bak',
    '.golden', '.baseline', '.result', '.output', '.input',
}

REVIEWABLE_SPECIAL_FILES = {
    'Makefile', 'Makefile.inc', 'BSDmakefile', 'README', 'README.md'
}

MANPAGE_SUFFIXES = {'.1', '.2', '.3', '.4', '.5', '.6', '.7', '.8', '.9', '.mdoc'}

# Used to collapse large code blocks in console output while keeping logs intact
CODE_BLOCK_RE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
COMMIT_PREFIX = "[ai-code-reviewer] "


def load_yaml_config(config_path: Path) -> Dict[str, Any]:
    """Load YAML configuration file with friendly error messages."""
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not installed, using basic parser (install with: pip install pyyaml)")
        return _basic_yaml_parse(config_path)
    
    try:
        with open(config_path, 'r') as f:
            content = f.read()
    except FileNotFoundError:
        print(f"\n{'='*60}")
        print("ERROR: Configuration file not found")
        print(f"{'='*60}")
        print(f"File: {config_path}")
        print(f"\nRun: cp config.yaml.defaults config.yaml")
        print(f"{'='*60}\n")
        sys.exit(1)
    except Exception as e:
        print(f"\n{'='*60}")
        print("ERROR: Cannot read configuration file")
        print(f"{'='*60}")
        print(f"File: {config_path}")
        print(f"Error: {e}")
        print(f"{'='*60}\n")
        sys.exit(1)
    
    try:
        return yaml.safe_load(content)
    except yaml.scanner.ScannerError as e:
        _print_yaml_error(config_path, content, e)
        sys.exit(1)
    except yaml.parser.ParserError as e:
        _print_yaml_error(config_path, content, e)
        sys.exit(1)
    except yaml.YAMLError as e:
        _print_yaml_error(config_path, content, e)
        sys.exit(1)


def _print_yaml_error(config_path: Path, content: str, error: Exception) -> None:
    """Print a friendly YAML error message with context."""
    print(f"\n{'='*60}")
    print("ERROR: Invalid YAML in configuration file")
    print(f"{'='*60}")
    print(f"File: {config_path}")
    print(f"\n{error}")
    
    # Try to extract line number from error
    error_str = str(error)
    line_num = None
    
    # Look for "line X" pattern in error message
    import re
    line_match = re.search(r'line (\d+)', error_str)
    if line_match:
        line_num = int(line_match.group(1))
    
    # Check for common issues
    lines = content.split('\n')
    issues_found = []
    
    # Check for tabs (most common issue)
    for i, line in enumerate(lines, 1):
        if '\t' in line:
            col = line.index('\t') + 1
            issues_found.append(f"  Line {i}, col {col}: Tab character found (use spaces instead)")
    
    if issues_found:
        print(f"\n{'='*60}")
        print("ISSUES DETECTED:")
        print(f"{'='*60}")
        for issue in issues_found[:5]:  # Show first 5 issues
            print(issue)
        if len(issues_found) > 5:
            print(f"  ... and {len(issues_found) - 5} more")
    
    # Show context around the error line
    if line_num and 1 <= line_num <= len(lines):
        print(f"\n{'='*60}")
        print(f"CONTEXT (around line {line_num}):")
        print(f"{'='*60}")
        start = max(0, line_num - 3)
        end = min(len(lines), line_num + 2)
        for i in range(start, end):
            marker = ">>>" if i == line_num - 1 else "   "
            # Show tabs visibly
            display_line = lines[i].replace('\t', '→→→→')
            print(f"{marker} {i+1:4d} | {display_line}")
    
    print(f"\n{'='*60}")
    print("FIX:")
    print(f"{'='*60}")
    print("  1. Open the file in your editor")
    print("  2. Replace all tabs with spaces (YAML requires spaces)")
    print("  3. Check indentation is consistent (2 spaces recommended)")
    print(f"\n  Quick fix: sed -i 's/\\t/  /g' {config_path}")
    print(f"{'='*60}\n")


def _basic_yaml_parse(config_path: Path) -> Dict[str, Any]:
    """Basic YAML parser for simple key-value configs."""
    result = {}
    current_dict = result
    indent_stack = [(0, result)]
    
    with open(config_path, 'r') as f:
        for line in f:
            stripped = line.lstrip()
            if not stripped or stripped.startswith('#'):
                continue
            
            indent = len(line) - len(stripped)
            
            while indent_stack and indent <= indent_stack[-1][0]:
                if len(indent_stack) > 1:
                    indent_stack.pop()
            
            current_dict = indent_stack[-1][1]
            
            if ':' in stripped:
                key, _, value = stripped.partition(':')
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                
                if value:
                    try:
                        if '.' in value:
                            value = float(value)
                        else:
                            value = int(value)
                    except ValueError:
                        pass
                    current_dict[key] = value
                else:
                    current_dict[key] = {}
                    indent_stack.append((indent + 2, current_dict[key]))
    
    return result


@dataclass
class ReviewSession:
    """Tracks state of a review session with hierarchical progress."""
    session_id: str
    start_time: datetime.datetime
    
    # Hierarchy: Source Tree → Directory → File → Chunk
    directories_completed: int = 0
    files_fixed: int = 0
    build_failures: int = 0
    total_errors_fixed: int = 0
    
    # Current location in hierarchy
    current_directory: Optional[str] = None  # e.g., "bin/chio"
    current_file: Optional[str] = None  # e.g., "bin/chio/chio.c"
    current_file_chunks_total: int = 0  # Total chunks in current file
    current_file_chunks_reviewed: int = 0  # Chunks reviewed so far
    files_in_current_directory: List[str] = field(default_factory=list)
    files_reviewed_in_directory: int = 0
    visited_files_in_directory: Set[str] = field(default_factory=set)
    
    # Changes tracking (accumulated until BUILD)
    pending_changes: bool = False
    last_diff: str = ""
    changed_files: List[str] = field(default_factory=list)
    completed_directories: List[str] = field(default_factory=list)
    
    # Loop detection
    action_history: List[Tuple[str, str]] = field(default_factory=list)  # (action_type, key_info)
    last_action_hash: Optional[str] = None
    consecutive_identical_actions: int = 0
    consecutive_parse_failures: int = 0
    last_failed_response: str = ""
    
    # Edit failure loop detection
    edit_failure_count: int = 0  # Consecutive EDIT_FILE failures
    last_failed_edit_file: Optional[str] = None
    
    def get_progress_summary(self) -> str:
        """Get hierarchical progress summary."""
        lines = []
        if self.current_directory:
            lines.append(f"📁 Directory: {self.current_directory}")
            if self.files_in_current_directory:
                lines.append(f"   Files: {self.files_reviewed_in_directory}/{len(self.files_in_current_directory)}")
        if self.current_file:
            lines.append(f"📄 File: {self.current_file}")
            if self.current_file_chunks_total > 1:
                lines.append(f"   Chunks: {self.current_file_chunks_reviewed}/{self.current_file_chunks_total}")
        if self.changed_files:
            lines.append(f"✏️  Edits: {len(self.changed_files)} files modified")
        return "\n".join(lines) if lines else "No active review"


class GitCommandError(RuntimeError):
    """Raised when a git command fails."""


class SecretScanner:
    """Scans git diffs for potentially sensitive information before commits."""
    
    # Patterns for common secret types
    PATTERNS = [
        # API Keys and Tokens
        (r'["\']?api[_-]?key["\']?\s*[:=]\s*["\']([A-Za-z0-9_\-]{20,})["\']', 'API Key'),
        (r'["\']?api[_-]?secret["\']?\s*[:=]\s*["\']([A-Za-z0-9_\-]{20,})["\']', 'API Secret'),
        (r'["\']?auth[_-]?token["\']?\s*[:=]\s*["\']([A-Za-z0-9_\-]{20,})["\']', 'Auth Token'),
        (r'["\']?access[_-]?token["\']?\s*[:=]\s*["\']([A-Za-z0-9_\-]{20,})["\']', 'Access Token'),
        (r'["\']?bearer["\']?\s*[:=]\s*["\']([A-Za-z0-9_\-]{20,})["\']', 'Bearer Token'),
        
        # AWS Credentials
        (r'AKIA[0-9A-Z]{16}', 'AWS Access Key ID'),
        (r'["\']?aws[_-]?secret["\']?\s*[:=]\s*["\']([A-Za-z0-9/+=]{40})["\']', 'AWS Secret Access Key'),
        
        # Private Keys
        (r'-----BEGIN (RSA |DSA |EC |OPENSSH )?PRIVATE KEY-----', 'Private Key'),
        (r'-----BEGIN (PGP|SSH2) PRIVATE KEY', 'Private Key'),
        
        # Passwords (only when obviously sensitive)
        (r'["\']?password["\']?\s*[:=]\s*["\']([^"\'\s]{8,})["\']', 'Hardcoded Password'),
        (r'["\']?passwd["\']?\s*[:=]\s*["\']([^"\'\s]{8,})["\']', 'Hardcoded Password'),
        (r'["\']?pwd["\']?\s*[:=]\s*["\']([^"\'\s]{8,})["\']', 'Hardcoded Password'),
        
        # Database Connection Strings
        (r'(mysql|postgresql|mongodb|redis)://[^:]+:[^@]+@', 'Database Credentials'),
        (r'jdbc:[a-z]+://[^:]+:[^@]+@', 'JDBC Credentials'),
        
        # OAuth and Client Secrets
        (r'["\']?client[_-]?secret["\']?\s*[:=]\s*["\']([A-Za-z0-9_\-]{20,})["\']', 'Client Secret'),
        (r'["\']?oauth[_-]?token["\']?\s*[:=]\s*["\']([A-Za-z0-9_\-]{20,})["\']', 'OAuth Token'),
        
        # GitHub/GitLab Tokens
        (r'gh[pousr]_[A-Za-z0-9]{36,}', 'GitHub Token'),
        (r'glpat-[A-Za-z0-9_\-]{20,}', 'GitLab Token'),
        
        # Generic High-Entropy Strings (base64-like)
        (r'["\']([A-Za-z0-9+/]{40,}={0,2})["\']', 'High-Entropy String (possible secret)'),
    ]
    
    # False positive patterns to exclude
    EXCLUDE_PATTERNS = [
        r'example\.com',
        r'placeholder',
        r'your[_-]?(api|secret|key|token)',
        r'INSERT[_-]?(YOUR|API|SECRET|KEY|TOKEN)',
        r'xxxx+',
        r'\*\*\*\*+',
        r'test[_-]?(key|secret|token)',
        r'fake[_-]?(key|secret|token)',
        r'dummy[_-]?(key|secret|token)',
    ]
    
    @classmethod
    def scan_diff(cls, diff_output: str) -> List[Tuple[str, str, str]]:
        """
        Scan git diff for potential secrets.
        
        Args:
            diff_output: Output from 'git diff' command
            
        Returns:
            List of (file_path, secret_type, matched_text) tuples
        """
        findings = []
        current_file = None
        
        for line in diff_output.split('\n'):
            # Track current file being diffed
            if line.startswith('+++'):
                current_file = line.split()[-1].lstrip('b/')
                continue
            
            # Only scan added lines (those starting with +)
            if not line.startswith('+') or line.startswith('+++'):
                continue
            
            # Remove the + prefix
            content = line[1:]
            
            # Check against each pattern
            for pattern, secret_type in cls.PATTERNS:
                matches = re.finditer(pattern, content, re.IGNORECASE)
                for match in matches:
                    matched_text = match.group(0)
                    
                    # Check if it's a false positive
                    is_false_positive = any(
                        re.search(exclude, matched_text, re.IGNORECASE)
                        for exclude in cls.EXCLUDE_PATTERNS
                    )
                    
                    if not is_false_positive:
                        findings.append((current_file or 'unknown', secret_type, matched_text))
        
        return findings
    
    @classmethod
    def format_findings(cls, findings: List[Tuple[str, str, str]]) -> str:
        """Format scan findings as a readable report."""
        if not findings:
            return ""
        
        lines = [
            "=" * 70,
            "⚠️  POTENTIAL SECRETS DETECTED IN COMMIT",
            "=" * 70,
            "",
            f"Found {len(findings)} potential secret(s):",
            ""
        ]
        
        for i, (file_path, secret_type, matched_text) in enumerate(findings, 1):
            # Redact the middle of the matched text for display
            if len(matched_text) > 20:
                redacted = matched_text[:8] + "..." + matched_text[-8:]
            else:
                redacted = matched_text[:4] + "..." + matched_text[-4:]
            
            lines.append(f"[{i}] {file_path}")
            lines.append(f"    Type: {secret_type}")
            lines.append(f"    Match: {redacted}")
            lines.append("")
        
        lines.extend([
            "=" * 70,
            "COMMIT BLOCKED FOR SAFETY",
            "=" * 70,
            "",
            "If these are false positives:",
            "1. Review the patterns in SecretScanner.PATTERNS",
            "2. Add exclusions to SecretScanner.EXCLUDE_PATTERNS",
            "3. Or manually commit with git (bypassing this tool)",
            "",
            "If these ARE secrets:",
            "1. Remove them from the code",
            "2. Use environment variables or config files (gitignored)",
            "3. Rotate any exposed credentials immediately",
            ""
        ])
        
        return '\n'.join(lines)


class GitHelper:
    """Helper for git operations."""
    
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self._git_dir = repo_root / '.git'
        self.secret_scanner = SecretScanner()
    
    def _run(self, args: List[str], capture: bool = True) -> Tuple[int, str]:
        """Run a git command and return (returncode, output)."""
        cmd = ['git', '-C', str(self.repo_root)] + args
        if capture:
            result = subprocess.run(cmd, capture_output=True, text=True)
            return result.returncode, (result.stdout + result.stderr).strip()
        else:
            result = subprocess.run(cmd)
            return result.returncode, ""

    def _path_exists(self, relative: str) -> bool:
        if not self._git_dir.exists():
            return False
        return (self._git_dir / relative).exists()

    def _resolve_branch_ref(self, branch: str) -> Optional[str]:
        if not branch:
            return None
        code, _ = self._run(['show-ref', '--verify', f'refs/heads/{branch}'])
        if code == 0:
            return branch
        code, _ = self._run(['show-ref', '--verify', f'refs/remotes/origin/{branch}'])
        if code == 0:
            return f'origin/{branch}'
        return None

    def _make_fallback_branch(self, base_branch: str) -> str:
        timestamp = datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')
        safe_base = re.sub(r'[^A-Za-z0-9._/-]+', '-', base_branch or 'detached')
        return f'reviewer/{safe_base}-{timestamp}'

    def _get_worktree_branch_paths(self) -> Dict[str, str]:
        code, output = self._run(['worktree', 'list', '--porcelain'])
        if code != 0 or not output:
            return {}
        entries: List[Dict[str, str]] = []
        current: Dict[str, str] = {}
        for line in output.splitlines():
            if line.startswith('worktree '):
                if current:
                    entries.append(current)
                current = {'path': line.split(' ', 1)[1].strip()}
            elif line.startswith('branch '):
                current['branch'] = line.split(' ', 1)[1].strip()
        if current:
            entries.append(current)
        return {
            entry['branch']: entry['path']
            for entry in entries
            if entry.get('branch') and entry.get('path')
        }

    def _get_worktree_path_for_branch(self, branch: str) -> Optional[str]:
        if not branch:
            return None
        branch_ref = f'refs/heads/{branch}'
        return self._get_worktree_branch_paths().get(branch_ref)

    def _tool_paths_for_checkout(self) -> List[str]:
        return [
            '.beads/',
            '.ai-code-reviewer/',
            '.angry-ai/',
            'REVIEW-INDEX.md',
        ]

    def _should_stash_tool_paths(self, output: str) -> bool:
        return 'untracked working tree files would be overwritten by checkout' in output

    def _stash_tool_paths_for_checkout(self) -> Tuple[bool, Optional[str]]:
        paths = self._tool_paths_for_checkout()
        existing = [path for path in paths if (self.repo_root / path).exists()]
        if not existing:
            return True, 'no tool files to stash'
        code, output = self._run([
            'stash',
            'push',
            '--all',
            '-m',
            'reviewer-prep-tool-files',
            '--',
            *existing,
        ])
        if code != 0 and 'No local changes to save' not in output:
            return False, f'Failed to stash tool files: {output}'
        return True, 'stashed tool files'

    def has_rebase_in_progress(self) -> bool:
        return any(self._path_exists(name) for name in ['rebase-apply', 'rebase-merge'])

    def has_merge_in_progress(self) -> bool:
        return self._path_exists('MERGE_HEAD')

    def abort_rebase_if_needed(self) -> Tuple[bool, Optional[str]]:
        if not self.has_rebase_in_progress():
            return True, None
        code, output = self._run(['rebase', '--abort'])
        if code == 0:
            return True, 'aborted incomplete rebase'
        return False, f'Failed to abort rebase: {output}'

    def abort_merge_if_needed(self) -> Tuple[bool, Optional[str]]:
        if not self.has_merge_in_progress():
            return True, None
        code, output = self._run(['merge', '--abort'])
        if code == 0:
            return True, 'aborted unfinished merge'
        return False, f'Failed to abort merge: {output}'

    def get_current_branch(self) -> Optional[str]:
        code, output = self._run(['rev-parse', '--abbrev-ref', 'HEAD'])
        if code != 0:
            return None
        return output.strip()

    def get_default_remote_branch(self) -> str:
        code, output = self._run(['symbolic-ref', 'refs/remotes/origin/HEAD'])
        if code == 0 and output.strip():
            return output.strip().split('/')[-1]
        return 'main'

    def get_upstream_ref(self, fallback_branch: Optional[str] = None) -> Optional[str]:
        code, output = self._run(['rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}'])
        if code == 0 and output.strip():
            return output.strip()
        branch = fallback_branch or self.get_default_remote_branch()
        return f'origin/{branch}' if branch else None

    def ensure_repository_ready(self, preferred_branch: str = 'main', allow_rebase: bool = False) -> Tuple[bool, str]:
        """Abort unfinished operations, ensure on a branch, optionally sync with upstream."""
        actions: List[str] = []

        ok, msg = self.abort_rebase_if_needed()
        if not ok:
            return False, msg or 'Failed to abort rebase'
        if msg:
            actions.append(msg)

        ok, msg = self.abort_merge_if_needed()
        if not ok:
            return False, msg or 'Failed to abort merge'
        if msg:
            actions.append(msg)

        branch = self.get_current_branch()
        target_branch = preferred_branch or self.get_default_remote_branch()
        if branch == 'HEAD' or not branch:
            worktree_path = self._get_worktree_path_for_branch(target_branch)
            if worktree_path:
                base_ref = self._resolve_branch_ref(target_branch) or 'HEAD'
                fallback_branch = self._make_fallback_branch(target_branch)
                code, output = self._run(['checkout', '-b', fallback_branch, base_ref])
                if code != 0 and self._should_stash_tool_paths(output):
                    ok, stash_msg = self._stash_tool_paths_for_checkout()
                    if not ok:
                        return False, stash_msg or f'Failed to stash tool files: {output}'
                    actions.append(stash_msg)
                    code, output = self._run(['checkout', '-b', fallback_branch, base_ref])
                if code != 0:
                    return False, f'Failed to checkout {fallback_branch} from {base_ref}: {output}'
                actions.append(
                    f'checked out {fallback_branch} (branch in worktree at {worktree_path})'
                )
            else:
                code, output = self._run(['checkout', target_branch])
                if code != 0 and self._should_stash_tool_paths(output):
                    ok, stash_msg = self._stash_tool_paths_for_checkout()
                    if not ok:
                        return False, stash_msg or f'Failed to stash tool files: {output}'
                    actions.append(stash_msg)
                    code, output = self._run(['checkout', target_branch])
                if code != 0:
                    if ('already used by worktree' in output
                            or 'already checked out at' in output):
                        base_ref = self._resolve_branch_ref(target_branch) or 'HEAD'
                        fallback_branch = self._make_fallback_branch(target_branch)
                        code, output = self._run(['checkout', '-b', fallback_branch, base_ref])
                        if code != 0 and self._should_stash_tool_paths(output):
                            ok, stash_msg = self._stash_tool_paths_for_checkout()
                            if not ok:
                                return False, stash_msg or f'Failed to stash tool files: {output}'
                            actions.append(stash_msg)
                            code, output = self._run(['checkout', '-b', fallback_branch, base_ref])
                        if code != 0:
                            return False, f'Failed to checkout {fallback_branch} from {base_ref}: {output}'
                        actions.append(f'checked out {fallback_branch} (from {base_ref})')
                    else:
                        return False, f'Failed to checkout {target_branch}: {output}'
                else:
                    actions.append(f'checked out {target_branch}')
        else:
            target_branch = branch

        # Ensure there are no unmerged files lingering
        code, status = self._run(['status', '--short'])
        if code != 0:
            return False, status or 'git status failed'
        unmerged = [line[3:] for line in status.splitlines() if line.startswith(('UU ', 'AA ', 'DD '))]
        if unmerged:
            return False, f'Unmerged files present: {", ".join(unmerged)}'

        can_rebase = allow_rebase and not self.has_changes()
        if can_rebase:
            upstream = self.get_upstream_ref(target_branch)
            if upstream:
                self._run(['fetch', 'origin'])
                code, counts = self._run(['rev-list', '--left-right', '--count', f'{upstream}...HEAD'])
                if code == 0:
                    parts = counts.strip().split()
                    behind = int(parts[0]) if parts else 0
                    if behind > 0:
                        code, output = self._run(['pull', '--rebase'])
                        if code != 0:
                            self.abort_rebase_if_needed()
                            return False, f'Failed to rebase onto {upstream}: {output}'
                        actions.append(f'rebased onto {upstream}')

        if not actions:
            return True, 'repository already clean'
        return True, '; '.join(actions)
    
    def has_changes(self) -> bool:
        """Check if there are uncommitted changes."""
        code, output = self._run(['status', '--porcelain'])
        return bool(output.strip())
    
    def diff(self, file_path: Optional[str] = None) -> str:
        """Get diff of changes."""
        args = ['diff']
        if file_path:
            args.append(file_path)
        code, output = self._run(args)
        return output
    
    def diff_staged(self) -> str:
        """Get diff of staged changes."""
        code, output = self._run(['diff', '--staged'])
        return output
    
    def diff_all(self) -> str:
        """Get diff of all changes (staged and unstaged)."""
        code, output = self._run(['diff', 'HEAD'])
        return output
    
    def add(self, *paths: str) -> bool:
        """Stage files for commit."""
        code, _ = self._run(['add'] + list(paths))
        return code == 0
    
    def add_all(self) -> bool:
        """Stage all changes."""
        code, _ = self._run(['add', '-A'])
        return code == 0

    def ensure_commit_prefix(self, message: str) -> str:
        """Ensure commit messages are prefixed for traceability."""
        trimmed = (message or "").strip("\n")
        if not trimmed.strip():
            return f"{COMMIT_PREFIX}Update"
        lines = trimmed.splitlines()
        first_line = lines[0].strip()
        if first_line.lower().startswith(COMMIT_PREFIX.lower()):
            return trimmed
        lines[0] = f"{COMMIT_PREFIX}{first_line}"
        return "\n".join(lines)
    
    def commit(self, message: str, skip_secret_scan: bool = False) -> Tuple[bool, str]:
        """
        Commit staged changes after scanning for secrets.
        
        Args:
            message: Commit message
            skip_secret_scan: Set to True to bypass secret scanning (use with caution!)
            
        Returns:
            Tuple of (success, output/error_message)
        """
        message = self.ensure_commit_prefix(message)
        # Scan staged changes for potential secrets before committing
        if not skip_secret_scan:
            staged_diff = self.diff_staged()
            if staged_diff:
                findings = self.secret_scanner.scan_diff(staged_diff)
                if findings:
                    # Secrets detected - block the commit
                    error_report = self.secret_scanner.format_findings(findings)
                    logger.error(f"Commit blocked: {len(findings)} potential secrets detected")
                    print("\n" + error_report)
                    return False, error_report
        
        code, output = self._run(['commit', '-m', message])
        return code == 0, output
    
    def push(self) -> Tuple[bool, str]:
        """Push to origin."""
        code, output = self._run(['push'])
        return code == 0, output
    
    def pull_rebase(self) -> Tuple[bool, str]:
        """Pull with rebase from origin."""
        code, output = self._run(['pull', '--rebase'])
        return code == 0, output
    
    def show_status(self) -> str:
        """Get git status."""
        code, output = self._run(['status', '--short'])
        if code != 0:
            raise GitCommandError(output.strip() or "git status --short failed")
        return output
    
    def changed_files_list(self) -> List[str]:
        """Get list of changed files."""
        code, output = self._run(['diff', '--name-only', 'HEAD'])
        if output:
            return [f.strip() for f in output.split('\n') if f.strip()]
        return []
    
    def is_ignored(self, path: str) -> bool:
        """
        Check if a path is ignored by .gitignore.
        
        Args:
            path: Relative path from repo root
            
        Returns:
            True if the path is ignored, False otherwise
        """
        # Always ignore .git directory
        if path.startswith('.git/') or path == '.git':
            return True
        
        # Use git check-ignore to respect all gitignore rules
        code, _ = self._run(['check-ignore', '-q', path])
        return code == 0
    
    def list_tracked_files(self, directory: str = '.') -> List[str]:
        """
        List files in a directory that are tracked by git (respects .gitignore).
        
        Args:
            directory: Directory to list, relative to repo root
            
        Returns:
            List of file paths relative to repo root
        """
        # Use git ls-files to get only tracked/trackable files
        code, output = self._run(['ls-files', '--cached', '--others', '--exclude-standard', directory])
        if code == 0 and output:
            return [f.strip() for f in output.split('\n') if f.strip()]
        return []
    
    def list_unignored_files_in_dir(self, directory: str) -> List[str]:
        """
        List files in a directory that are not ignored by .gitignore.
        
        This is useful for discovering reviewable files while respecting
        the project's .gitignore patterns.
        
        Args:
            directory: Directory path relative to repo root
            
        Returns:
            List of file paths relative to repo root
        """
        dir_path = self.repo_root / directory
        if not dir_path.exists() or not dir_path.is_dir():
            return []
        
        files = []
        try:
            for item in dir_path.iterdir():
                if not item.is_file():
                    continue
                rel_path = str(item.relative_to(self.repo_root))
                
                # Always skip .git
                if rel_path.startswith('.git/') or rel_path == '.git':
                    continue
                
                # Check if ignored
                if not self.is_ignored(rel_path):
                    files.append(rel_path)
        except PermissionError:
            pass
        
        return sorted(files)

    def recover_repository(self) -> bool:
        """Attempt automatic recovery from corrupt git state."""
        print("\n*** AUTOMATED GIT RECOVERY INITIATED ***")
        success = True

        def _run_step(description: str, args: List[str], ignore_failure: bool = False) -> bool:
            print(f"  - {description} ({' '.join(['git'] + args)})")
            code, output = self._run(args)
            if code != 0:
                print(f"    WARNING: Command failed with exit {code}: {output}")
                if not ignore_failure:
                    return False
            return True

        # Step 1: remove untracked files that block resets
        _run_step("Removing untracked files", ['clean', '-fdx'], ignore_failure=True)

        # Step 2: abort any in-progress rebase/merge
        _run_step("Aborting unfinished rebase", ['rebase', '--abort'], ignore_failure=True)

        # Determine upstream
        upstream_ref = 'origin/main'
        code, upstream = self._run(['rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}'])
        if code == 0 and upstream.strip():
            upstream_ref = upstream.strip()
        else:
            print("    NOTE: Unable to detect upstream branch automatically; defaulting to origin/main")

        # Step 3: fetch latest from origin
        if not _run_step("Fetching latest refs", ['fetch', 'origin'], ignore_failure=False):
            success = False

        # Step 4: hard reset to upstream
        reset_ok = _run_step(f"Resetting to {upstream_ref}", ['reset', '--hard', upstream_ref])
        if not reset_ok:
            success = False
            if upstream_ref != 'origin/main':
                print("    Attempting fallback reset to origin/main ...")
                if _run_step("Resetting to origin/main", ['reset', '--hard', 'origin/main']):
                    success = True

        print("*** AUTOMATED GIT RECOVERY {} ***".format("SUCCEEDED" if success else "FAILED"))
        return success


class BeadsMigrationError(RuntimeError):
    """Raised when beads migration is required but cannot proceed safely."""


class BeadsManager:
    """Lightweight wrapper around the bd CLI for directory tracking."""

    def __init__(
        self,
        source_root: Path,
        tool_root: Optional[Path] = None,
        git_helper: Optional['GitHelper'] = None,
        bd_cmd: Optional[str] = None,
    ):
        self.source_root = source_root
        self.tool_root = tool_root or Path(__file__).resolve().parent
        self.repo_root = source_root
        self.git_helper = git_helper
        self.bd_cmd = bd_cmd or shutil.which(os.environ.get('BD_CMD', 'bd'))
        self.issues: Dict[str, Dict[str, Any]] = {}
        self.wrong_source_tree = False
        
        # Check if bd command is available
        if not self.bd_cmd:
            logger.info("Beads integration disabled (bd command not found)")
            self.enabled = False
            return
        
        self._ensure_beads_location()

        # Auto-initialize beads if .beads doesn't exist
        if not (self.repo_root / '.beads').exists():
            logger.info("Beads not initialized in source tree, initializing automatically...")
            print("*** Initializing beads issue tracker in source tree...")
            if self._initialize_beads():
                logger.info("Beads initialized successfully")
                print("*** Beads initialized and committed successfully")
            else:
                logger.warning("Failed to initialize beads, integration disabled")
                print("*** WARNING: Failed to initialize beads, integration disabled")
                self.enabled = False
                return
        
        self.enabled = True
        self._load_existing_issues()
        
        # Check if loaded issues reference directories outside our source tree
        if self.issues:
            self.wrong_source_tree = self._check_for_wrong_source_tree()
    
    def _safe_resolve(self, path: Path) -> Path:
        try:
            return path.resolve()
        except Exception:
            return path

    def _run_bd_command(
        self,
        args: List[str],
        cwd: Optional[Path] = None,
        timeout: int = 120,
        env_overrides: Optional[Dict[str, str]] = None,
    ) -> subprocess.CompletedProcess:
        if not self.bd_cmd:
            raise BeadsMigrationError("bd command not available for beads migration")
        cwd_path = cwd or self.repo_root
        cmd = [self.bd_cmd] + args
        env = os.environ.copy()
        if env_overrides:
            env.update(env_overrides)
        try:
            return subprocess.run(
                cmd,
                cwd=str(cwd_path),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise BeadsMigrationError(
                f"bd command timed out after {timeout}s in {cwd_path}: {shlex.join(cmd)}\n"
                "This can happen on very large repositories when rebuilding the beads DB.\n"
                "Try running the command manually with a higher timeout, or set BD_MIGRATION_TIMEOUT_SECONDS."
            ) from exc

    def _sqlite_env_overrides(self) -> Dict[str, str]:
        return {
            'BD_NO_DB': 'false',
            'BEADS_NO_DB': 'false',
            'BD_NO_DB_BOOL': 'false',
            'BEADS_NO_DB_BOOL': 'false',
        }

    def _bd_supports_migrate_issues(self) -> bool:
        try:
            result = self._run_bd_command(['migrate', 'issues', '--help'], cwd=self.tool_root, timeout=30)
            return result.returncode == 0
        except Exception:
            return False

    def _beads_db_exists(self, root: Path) -> bool:
        beads_dir = root / '.beads'
        if not beads_dir.exists() or not beads_dir.is_dir():
            return False
        return any(beads_dir.glob('*.db'))

    def _beads_jsonl_exists(self, root: Path) -> bool:
        beads_dir = root / '.beads'
        if not beads_dir.exists() or not beads_dir.is_dir():
            return False
        if (beads_dir / 'issues.jsonl').exists():
            return True
        return any(beads_dir.glob('*.jsonl'))

    def _read_issue_prefix_from_config(self, root: Path) -> Optional[str]:
        config_path = root / '.beads' / 'config.yaml'
        if not config_path.exists():
            return None
        try:
            content = config_path.read_text(encoding='utf-8')
        except Exception:
            return None
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith('issue_prefix:'):
                _, _, value = stripped.partition(':')
                prefix = value.strip().strip('"').strip("'")
                if prefix:
                    return prefix
        return None

    def _infer_issue_prefix_from_jsonl(self, root: Path) -> Optional[str]:
        jsonl_path = root / '.beads' / 'issues.jsonl'
        if not jsonl_path.exists():
            return None
        try:
            with jsonl_path.open('r', encoding='utf-8') as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        payload = None
                    issue_id = None
                    if isinstance(payload, dict):
                        issue_id = payload.get('id') or payload.get('issue_id')
                    if not issue_id:
                        match = re.search(r'"id"\s*:\s*"([^"]+)"', line)
                        if match:
                            issue_id = match.group(1)
                    if issue_id:
                        prefix_match = re.match(r'^([A-Za-z0-9]+)[-_]', issue_id)
                        if prefix_match:
                            return prefix_match.group(1)
        except Exception:
            return None
        return None

    def _determine_issue_prefix(self, root: Path) -> str:
        prefix = self._read_issue_prefix_from_config(root)
        if prefix:
            return prefix
        prefix = self._infer_issue_prefix_from_jsonl(root)
        if prefix:
            return prefix
        return root.name

    def _run_doctor_fix(self, root: Path, source: Optional[str] = None) -> None:
        args = ['doctor', '--fix', '--yes']
        if source:
            args.extend(['--source', source])
        timeout = int(os.environ.get('BD_MIGRATION_TIMEOUT_SECONDS', '3600'))
        doctor = self._run_bd_command(args, cwd=root, timeout=timeout, env_overrides=self._sqlite_env_overrides())
        if doctor.returncode != 0:
            raise BeadsMigrationError(
                f"bd doctor failed for {root}; stderr: {doctor.stderr.strip()}"
            )

    def _ensure_beads_db(self, root: Path) -> bool:
        """Ensure a beads database exists for the given root. Returns True if doctor ran."""
        beads_dir = root / '.beads'
        if not beads_dir.exists() or not beads_dir.is_dir():
            return False
        if self._beads_db_exists(root):
            return False
        if not self._beads_jsonl_exists(root):
            raise BeadsMigrationError(
                f"No beads database or JSONL found in {beads_dir}; cannot migrate."
            )
        prefix = self._determine_issue_prefix(root)
        print(f"*** Beads database missing in {root}; initializing with prefix '{prefix}'...")
        timeout = int(os.environ.get('BD_MIGRATION_TIMEOUT_SECONDS', '3600'))
        init = self._run_bd_command(
            ['init', '--from-jsonl', '--prefix', prefix],
            cwd=root,
            timeout=timeout,
            env_overrides=self._sqlite_env_overrides(),
        )
        if init.returncode != 0:
            raise BeadsMigrationError(
                "bd init failed while rebuilding from JSONL; cannot migrate. "
                f"stderr: {init.stderr.strip()}"
            )
        self._run_doctor_fix(root, source='jsonl')
        if not self._beads_db_exists(root):
            raise BeadsMigrationError(
                f"bd doctor did not create a database in {beads_dir}; cannot migrate."
            )
        return True

    def _move_beads_directory(self, source_beads: Path, dest_beads: Path) -> None:
        print("*** Moving beads data to source tree root...")
        try:
            shutil.move(str(source_beads), str(dest_beads))
        except Exception as exc:
            raise BeadsMigrationError(
                f"Failed to move .beads from {source_beads} to {dest_beads}: {exc}"
            ) from exc

        ran_doctor = self._ensure_beads_db(self.repo_root)
        if not ran_doctor:
            self._run_doctor_fix(self.repo_root)

    def _migrate_beads_issues(self, from_root: Path, to_root: Path) -> None:
        if not self._bd_supports_migrate_issues():
            raise BeadsMigrationError(
                "Beads migration required but 'bd migrate issues' is not available. "
                "Upgrade bd or migrate beads manually."
            )

        self._ensure_beads_db(from_root)
        if (to_root / '.beads').exists():
            self._ensure_beads_db(to_root)

        timeout = int(os.environ.get('BD_MIGRATION_TIMEOUT_SECONDS', '3600'))
        result = self._run_bd_command(
            [
                'migrate', 'issues',
                '--from', str(from_root),
                '--to', str(to_root),
                '--status', 'all',
                '--include', 'closure',
                '--yes',
                '--no-daemon',
            ],
            cwd=self.tool_root,
            timeout=timeout,
            env_overrides=self._sqlite_env_overrides(),
        )
        if result.returncode != 0:
            raise BeadsMigrationError(
                "bd migrate issues failed; cannot reconcile .beads in tool repo with source tree. "
                f"stderr: {result.stderr.strip()}"
            )

    def _ensure_beads_location(self) -> None:
        tool_root = self._safe_resolve(self.tool_root)
        source_root = self._safe_resolve(self.source_root)

        if tool_root == source_root:
            return

        tool_beads = tool_root / '.beads'
        source_beads = source_root / '.beads'

        if not tool_beads.exists():
            return

        if tool_beads.exists() and not tool_beads.is_dir():
            raise BeadsMigrationError(f"Found non-directory .beads at {tool_beads}")
        if source_beads.exists() and not source_beads.is_dir():
            raise BeadsMigrationError(f"Found non-directory .beads at {source_beads}")

        if source_beads.exists():
            self._migrate_beads_issues(tool_root, source_root)
        else:
            self._move_beads_directory(tool_beads, source_beads)

    def _initialize_beads(self) -> bool:
        """
        Initialize beads in the source tree and auto-commit any files created.
        Returns True if successful, False otherwise.
        """
        if not self.bd_cmd:
            return False
        
        try:
            # Get current git status to detect new files
            initial_status = None
            if self.git_helper:
                try:
                    initial_status = self.git_helper.run_git(['status', '--porcelain'])
                except Exception:
                    pass
            
            # Run bd init
            logger.info(f"Running: bd init in {self.repo_root}")
            result = subprocess.run(
                [self.bd_cmd, 'init'],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=30,
            )
            
            if result.returncode != 0:
                logger.error(f"bd init failed: {result.stderr.strip()}")
                return False
            
            logger.info(f"bd init output: {result.stdout.strip()}")
            
            # Check if .beads directory was created
            if not (self.repo_root / '.beads').exists():
                logger.error(".beads directory not created by bd init")
                return False
            
            # Auto-commit any new files created by bd init
            if self.git_helper:
                try:
                    new_status = self.git_helper.run_git(['status', '--porcelain'])
                    
                    # Find newly added/modified files (excluding .beads/ itself)
                    files_to_commit = []
                    initial_files = set()
                    if initial_status:
                        for line in initial_status.split('\n'):
                            if line.strip() and len(line) > 3:
                                initial_files.add(line[3:].strip())
                    
                    for line in new_status.split('\n'):
                        if not line.strip():
                            continue
                        if len(line) < 3:
                            continue
                        status = line[:2]
                        filepath = line[3:].strip()
                        
                        # Skip .beads/ directory itself (it's gitignored)
                        if filepath.startswith('.beads/'):
                            continue
                        
                        # Look for new or modified files
                        if status in ['??', ' M', 'M ', 'MM', 'A ', 'AM']:
                            # Only commit files that didn't exist before bd init
                            if filepath not in initial_files:
                                files_to_commit.append(filepath)
                    
                    if files_to_commit:
                        logger.info(f"Auto-committing beads integration files: {files_to_commit}")
                        
                        # Add files
                        self.git_helper.run_git(['add'] + files_to_commit)
                        
                        # Commit with clear message
                        commit_msg = (
                            "Initialize beads issue tracking integration\n\n"
                            "Auto-generated by ai-code-reviewer tool.\n"
                            "Beads (bd) is used internally for tracking code review progress."
                        )
                        commit_msg = self.git_helper.ensure_commit_prefix(commit_msg)
                        self.git_helper.run_git(['commit', '-m', commit_msg])
                        logger.info("Beads integration files committed successfully")
                    else:
                        logger.info("No new files created by bd init (or .gitignore already present)")
                        
                except Exception as exc:
                    logger.warning(f"Failed to auto-commit beads files: {exc}")
                    # Don't fail initialization just because commit failed
            
            return True
            
        except subprocess.TimeoutExpired:
            logger.error("bd init timed out")
            return False
        except Exception as exc:
            logger.error(f"Error initializing beads: {exc}")
            return False

    def _run_bd(self, args: List[str]) -> Optional[str]:
        if not self.enabled or not self.bd_cmd:
            return None
        try:
            result = subprocess.run(
                [self.bd_cmd] + args,
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                logger.warning("bd command failed (%s): %s", ' '.join(args), result.stderr.strip())
                return None
            return result.stdout
        except FileNotFoundError:
            logger.warning("bd command not found at runtime")
            self.enabled = False
        except subprocess.TimeoutExpired:
            logger.warning("bd command timed out: %s", ' '.join(args))
        except Exception as exc:
            logger.warning("bd command error: %s", exc)
        return None

    def _load_existing_issues(self) -> None:
        output = self._run_bd(['search', '--json', '--limit', '200000', 'Review directory:'])
        if not output:
            return
        try:
            data = json.loads(output)
        except json.JSONDecodeError as exc:
            logger.warning("Unable to parse bd search output: %s", exc)
            return

        for issue in data:
            directory = self._extract_directory(issue)
            if not directory:
                continue
            # Prefer non-closed issues if duplicates exist
            existing = self.issues.get(directory)
            if existing:
                if existing.get('status') != 'closed' and issue.get('status') == 'closed':
                    continue
            self.issues[directory] = {
                'id': issue.get('id'),
                'status': issue.get('status', 'open'),
                'title': issue.get('title'),
            }

    @staticmethod
    def _extract_directory(issue: Dict[str, Any]) -> Optional[str]:
        title = (issue.get('title') or '').strip()
        if title.startswith('Review directory: '):
            candidate = title.split(':', 1)[1].strip()
            if candidate:
                return candidate
        description = issue.get('description') or ''
        match = re.search(r'in ([\w./-]+) directory \(relative to source root', description)
        if match:
            return match.group(1)
        return None
    
    def _check_for_wrong_source_tree(self) -> bool:
        """
        Check if loaded beads reference directories outside our source tree.
        Returns True if issues appear to be for a different source tree.
        """
        if not self.issues:
            return False
        
        # Check first few directories to see if they look like external paths
        sample_size = min(10, len(self.issues))
        sample_dirs = list(self.issues.keys())[:sample_size]
        
        external_count = 0
        for directory in sample_dirs:
            # Check for obvious external path markers
            if directory.startswith('../'):
                external_count += 1
            elif '/' in directory and not (self.repo_root / directory.split('/')[0]).exists():
                external_count += 1
        
        # If more than half look external, this is probably the wrong source tree
        return external_count > (sample_size / 2)

    def ensure_directories(self, directories: List[str]) -> int:
        if not self.enabled:
            return 0
        created = 0
        for directory in directories:
            if directory in self.issues:
                continue
            description = (
                f"AI code review of all files in {directory} directory "
                f"(relative to source root: {self.repo_root})"
            )
            output = self._run_bd([
                'create',
                f'Review directory: {directory}',
                '--description', description,
                '-t', 'task',
                '-p', '2',
                '--json'
            ])
            if not output:
                continue
            try:
                issue = json.loads(output)
                self.issues[directory] = {
                    'id': issue.get('id'),
                    'status': issue.get('status', 'open'),
                    'title': issue.get('title'),
                }
                created += 1
            except json.JSONDecodeError:
                logger.warning("Failed to parse bd create output for %s", directory)
        return created

    def _get_issue_id(self, directory: str) -> Optional[str]:
        issue = self.issues.get(directory)
        return issue.get('id') if issue else None

    def _ensure_directory_issue(self, directory: str) -> Optional[str]:
        """Lazily create a beads issue for a directory if one doesn't exist yet."""
        issue_id = self._get_issue_id(directory)
        if issue_id:
            return issue_id
        description = (
            f"AI code review of all files in {directory} directory "
            f"(relative to source root: {self.repo_root})"
        )
        output = self._run_bd([
            'create',
            f'Review directory: {directory}',
            '--description', description,
            '-t', 'task',
            '-p', '2',
            '--json'
        ])
        if not output:
            return None
        try:
            issue = json.loads(output)
            self.issues[directory] = {
                'id': issue.get('id'),
                'status': issue.get('status', 'open'),
                'title': issue.get('title'),
            }
            return issue.get('id')
        except json.JSONDecodeError:
            logger.warning("Failed to parse bd create output for %s", directory)
            return None

    def mark_in_progress(self, directory: str) -> None:
        issue_id = self._ensure_directory_issue(directory)
        if not issue_id:
            return
        output = self._run_bd(['update', issue_id, '--status', 'in_progress', '--json'])
        if output:
            self.issues[directory]['status'] = 'in_progress'

    def mark_open(self, directory: str) -> None:
        issue_id = self._ensure_directory_issue(directory)
        if not issue_id:
            return
        output = self._run_bd(['update', issue_id, '--status', 'open', '--json'])
        if output:
            self.issues[directory]['status'] = 'open'

    def mark_completed(self, directory: str, commit_hash: str) -> None:
        issue_id = self._ensure_directory_issue(directory)
        if not issue_id:
            return
        reason = f"Completed via commit {commit_hash}"
        output = self._run_bd(['close', issue_id, '--reason', reason, '--json'])
        if output:
            self.issues[directory]['status'] = 'closed'
    
    def has_open_work(self) -> bool:
        """Return True if any directory review bead is still open or in_progress."""
        return any(
            issue.get('status') in ('open', 'in_progress')
            for issue in self.issues.values()
        )

    def get_open_directories(self) -> List[str]:
        """Return directory names that have open or in_progress beads."""
        return [
            directory for directory, issue in self.issues.items()
            if issue.get('status') in ('open', 'in_progress')
        ]

    def get_open_count(self) -> int:
        """Return count of non-closed directory review beads."""
        return sum(
            1 for issue in self.issues.values()
            if issue.get('status') in ('open', 'in_progress')
        )

    def create_systemic_issue(
        self,
        title: str,
        description: str,
        issue_type: str = 'bug',
        priority: int = 1,
        labels: Optional[List[str]] = None
    ) -> Optional[str]:
        """
        Create a beads issue for systemic problems discovered during review.
        
        Args:
            title: Short issue title
            description: Detailed description
            issue_type: bug, feature, task, epic, chore
            priority: 0-4 (0=critical, 1=high, 2=medium, 3=low, 4=backlog)
            labels: Optional list of labels
            
        Returns:
            Issue ID if created, None otherwise
        """
        if not self.enabled:
            return None
        
        args = [
            'create',
            title,
            '--description', description,
            '-t', issue_type,
            '-p', str(priority),
            '--json'
        ]
        
        # Add labels if provided
        if labels:
            for label in labels:
                args.extend(['--label', label])
        
        output = self._run_bd(args)
        if not output:
            return None
        
        try:
            issue = json.loads(output)
            issue_id = issue.get('id')
            if issue_id:
                logger.info(f"Created systemic issue: {issue_id} - {title}")
                return issue_id
        except json.JSONDecodeError as exc:
            logger.warning(f"Failed to parse bd create output: {exc}")
        
        return None


class FileEditor:
    """Handles file editing operations."""
    
    def __init__(self, git: GitHelper):
        self.git = git

    @staticmethod
    def _closest_block(content: str, target: str) -> Optional[str]:
        """Return the closest matching block from content for the target snippet."""
        target_clean = target.strip()
        if not target_clean:
            return None
        target_lines = [line for line in target.splitlines() if line.strip()]
        if not target_lines:
            return None

        content_lines = content.splitlines()
        window = len(target_lines)
        if window == 0 or window > len(content_lines):
            return None

        best_ratio = 0
        best_block = None
        for idx in range(len(content_lines) - window + 1):
            block = content_lines[idx:idx + window]
            ratio = SequenceMatcher(
                None,
                '\n'.join(block).strip(),
                '\n'.join(target_lines).strip()
            ).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_block = block

        if best_block and best_ratio >= 0.4:
            return '\n'.join(best_block)
        return None
    
    @staticmethod
    def read_file(file_path: Path, max_chars: int = 50000) -> str:
        """Read a file, truncating if necessary."""
        try:
            text = file_path.read_text(encoding='utf-8', errors='replace')
            if len(text) > max_chars:
                lines = text[:max_chars].rsplit('\n', 1)[0]
                return lines + f"\n\n[... TRUNCATED: {len(text) - len(lines)} more characters ...]"
            return text
        except Exception as e:
            return f"ERROR reading file: {e}"
    
    def edit_file(self, file_path: Path, old_text: str, new_text: str) -> Tuple[bool, str, str]:
        """
        Edit a file by replacing old_text with new_text.
        
        Returns:
            Tuple of (success, message, diff)
        """
        try:
            content = file_path.read_text(encoding='utf-8')
            
            if old_text not in content:
                closest = self._closest_block(content, old_text)
                hint = ""
                if closest:
                    hint = (
                        "\nClosest match found in file (copy this EXACT block for OLD):\n<<<\n"
                        f"{closest}\n>>>"
                    )
                return False, f"OLD text not found in {file_path}{hint}", ""
            
            count = content.count(old_text)
            if count > 1:
                return False, f"OLD text appears {count} times in {file_path} - must be unique", ""
            
            new_content = content.replace(old_text, new_text)
            file_path.write_text(new_content, encoding='utf-8')
            
            diff = self.git.diff(str(file_path))
            
            return True, f"Successfully edited {file_path}", diff
        except Exception as e:
            return False, f"Error editing {file_path}: {e}", ""
    
    def write_file(self, file_path: Path, content: str) -> Tuple[bool, str, str]:
        """Write content to a file."""
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding='utf-8')
            diff = self.git.diff(str(file_path))
            return True, f"Successfully wrote {file_path}", diff
        except Exception as e:
            return False, f"Error writing {file_path}: {e}", ""
    
    def append_to_file(self, file_path: Path, content: str) -> Tuple[bool, str]:
        """Append content to a file."""
        try:
            with open(file_path, 'a', encoding='utf-8') as f:
                f.write(content)
            return True, f"Successfully appended to {file_path}"
        except Exception as e:
            return False, f"Error appending to {file_path}: {e}"


class ActionParser:
    """Parses AI responses for file edit actions."""

    # Support leading markdown bullets/headings and either colons or dashes
    ACTION_RE = re.compile(
        r'^[\t >*\-\x60]*'        # optional list markers like "> ", "- ", "```"
        r'(?:#{1,6}\s*)?'          # optional markdown heading prefix
        r'ACTION'                   # the literal ACTION keyword
        r'\s*[:\-]\s*'           # separator (colon or dash)
        r'([A-Z0-9_]+)'             # action name (READ_FILE, EDIT_FILE, etc.)
        r'(.*)$',
        re.MULTILINE | re.IGNORECASE,
    )

    # Fallback matcher that finds inline ACTION directives anywhere in a line
    ACTION_INLINE_RE = re.compile(
        r'ACTION\s*[:\-]\s*([A-Z0-9_]+)\s*(.*)',
        re.IGNORECASE,
    )

    ACTIONS_WITH_ARGUMENT = {
        'READ_FILE', 'EDIT_FILE', 'WRITE_FILE', 'LIST_DIR', 'FIND_FILE',
        'GREP', 'SET_SCOPE'
    }

    @classmethod
    def _find_fallback_match(cls, response: str) -> Optional[Tuple[str, str, int]]:
        """Fallback search for ACTION lines when strict regex misses them."""
        fallback_match = None
        for match in cls.ACTION_INLINE_RE.finditer(response):
            fallback_match = match
        if not fallback_match:
            return None
        action = fallback_match.group(1)
        arg = fallback_match.group(2)
        return action, arg, fallback_match.end()

    @classmethod
    def parse(cls, response: str) -> Optional[Dict[str, Any]]:
        """Parse an AI response for action directives."""
        matches = list(cls.ACTION_RE.finditer(response))
        match = matches[-1] if matches else None
        action_raw: Optional[str]
        arg_raw: str
        body_start: int

        if match:
            action_raw = match.group(1)
            arg_raw = match.group(2)
            body_start = match.end()
        else:
            fallback = cls._find_fallback_match(response)
            if not fallback:
                return None
            action_raw, arg_raw, body_start = fallback

        action = action_raw.strip().upper().replace('-', '_')
        arg = arg_raw.strip()

        remainder = response[body_start:]
        remainder = remainder.lstrip('\r\n')

        if not arg and action in cls.ACTIONS_WITH_ARGUMENT and remainder:
            # Some models put the argument on the next line
            lines = remainder.splitlines()
            if lines:
                arg = lines[0].strip()
                remainder = '\n'.join(lines[1:])

        body = remainder.strip()

        result = {'action': action, 'argument': arg, 'body': body}

        if action == 'EDIT_FILE':
            result['file_path'] = arg
            old_match = re.search(r'OLD:\s*<<<(.*?)>>>', body, re.DOTALL)
            new_match = re.search(r'NEW:\s*<<<(.*?)>>>', body, re.DOTALL)
            if old_match and new_match:
                result['old_text'] = old_match.group(1).strip()
                result['new_text'] = new_match.group(1).strip()

        elif action == 'WRITE_FILE':
            result['file_path'] = arg
            content_match = re.search(r'CONTENT:\s*<<<(.*?)>>>', body, re.DOTALL)
            if content_match:
                result['content'] = content_match.group(1).strip()

        elif action == 'READ_FILE':
            result['file_path'] = arg

        elif action == 'LIST_DIR':
            result['dir_path'] = arg

        elif action == 'FIND_FILE':
            result['pattern'] = arg

        elif action == 'GREP':
            result['pattern'] = arg

        elif action == 'SET_SCOPE':
            result['directory'] = arg

        elif action == 'NEXT_CHUNK':
            pass  # No arguments needed

        elif action == 'SKIP_FILE':
            pass  # No arguments needed

        return result


class ReviewLoop:
    """Main review loop that coordinates AI, file editing, and builds."""
    
    def __init__(
        self,
        ollama_client: Any,
        build_executor: Any,
        source_root: Path,
        persona_dir: Path,
        review_config: Optional[Dict[str, Any]] = None,
        target_directories: int = 10,
        max_iterations_per_directory: int = 200,
        max_parallel_files: int = 1,
        log_dir: Optional[Path] = None,
        ops_logger: Optional[OpsLogger] = None,
        forever_mode: bool = False,
    ):
        self.ollama = ollama_client
        self.builder = build_executor
        self.source_root = source_root
        self.persona_dir = persona_dir
        self.target_directories = target_directories
        self.max_iterations_per_directory = max_iterations_per_directory
        self.max_parallel_files = max_parallel_files
        self.review_config = review_config or {}
        self.forever_mode = forever_mode
        
        # Persona files (behavior templates - shared across projects)
        self.bootstrap_file = persona_dir / "AI_START_HERE.md"
        
        # Source-specific files (lessons learned and progress - per project)
        # These live in the source tree so each project has its own history
        self.source_meta_dir = source_root / ".ai-code-reviewer"
        self.source_meta_dir.mkdir(parents=True, exist_ok=True)
        self.lessons_file = self.source_meta_dir / "LESSONS.md"
        self.review_summary_file = self.source_meta_dir / "REVIEW-SUMMARY.md"
        
        # One-time migration from legacy locations
        self._migrate_legacy_files(source_root, persona_dir)
        
        # Initialize files if they don't exist
        if not self.lessons_file.exists():
            self.lessons_file.write_text(
                "# Lessons Learned\n\n"
                "This file tracks mistakes made during code review to avoid repeating them.\n"
                "Each lesson is recorded with timestamp, category, and remediation advice.\n\n"
            )
        if not self.review_summary_file.exists():
            self.review_summary_file.write_text(
                "# Review Summary\n\n"
                "Progress tracking for code review sessions.\n\n"
                "---\n\n"
            )
        
        # Logs go in persona directory too (or override)
        self.log_dir = log_dir or (persona_dir / 'logs')
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        self.git = GitHelper(source_root)
        self.editor = FileEditor(self.git)
        self.parser = ActionParser()
        
        # Store chunker config values (with sensible defaults)
        self.chunk_size = review_config.get('chunk_size', 250)
        self.chunk_threshold = review_config.get('chunk_threshold', 400)
        logger.info(f"File chunker: threshold={self.chunk_threshold} lines, chunk_size={self.chunk_size} lines")
        
        self.session = ReviewSession(
            session_id=datetime.datetime.now().strftime("%Y%m%d_%H%M%S"),
            start_time=datetime.datetime.now(),
        )
        
        # Operations logger for internal metrics
        self.ops = ops_logger or OpsLogger(session_id=self.session.session_id)

        # Retry tracker for problematic directories
        self.retry_tracker_path = self.persona_dir / 'retry-tracker.json'
        self.retry_tracker = self._load_retry_tracker()
        self.max_directory_retries = int(self.review_config.get('max_directory_retries', 3))
        
        # Chunk tracking for large files
        self.current_chunks: List[Any] = []  # Chunks for current file
        self.current_chunk_index: int = 0  # Which chunk we're on
        self.chunked_file_path: Optional[Path] = None  # Path of file being chunked
        
        # Load bootstrap content
        self.bootstrap_content = self.bootstrap_file.read_text(encoding='utf-8')

        # Validate persona
        print("*** Validating persona...")
        is_valid, validation_report = PersonaValidator.validate_and_report(persona_dir)
        if not is_valid:
            logger.error(f"Persona validation failed:\n{validation_report}")
            print(f"\n{'='*70}")
            print("ERROR: Persona Validation Failed")
            print(f"{'='*70}")
            print(validation_report)
            print(f"\n{'='*70}")
            print("Please fix the persona files or choose a different persona.")
            print(f"{'='*70}\n")
            raise ValueError("Invalid persona")
        else:
            logger.info(f"Persona validated successfully:\n{validation_report}")
            print(f"    ✓ Persona validated: {persona_dir.name}")

        # Initialize persona metrics tracker
        metrics_dir = self.source_meta_dir / "metrics"
        self.metrics_tracker = PersonaMetricsTracker(metrics_dir)
        self.metrics = self.metrics_tracker.start_session(
            persona_name=persona_dir.name,
            session_id=self.session.session_id
        )
        logger.info(f"Persona metrics tracking enabled: {metrics_dir}")

        # Load or generate the review index
        print("*** Loading review index...")
        force_rebuild = bool(self.review_config.get('rebuild_index', False))
        env_rebuild = os.environ.get('ANGRY_AI_REBUILD_INDEX')
        if env_rebuild is not None:
            force_rebuild = env_rebuild.strip().lower() in {'1', 'true', 'yes', 'on'}
        self.index = generate_index(source_root, force_rebuild=force_rebuild)
        print(f"    Found {len(self.index.entries)} reviewable directories")
        self.beads = self._init_beads_manager()
        
        # Conversation history
        self.history: List[Dict[str, str]] = []
        
        # Parallel processing support
        # max_parallel_files: 0 = dynamic (from server), 1 = sequential, 2+ = static parallel
        self._dynamic_parallelism = (max_parallel_files == 0)
        self._edit_lock = threading.Lock()  # Always have lock for safety
        self._interrupted = False  # Set on Ctrl+C for graceful shutdown
        self._active_futures: List[Future] = []  # Track in-flight requests
        
        if self._dynamic_parallelism:
            # Query server for recommended parallelism
            try:
                recommended = self.ollama.get_recommended_parallelism(max_parallel=16)
                self.max_parallel_files = recommended
                self._parallel_mode = recommended > 1
                print(f"*** Dynamic parallelism: server capacity = {recommended} concurrent reviews")
                logger.info(f"Dynamic parallelism enabled: {recommended} workers from server metrics")
            except Exception as e:
                # Fall back to reasonable default
                self.max_parallel_files = 8
                self._parallel_mode = True
                print(f"*** Dynamic parallelism: server metrics unavailable, using default (8)")
                logger.warning(f"Could not get server metrics for dynamic parallelism: {e}")
        else:
            self._parallel_mode = max_parallel_files > 1
            
            # Check if static value differs from server recommendation
            if self._parallel_mode:
                try:
                    recommended = self.ollama.get_recommended_parallelism(max_parallel=16)
                    if abs(recommended - max_parallel_files) >= 2:
                        print(f"\n*** WARNING: Parallelism mismatch")
                        print(f"    Config specifies: {max_parallel_files} concurrent reviews")
                        print(f"    Server recommends: {recommended} (based on GPU/KV cache capacity)")
                        if recommended > max_parallel_files:
                            print(f"    You may be under-utilizing your GPU. Consider setting max_parallel_files: 0")
                        else:
                            print(f"    You may be over-loading your GPU. Consider setting max_parallel_files: 0")
                        print()
                except Exception:
                    pass  # Can't get recommendation, skip warning
            
            if self._parallel_mode:
                logger.info(f"Static parallel mode: {max_parallel_files} workers (from config)")
                print(f"*** Parallel mode: {max_parallel_files} concurrent file reviews (static)")
            else:
                logger.info("Sequential mode: 1 file at a time")
                print("*** Sequential mode: reviewing files one at a time")
        
        self._init_conversation()
    
    def _migrate_legacy_files(self, source_root: Path, persona_dir: Path) -> None:
        """
        One-time migration from legacy file locations.
        
        Checks for files in:
        1. Persona directory (oldest location: personas/*/LESSONS.md)
        2. .angry-ai/ (previous location)
        
        Moves/merges them to: .ai-code-reviewer/
        
        This ensures continuity when users update the tool.
        """
        import shutil
        
        legacy_locations = [
            # (source_path, description)
            (persona_dir / "LESSONS.md", "persona directory"),
            (persona_dir / "REVIEW-SUMMARY.md", "persona directory"),
            (source_root / ".angry-ai" / "LESSONS.md", ".angry-ai directory"),
            (source_root / ".angry-ai" / "REVIEW-SUMMARY.md", ".angry-ai directory"),
        ]
        
        migrated = []
        
        for legacy_path, location_desc in legacy_locations:
            if not legacy_path.exists():
                continue
            
            # Determine target file
            filename = legacy_path.name
            target_path = self.source_meta_dir / filename
            
            # Read legacy content
            try:
                legacy_content = legacy_path.read_text(encoding='utf-8')
            except Exception as e:
                logger.warning(f"Could not read legacy file {legacy_path}: {e}")
                continue
            
            # If target already exists, merge content
            if target_path.exists():
                try:
                    existing_content = target_path.read_text(encoding='utf-8')
                    # Append legacy content with separator
                    merged_content = (
                        existing_content.rstrip() + "\n\n" +
                        f"--- Migrated from {location_desc} ---\n\n" +
                        legacy_content
                    )
                    target_path.write_text(merged_content, encoding='utf-8')
                    migrated.append(f"{filename} (merged from {location_desc})")
                except Exception as e:
                    logger.warning(f"Could not merge {legacy_path} into {target_path}: {e}")
                    continue
            else:
                # Move to new location
                try:
                    shutil.copy2(legacy_path, target_path)
                    migrated.append(f"{filename} (from {location_desc})")
                except Exception as e:
                    logger.warning(f"Could not copy {legacy_path} to {target_path}: {e}")
                    continue
            
            # Remove legacy file after successful migration
            try:
                legacy_path.unlink()
                logger.info(f"Removed legacy file: {legacy_path}")
            except Exception as e:
                logger.warning(f"Could not remove legacy file {legacy_path}: {e}")
        
        # Try to remove .angry-ai/ directory if empty
        old_dir = source_root / ".angry-ai"
        if old_dir.exists():
            try:
                # Only remove if empty (logs/ might still be there)
                if not any(old_dir.iterdir()):
                    old_dir.rmdir()
                    logger.info(f"Removed empty legacy directory: {old_dir}")
            except Exception:
                pass  # Not empty or can't remove, that's fine
        
        if migrated:
            print(f"\n*** Migrated legacy files to .ai-code-reviewer/:")
            for item in migrated:
                print(f"    ✓ {item}")
            print()
    
    def _load_retry_tracker(self) -> Dict[str, Dict[str, Any]]:
        if self.retry_tracker_path.exists():
            try:
                with open(self.retry_tracker_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
            except Exception as exc:
                print(f"*** WARNING: Unable to read {self.retry_tracker_path}: {exc}")
        return {}

    def _save_retry_tracker(self) -> None:
        try:
            with open(self.retry_tracker_path, 'w', encoding='utf-8') as f:
                json.dump(self.retry_tracker, f, indent=2, sort_keys=True)
        except Exception as exc:
            print(f"*** WARNING: Unable to write {self.retry_tracker_path}: {exc}")

    def _init_beads_manager(self) -> Optional[BeadsManager]:
        try:
            tool_root = Path(__file__).resolve().parent
            manager = BeadsManager(self.source_root, tool_root=tool_root, git_helper=self.git)
            if not manager.enabled:
                print("*** Beads integration disabled (bd unavailable)")
                return None
            
            # Check if beads are for a different source tree
            if manager.wrong_source_tree:
                print("\n" + "=" * 70)
                print("WARNING: Existing beads appear to be for a different source tree")
                print("=" * 70)
                print(f"Current source root: {self.source_root}")
                print(f"Beads database has {len(manager.issues)} issues for external directories")
                print()
                print("Sample beads found:")
                for i, directory in enumerate(list(manager.issues.keys())[:5]):
                    print(f"  - {directory}")
                print()
                print("Options:")
                print("  1. Run 'bd close --all' to clear old beads")
                print("  2. Or delete .beads/ directory to start fresh")
                print("  3. Or point source.root in config.yaml to the correct tree")
                print()
                print("Continuing with empty beads tracking for this run...")
                print("=" * 70 + "\n")
                # Clear the issues so we create new ones for this source tree
                manager.issues = {}
            
            tracked = len(manager.issues)
            total = len(self.index.entries)
            print(f"    Beads: {tracked}/{total} directories already tracked (lazy creation for rest)")
            return manager
        except BeadsMigrationError as exc:
            print("\nWARNING: Beads migration failed; continuing without beads integration")
            print("-" * 70)
            print(str(exc))
            print("-" * 70)
            return None
        except Exception as exc:
            print(f"*** WARNING: Unable to initialize beads manager: {exc}")
            logger.warning("Beads initialization failed", exc_info=exc)
            return None

    def _beads_mark_in_progress(self, directory: str) -> None:
        if self.beads:
            self.beads.mark_in_progress(directory)

    def _beads_mark_completed(self, directory: str, commit_hash: str) -> None:
        if self.beads:
            self.beads.mark_completed(directory, commit_hash)

    def _beads_mark_open(self, directory: str) -> None:
        if self.beads:
            self.beads.mark_open(directory)

    def _get_retry_record(self, directory: str) -> Dict[str, Any]:
        return self.retry_tracker.get(directory, {})

    def _should_auto_skip(self, directory: str) -> bool:
        if self.max_directory_retries <= 0:
            return False
        attempts = self._get_retry_record(directory).get('attempts', 0)
        return attempts >= self.max_directory_retries

    def _record_directory_attempt(self, directory: str) -> int:
        record = self.retry_tracker.setdefault(directory, {})
        attempts = record.get('attempts', 0) + 1
        record['attempts'] = attempts
        record['last_attempt'] = datetime.datetime.now().isoformat()
        self._save_retry_tracker()
        return attempts

    def _clear_directory_attempt(self, directory: Optional[str]) -> None:
        if not directory:
            return
        if directory in self.retry_tracker:
            self.retry_tracker.pop(directory, None)
            self._save_retry_tracker()

    def _init_conversation(self) -> None:
        """Initialize the conversation with system prompt, bootstrap, lessons, and index."""
        system_prompt = self._build_system_prompt()
        
        # Load LESSONS.md to provide context of past mistakes
        lessons_content = ""
        if self.lessons_file.exists():
            try:
                lessons_content = self.lessons_file.read_text(encoding='utf-8')
                # Truncate if too long (keep last 8000 chars = ~20-30 lessons)
                if len(lessons_content) > 8000:
                    lessons_content = "...[earlier lessons truncated]...\n\n" + lessons_content[-8000:]
            except Exception as e:
                logger.warning(f"Failed to load LESSONS.md: {e}")
                lessons_content = ""
        
        # Get current position and next target from index
        index_summary = self.index.get_summary_for_ai()
        current = self.index.get_current()
        next_target = self.index.get_next_pending()
        
        # Build the initial user message with context
        init_message = f"""Here is your bootstrap instruction file:

```markdown
{self.bootstrap_content}
```

{index_summary}

"""
        
        # Include lessons learned if available
        if lessons_content:
            init_message += f"""
=== LESSONS LEARNED FROM PAST MISTAKES ===

**CRITICAL**: Before making ANY edit, consult these lessons to avoid repeating mistakes!

```markdown
{lessons_content}
```

**Remember**: These lessons were learned the hard way (build failures, reverted changes).
Check this list before every EDIT_FILE action to ensure you're not repeating a documented mistake.

"""
        
        if current:
            init_message += f"\nRESUME reviewing: `{current}` (already in progress)\n"
            init_message += f"Use: ACTION: SET_SCOPE {current}\n"
        elif next_target:
            init_message += f"\nSTART with: `{next_target}`\n"
            init_message += f"Use: ACTION: SET_SCOPE {next_target}\n"
        
        init_message += "\nBegin your review."
        
        self.history = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": init_message},
        ]
    
    def _build_system_prompt(self) -> str:
        """Build the system prompt for the AI."""
        return """You are an autonomous code review AI for FreeBSD source code.

IMPORTANT: Work ONE DIRECTORY AT A TIME. Each directory (bin/cpuset/, sbin/mount/, etc.)
is a complete unit with its own Makefile. Review ALL files in a directory before moving on.

ACTIONS:

ACTION: SET_SCOPE bin/cpuset
  - Declare which directory you are reviewing
  - MUST be set before making edits
  - All edits will be committed together when BUILD succeeds

ACTION: FIND_FILE filename.c
  - Search for files by name (supports wildcards)
  - Use to discover which directories contain code to review

ACTION: GREP pattern
  - Search file contents for a regex pattern

ACTION: READ_FILE path/to/file
  - Read a file from the source tree
  - Large files are automatically chunked by function
  - You'll review function-by-function for better performance

ACTION: NEXT_CHUNK
  - Get the next chunk of a large file being reviewed
  - Use this after reviewing/fixing the current chunk
  - Continue until all chunks are reviewed

ACTION: SKIP_FILE
  - Skip remaining chunks of a large file
  - Use if file is vendor code, generated code, or not worth reviewing

ACTION: LIST_DIR path/to/directory
  - List contents of a directory
  - Use to see all files in a directory before reviewing

ACTION: EDIT_FILE path/to/file
OLD:
<<<
EXACT text copied from file (include 3-5 lines context)
>>>
NEW:
<<<
replacement text
>>>

CRITICAL EDIT_FILE RULES:
- OLD block must be COPIED EXACTLY from the file you just read
- Do NOT paraphrase or summarize - copy the EXACT characters
- FreeBSD code uses TABS for indentation, not spaces!
- Include enough context lines to make it unique
- The <<< and >>> delimiters are REQUIRED
- If the code already has the fix (e.g., strtonum already present), SKIP IT

EXAMPLE (correct):
ACTION: EDIT_FILE bin/cpuset/cpuset.c
OLD:
<<<
		case 'd':
			dflag = 1;
			which = CPU_WHICH_DOMAIN;
			id = atoi(optarg);
			break;
>>>
NEW:
<<<
		case 'd':
			dflag = 1;
			which = CPU_WHICH_DOMAIN;
			id = strtonum(optarg, 0, INT_MAX, &errstr);
			if (errstr)
				errx(1, "domain id %s: %s", errstr, optarg);
			break;
>>>

ACTION: WRITE_FILE path/to/file
CONTENT:
<<<
file content
>>>
  - Create or overwrite a file

ACTION: BUILD
  - Run make buildworld to validate ALL changes in current scope
  - If succeeds: all changes in scope directory are committed together
  - If fails: analyze errors, fix them, rebuild

ACTION: HALT
  - Signal completely done with review session
  - Will be REJECTED if:
    * You have uncommitted changes (run BUILD first)
    * No directories completed yet (must complete at least 1)
    * There are still reviewable directories and less than 3 completed
  - Keep working until all target directories are done

FREEBSD SOURCE TREE STRUCTURE:
- bin/       - Essential user commands (cpuset, chio, chmod, cp, etc.)
- sbin/      - Essential system commands (mount, ifconfig, init, etc.)
- usr.bin/   - Non-essential user commands
- usr.sbin/  - Non-essential system commands
- lib/       - System libraries
- sys/       - Kernel source

Each subdirectory (bin/cpuset/, bin/chio/, sbin/mount/, etc.) has its own Makefile
and represents a complete tool or library. Review ALL files in a directory together.

WORKFLOW:
1. Read REVIEW-SUMMARY.md to see completed directories (marked with ✓)
2. Pick a directory that is NOT already marked complete
3. SET_SCOPE to that directory
4. LIST_DIR to see all files in it
5. READ each .c and .h file
6. CHECK if the file already has fixes (e.g., strtonum already used)
   - If already fixed: SKIP this file, note it's done
   - If needs fixes: proceed to EDIT
7. EDIT files to fix issues (security, correctness, style)
8. When all files in directory are reviewed, run BUILD
9. If build fails: fix errors, rebuild
10. If build succeeds: directory is done, pick next directory
11. HALT only when all directories reviewed or stuck

SKIP FILES THAT ARE ALREADY FIXED:
- If you see strtonum() already in use where atoi() would be, it's fixed
- If you see error checking already present, it's fixed
- Move to the NEXT file or directory instead of re-fixing

RULES:
1. SET_SCOPE before editing any files
2. Review ALL files in a directory before BUILD
3. Commit message will reflect the entire directory's changes
4. Use relative paths from source root
5. Include enough context in OLD blocks for uniqueness
6. **CONSULT LESSONS.md** - Before making edits, check the lessons learned from past mistakes!
   The lessons are provided in your initial context. Don't repeat documented errors.

Respond with analysis followed by a single ACTION line.
"""
    
    def _review_single_file(self, file_path: str) -> List[Dict[str, Any]]:
        """
        Review a single file and return proposed edits.
        
        This method creates its own conversation context for parallel review.
        
        Args:
            file_path: Relative path to the file from source root
            
        Returns:
            List of edit dictionaries with 'file_path', 'old_text', 'new_text'
        """
        path = self.source_root / file_path
        if not path.exists():
            logger.warning(f"Parallel review: file not found: {file_path}")
            return []
        
        # Skip non-code files
        suffix = path.suffix.lower()
        if suffix in MANPAGE_SUFFIXES:
            logger.debug(f"Parallel review: skipping manpage {file_path}")
            return []
        
        # Read the file content
        try:
            content = path.read_text(encoding='utf-8', errors='replace')
        except Exception as e:
            logger.warning(f"Parallel review: failed to read {file_path}: {e}")
            return []
        
        # Truncate very large files for parallel review
        if len(content) > 50000:
            content = content[:50000] + "\n\n[... TRUNCATED for parallel review ...]"
        
        # Build a focused prompt for this single file
        system_prompt = """You are a code reviewer for FreeBSD source code.
Review the file and suggest specific edits to fix issues like:
- Security vulnerabilities (buffer overflows, unsafe functions)
- Replace atoi/atol with strtonum for better error handling
- Memory leaks and resource management
- Error handling improvements
- Code correctness issues

IMPORTANT: Only suggest edits if there are REAL issues to fix.
If the code is already correct (e.g., already uses strtonum), say "NO_EDITS_NEEDED".

For each edit, use this EXACT format:

EDIT:
FILE: <path>
OLD:
<<<
exact text to replace (copy from file)
>>>
NEW:
<<<
replacement text
>>>

Include 3-5 lines of context in OLD blocks for uniqueness.
Use TABS for indentation (FreeBSD style).
You may suggest multiple edits."""

        user_prompt = f"""Review this file and suggest edits:

FILE: {file_path}

```c
{content}
```

Analyze the code and provide EDIT blocks for any issues found.
If no changes needed, respond with just: NO_EDITS_NEEDED"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        
        try:
            logger.info(f"Parallel review: sending {file_path} to LLM")
            response = self.ollama.chat(messages)
            logger.info(f"Parallel review: received response for {file_path}")
        except Exception as e:
            logger.error(f"Parallel review: LLM error for {file_path}: {e}")
            return []
        
        # Parse edits from response
        edits = []
        if "NO_EDITS_NEEDED" in response:
            logger.debug(f"Parallel review: no edits needed for {file_path}")
            return []

        def _normalize_edit_path(candidate: str, default_path: str) -> str:
            if not candidate:
                return default_path
            cleaned = candidate.strip().strip('`').strip('"').strip("'")
            lowered = cleaned.lower()
            placeholders = {
                '<path>', '<file>', '<file_path>', '<filepath>', '<file path>',
                'path', 'file', 'file_path', 'filepath'
            }
            if lowered in placeholders:
                return default_path
            if lowered.startswith('<') and lowered.endswith('>'):
                return default_path
            return cleaned
        
        # Parse EDIT blocks
        edit_pattern = re.compile(
            r'EDIT:\s*\n'
            r'FILE:\s*([^\n]+)\s*\n'
            r'OLD:\s*\n<<<\s*\n(.*?)\n>>>\s*\n'
            r'NEW:\s*\n<<<\s*\n(.*?)\n>>>',
            re.DOTALL
        )
        
        for match in edit_pattern.finditer(response):
            edit_file = _normalize_edit_path(match.group(1).strip(), file_path)
            old_text = match.group(2).strip()
            new_text = match.group(3).strip()
            
            # Validate the edit
            if old_text and new_text and old_text != new_text:
                edits.append({
                    'file_path': edit_file,
                    'old_text': old_text,
                    'new_text': new_text
                })
                logger.info(f"Parallel review: found edit for {edit_file}")
        
        # Also try the standard ACTION: EDIT_FILE format
        action_pattern = re.compile(
            r'ACTION:\s*EDIT_FILE\s+([^\n]+)\s*\n'
            r'OLD:\s*\n<<<\s*\n(.*?)\n>>>\s*\n'
            r'NEW:\s*\n<<<\s*\n(.*?)\n>>>',
            re.DOTALL | re.IGNORECASE
        )
        
        for match in action_pattern.finditer(response):
            edit_file = _normalize_edit_path(match.group(1).strip(), file_path)
            old_text = match.group(2).strip()
            new_text = match.group(3).strip()
            
            if old_text and new_text and old_text != new_text:
                # Avoid duplicates
                is_dup = any(
                    e['old_text'] == old_text and e['new_text'] == new_text
                    for e in edits
                )
                if not is_dup:
                    edits.append({
                        'file_path': edit_file,
                        'old_text': old_text,
                        'new_text': new_text
                    })
        
        return edits
    
    def _parallel_review_directory(self, directory: str, files: List[str]) -> List[Dict[str, Any]]:
        """
        Review multiple files in parallel and collect proposed edits.
        
        Uses dynamic parallelism based on server capacity metrics when available.
        
        Args:
            directory: Current directory being reviewed
            files: List of file paths to review
            
        Returns:
            List of all proposed edits from all files
        """
        if not files:
            return []
        
        all_edits = []
        
        # Determine worker count based on mode
        if self._dynamic_parallelism:
            # Re-check server metrics for current capacity (may have changed)
            try:
                recommended = self.ollama.get_recommended_parallelism(max_parallel=16)
                workers = min(recommended, len(files))
                if workers != self.max_parallel_files:
                    print(f"\n*** Dynamic parallelism updated: {workers} workers (was {self.max_parallel_files})")
                    self.max_parallel_files = workers
            except Exception as e:
                workers = min(self.max_parallel_files, len(files))
                logger.debug(f"Could not refresh server metrics: {e}")
        else:
            # Static mode - use configured value
            workers = min(self.max_parallel_files, len(files))
        
        print(f"*** Parallel review: {len(files)} files with {workers} workers")
        logger.info(f"Starting parallel review of {len(files)} files with {workers} workers")
        
        # Reset interrupt flag at start of parallel work
        self._interrupted = False
        
        with ThreadPoolExecutor(max_workers=workers) as executor:
            # Submit all file review tasks
            future_to_file = {
                executor.submit(self._review_single_file, f): f
                for f in files
            }
            self._active_futures = list(future_to_file.keys())
            
            # Collect results as they complete
            completed = 0
            cancelled = 0
            try:
                for future in as_completed(future_to_file):
                    if self._interrupted:
                        # Cancel remaining futures
                        for f in future_to_file.keys():
                            if not f.done():
                                f.cancel()
                                cancelled += 1
                        break
                    
                    file_path = future_to_file[future]
                    completed += 1
                    try:
                        edits = future.result()
                        if edits:
                            all_edits.extend(edits)
                            print(f"    [{completed}/{len(files)}] {file_path}: {len(edits)} edit(s)")
                        else:
                            print(f"    [{completed}/{len(files)}] {file_path}: no changes")
                    except Exception as e:
                        logger.error(f"Parallel review failed for {file_path}: {e}")
                        print(f"    [{completed}/{len(files)}] {file_path}: ERROR - {e}")
            except KeyboardInterrupt:
                print("\n*** Interrupt received - cancelling pending reviews...")
                self._interrupted = True
                for f in future_to_file.keys():
                    if not f.done():
                        f.cancel()
                        cancelled += 1
            finally:
                self._active_futures = []
        
        if self._interrupted:
            print(f"*** Parallel review interrupted: {completed} completed, {cancelled} cancelled")
            print(f"*** No edits will be applied (interrupted before completion)")
            logger.info(f"Parallel review interrupted: {completed}/{len(files)} completed, {cancelled} cancelled")
            return []  # Return empty - don't apply partial edits
        
        print(f"*** Parallel review complete: {len(all_edits)} total edits proposed")
        logger.info(f"Parallel review complete: {len(all_edits)} edits from {len(files)} files")
        
        return all_edits
    
    def _gather_additional_files_for_batch(self, current_dir: str, needed: int) -> List[str]:
        """
        Gather additional files from upcoming directories to fill a parallel batch.
        
        This improves GPU utilization when the current directory has few files.
        Only reviews code files, and only from directories that haven't been reviewed.
        
        Args:
            current_dir: Current directory being reviewed
            needed: Number of additional files needed to fill the batch
            
        Returns:
            List of additional file paths from upcoming directories
        """
        if needed <= 0:
            return []
        
        additional_files = []
        
        # Get pending directories from the index
        pending_dirs = []
        for entry in self.index.entries:
            if entry.status == 'pending' and entry.path != current_dir:
                pending_dirs.append(entry.path)
        
        for upcoming_dir in pending_dirs:
            if len(additional_files) >= needed:
                break
            
            dir_path = self.source_root / upcoming_dir
            if not dir_path.exists() or not dir_path.is_dir():
                continue
            
            # Find code files in this directory (respecting .gitignore)
            for item in sorted(dir_path.iterdir()):
                if len(additional_files) >= needed:
                    break
                if not item.is_file() or item.name.startswith('.'):
                    continue
                
                rel_path = str(item.relative_to(self.source_root))
                
                # Skip files ignored by .gitignore
                if self.git.is_ignored(rel_path):
                    continue
                
                suffix = item.suffix.lower()
                if suffix in {'.c', '.h', '.cc', '.cpp', '.rs', '.go'}:
                    additional_files.append(rel_path)
                    logger.debug(f"Adding {rel_path} to batch from {upcoming_dir}")
        
        if additional_files:
            logger.info(f"Batched {len(additional_files)} additional files from upcoming directories")
        
        return additional_files
    
    def _apply_parallel_edits(self, edits: List[Dict[str, Any]]) -> Tuple[int, int, List[str]]:
        """
        Apply edits collected from parallel review.
        
        Args:
            edits: List of edit dictionaries
            
        Returns:
            Tuple of (successful_edits, failed_edits, changed_files)
        """
        successful = 0
        failed = 0
        changed_files = []
        
        for edit in edits:
            file_path = edit['file_path']
            old_text = edit['old_text']
            new_text = edit['new_text']
            
            path = self._resolve_path(file_path)
            
            with self._edit_lock if self._edit_lock else threading.Lock():
                success, msg, diff = self.editor.edit_file(path, old_text, new_text)
            
            if success:
                successful += 1
                if file_path not in changed_files:
                    changed_files.append(file_path)
                logger.info(f"Applied edit to {file_path}")
            else:
                failed += 1
                logger.warning(f"Failed to apply edit to {file_path}: {msg}")
        
        return successful, failed, changed_files
    
    def _identify_failing_files(self, build_result: 'BuildResult') -> Set[str]:
        """
        Identify which changed files are causing build failures.
        
        Parses compiler errors to extract file paths and cross-references
        with the list of files we've modified.
        
        Args:
            build_result: BuildResult from the failed build
            
        Returns:
            Set of file paths (relative to source root) that have errors
        """
        failing_files = set()
        
        for error in build_result.errors:
            if error.severity == 'error' and error.file_path:
                # Normalize path - may be absolute or relative
                error_path = Path(error.file_path)
                
                # Try to make it relative to source root
                try:
                    if error_path.is_absolute():
                        rel_path = str(error_path.relative_to(self.source_root))
                    else:
                        rel_path = str(error_path)
                except ValueError:
                    rel_path = str(error_path)
                
                # Check if this is one of our changed files
                for changed_file in self.session.changed_files:
                    if changed_file == rel_path or changed_file.endswith('/' + rel_path) or rel_path.endswith('/' + changed_file):
                        failing_files.add(changed_file)
                        break
                    # Also check by filename only (for errors that don't include full path)
                    if Path(changed_file).name == Path(rel_path).name:
                        failing_files.add(changed_file)
                        break
        
        return failing_files
    
    def _selective_revert_and_commit(self, build_result: 'BuildResult') -> Tuple[List[str], List[str], str]:
        """
        Selectively revert only failing files, keep and commit successful changes.
        
        This is smarter than reverting everything - we identify which files
        caused build errors, revert only those, and commit the rest.
        
        Args:
            build_result: BuildResult from the failed build
            
        Returns:
            Tuple of (reverted_files, committed_files, commit_message)
        """
        failing_files = self._identify_failing_files(build_result)
        all_changed = set(self.session.changed_files)
        
        # Files that built successfully
        successful_files = all_changed - failing_files
        
        reverted_files = []
        committed_files = []
        commit_message = ""
        
        print(f"\n*** Selective revert: {len(failing_files)} failing, {len(successful_files)} successful")
        
        # Revert only the failing files
        if failing_files:
            print("*** Reverting failing files:")
            for file_path in failing_files:
                full_path = self.source_root / file_path
                if full_path.exists():
                    code, output = self.git._run(['checkout', str(full_path)])
                    if code == 0:
                        print(f"    Reverted: {file_path}")
                        reverted_files.append(file_path)
                    else:
                        print(f"    WARNING: Could not revert {file_path}: {output}")
            
            # Record lessons for the failed files
            error_report = build_result.get_error_report()
            self._record_lesson(error_report, failed_fix_attempt=", ".join(reverted_files))
        
        # Commit successful files if any
        if successful_files:
            print("*** Committing successful changes:")
            for file_path in successful_files:
                print(f"    Keeping: {file_path}")
                committed_files.append(file_path)
            
            # Stage successful files
            for file_path in successful_files:
                self.git.add(str(self.source_root / file_path))
            
            # Generate commit message
            dirs_affected = set(str(Path(f).parent) for f in successful_files)
            # Get the diff for the staged files
            full_diff = self.git.diff_staged()
            commit_message = self._generate_commit_message(full_diff, list(successful_files))
            
            # Commit
            success, output = self.git.commit(commit_message)
            if success:
                self.git.push()
                print(f"*** Committed {len(successful_files)} successful changes")

                # Get commit hash for tracking
                _, commit_hash = self.git._run(['rev-parse', 'HEAD'])
                commit_hash = commit_hash.strip()[:12]

                # Update review summary for successful directories
                for dir_path in dirs_affected:
                    self._update_review_summary(
                        [f for f in successful_files if f.startswith(dir_path)],
                        commit_message,
                        dir_path
                    )
                    # Mark directory complete in index if all its files succeeded
                    dir_files = [f for f in all_changed if f.startswith(dir_path)]
                    if all(f in successful_files for f in dir_files):
                        self.index.mark_done(dir_path, f"Completed via commit {commit_hash}")
                        self._beads_mark_completed(dir_path, commit_hash)
            else:
                print(f"*** Commit failed: {output}")
        
        # Update session state
        self.session.changed_files = list(reverted_files)  # Only failing files remain "changed" but reverted
        self.session.pending_changes = len(reverted_files) > 0
        
        return reverted_files, committed_files, commit_message
    
    def _log_exchange(self, step: int, request: str, response: str) -> None:
        """Log conversation exchange to file."""
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = self.log_dir / f"step_{step:04d}_{timestamp}.txt"
        
        with open(log_file, 'w') as f:
            f.write(f"=== STEP {step} ===\n\n")
            f.write("--- REQUEST ---\n")
            f.write(request)
            f.write("\n\n--- RESPONSE ---\n")
            f.write(response)

    def _format_response_for_console(self, response: str) -> str:
        """Collapse noisy code blocks before printing to stdout."""
        def _collapse_block(match: re.Match) -> str:
            lang = match.group(1).strip().lower()
            body = match.group(2)
            if lang == 'diff':
                return match.group(0)
            line_count = len(body.splitlines())
            return f"```{lang}\n[... {line_count} lines hidden; see persona logs ...]\n```"

        sanitized = CODE_BLOCK_RE.sub(_collapse_block, response)
        max_chars = 2000
        if len(sanitized) > max_chars:
            trimmed = sanitized[:max_chars].rstrip()
            hidden = len(sanitized) - max_chars
            return f"{trimmed}\n... [truncated {hidden} chars; see persona logs for full output]"
        return sanitized
    
    def _resolve_path(self, path_str: str) -> Path:
        """Resolve a relative path within the source tree."""
        path = Path(path_str)
        resolved = path.resolve() if path.is_absolute() else (self.source_root / path).resolve()

        try:
            rel = resolved.relative_to(self.source_root)
        except ValueError:
            raise ValueError(f"Path escapes source root: {path_str}")

        # Never allow interacting with git metadata; it is easy for the AI to corrupt it.
        if rel.parts and rel.parts[0] == '.git':
            raise ValueError(f"Refusing to access git metadata: {rel}")

        return resolved
    
    def _ask_ai_simple(self, prompt: str) -> str:
        """Make a simple one-shot query to the AI (no conversation history)."""
        messages = [
            {"role": "system", "content": "You are a helpful assistant. Be concise and direct."},
            {"role": "user", "content": prompt}
        ]
        try:
            return self.ollama.chat(messages)
        except Exception as e:
            logger.error(f"AI query failed: {e}")
            return ""
    
    def _generate_commit_message(self, diff: str, changed_files: List[str], 
                                   directory: Optional[str] = None) -> str:
        """Ask the AI to generate a commit message based on the diff."""
        print("\n*** Generating commit message...")
        
        # Truncate diff if too long
        max_diff_len = 8000
        if len(diff) > max_diff_len:
            diff = diff[:max_diff_len] + "\n... [diff truncated] ..."
        
        files_list = ", ".join(changed_files[:10])
        if len(changed_files) > 10:
            files_list += f" and {len(changed_files) - 10} more"
        
        # Determine component name from directory or files
        if directory:
            # Extract component name (e.g., "bin/cpuset" -> "cpuset")
            component = directory.split('/')[-1] if '/' in directory else directory
        else:
            # Try to extract from first file path
            if changed_files:
                parts = changed_files[0].split('/')
                component = parts[1] if len(parts) > 1 else parts[0]
            else:
                component = "various"
        
        prompt = f"""Generate a git commit message for these FreeBSD source code changes.

Component/Directory: {directory or 'various'}
Changed files: {files_list}

Diff:
```diff
{diff}
```

Write a commit message following these rules:
1. First line: "[ai-code-reviewer] {component}: <short summary>" (72 chars max total)
2. Blank line
3. Body: explain WHAT changed and WHY (wrap at 72 chars)
4. Focus on the security/correctness fixes, not style changes
5. Use imperative mood ("Fix" not "Fixed")
6. This commit covers ALL changes in the {component} directory

Example format:
[ai-code-reviewer] cpuset: Replace atoi() with strtonum()

- atoi() doesn't detect overflow or invalid input
- strtonum() provides proper bounds checking and error reporting
- Fixes potential integer overflow vulnerability
- Also fixed unchecked printf() calls

Output ONLY the commit message, no other text."""

        message = self._ask_ai_simple(prompt)
        
        # Clean up the response - remove any markdown formatting
        message = message.strip()
        if message.startswith("```"):
            lines = message.split("\n")
            message = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
        
        # If we got a reasonable message, use it; otherwise fall back
        if message and len(message) > 10:
            message = self.git.ensure_commit_prefix(message)
            print(f"*** Commit message:\n{message}\n")
            return message
        else:
            fallback = f"{component}: Code review fixes\n\nFiles: {files_list}"
            return self.git.ensure_commit_prefix(fallback)
    
    def _record_lesson(self, error_report: str, failed_fix_attempt: str = "") -> None:
        """Record a lesson learned from a build failure to LESSONS.md."""
        print("\n*** Recording lesson learned...")

        # Record in metrics
        self.metrics.record_lesson()

        # Truncate if needed
        if len(error_report) > 4000:
            error_report = error_report[:4000] + "\n... [truncated] ..."
        
        prompt = f"""A FreeBSD build failed. Extract a concise lesson learned.

Build errors:
```
{error_report}
```

{f"Fix attempt that failed: {failed_fix_attempt}" if failed_fix_attempt else ""}

Write a SHORT lesson (2-4 lines) in this format:
### [Category]: Brief title
- What went wrong
- How to avoid it next time

Categories: COMPILER, HEADERS, SYNTAX, LOGIC, STYLE

Output ONLY the lesson entry, nothing else."""

        lesson = self._ask_ai_simple(prompt)
        
        if not lesson or len(lesson) < 10:
            lesson = f"### BUILD: Build failure\n- Error occurred during buildworld\n- Review compiler output carefully"
        
        # Append to LESSONS.md
        lessons_path = self.lessons_file
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        
        entry = f"\n\n## {timestamp}\n{lesson.strip()}\n"
        
        success, msg = self.editor.append_to_file(lessons_path, entry)
        if success:
            print(f"*** Lesson recorded to {self.lessons_file.relative_to(self.source_root)}")
        else:
            logger.warning(f"Failed to record lesson: {msg}")
    
    def _update_review_summary(self, changed_files: List[str], commit_message: str,
                                directory: Optional[str] = None) -> None:
        """Update REVIEW-SUMMARY.md with the completed directory review."""
        print("\n*** Updating REVIEW-SUMMARY.md...")
        
        summary_path = self.review_summary_file
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        
        # Extract first line of commit message for summary
        summary_line = commit_message.split('\n')[0]
        
        files_fixed = "\n".join(f"  - {f}" for f in changed_files[:10])
        if len(changed_files) > 10:
            files_fixed += f"\n  - ... and {len(changed_files) - 10} more"
        
        dir_info = f"**Directory:** `{directory}`\n\n" if directory else ""
        
        entry = f"""
## {timestamp} - {directory or 'Build'} ✓

{dir_info}**Summary:** {summary_line}

**Files fixed:**
{files_fixed}

---
"""
        
        # Read current content and prepend new entry after the header
        try:
            current = summary_path.read_text(encoding='utf-8')
            # Find end of header (after first ---)
            parts = current.split('---', 2)
            if len(parts) >= 2:
                # Insert after header
                new_content = parts[0] + '---' + entry + '---'.join(parts[1:])
            else:
                new_content = current + entry
            
            summary_path.write_text(new_content, encoding='utf-8')
            print(f"*** Updated {self.review_summary_file.relative_to(self.source_root)}")
        except Exception as e:
            logger.warning(f"Failed to update review summary: {e}")
    
    def _run_build_with_live_output(self) -> 'BuildResult':
        """
        Run the build command with LIVE output to terminal.
        """
        from build_executor import BuildResult, CompilerError, ErrorParser
        import time
        
        command = self.builder.config.build_command
        source_root = self.builder.config.source_root
        
        print("\n" + "=" * 60)
        print("RUNNING BUILD")
        print("=" * 60)
        print(f"Command: {command}")
        print(f"Directory: {source_root}")
        print("=" * 60 + "\n")
        
        start_time = time.time()
        
        try:
            process = subprocess.Popen(
                command,
                shell=True,
                cwd=str(source_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            
            output_lines = []
            
            for line in iter(process.stdout.readline, ''):
                print(line, end='')
                output_lines.append(line)
                
                if len(output_lines) > 5000:
                    output_lines = output_lines[-4000:]
            
            process.wait()
            elapsed = time.time() - start_time
            
            raw_output = ''.join(output_lines)
            errors, warnings = ErrorParser.parse_output(raw_output)
            
            print("\n" + "=" * 60)
            if process.returncode == 0:
                print(f"BUILD SUCCEEDED in {elapsed:.1f}s ({len(warnings)} warnings)")
            else:
                print(f"BUILD FAILED in {elapsed:.1f}s ({len(errors)} errors, {len(warnings)} warnings)")
            print("=" * 60 + "\n")
            
            return BuildResult(
                success=(process.returncode == 0),
                return_code=process.returncode,
                duration_seconds=elapsed,
                errors=errors,
                warnings=warnings,
                raw_output=raw_output,
                truncated=(len(output_lines) >= 4000),
            )
            
        except Exception as e:
            elapsed = time.time() - start_time
            print(f"\nBUILD ERROR: {e}\n")
            return BuildResult(
                success=False,
                return_code=-1,
                duration_seconds=elapsed,
                errors=[CompilerError(
                    file_path="(build system)",
                    line_number=0,
                    column=None,
                    severity="error",
                    message=f"Build system error: {e}",
                )],
            )
    
    def _commit_and_push(self, message: str) -> Tuple[bool, str]:
        """Stage all changes, commit with message, and push."""
        print("\n*** Committing changes...")

        ok, prep_msg = self.git.ensure_repository_ready(allow_rebase=False)
        if not ok:
            return False, f"Repository not ready: {prep_msg}"
        if prep_msg:
            print(f"*** Repository ready: {prep_msg}")
        
        if not self.git.add_all():
            return False, "Failed to stage changes"
        
        success, output = self.git.commit(message)
        if not success:
            return False, f"Failed to commit: {output}"
        
        print(f"*** Committed!")
        
        # Pull with rebase before push to handle concurrent changes
        print("*** Pulling latest changes...")
        success, output = self.git.pull_rebase()
        if not success:
            print(f"*** Warning: pull --rebase failed: {output}")
            self.git.abort_rebase_if_needed()
            return False, f"Failed to rebase before push: {output}"
        
        print("*** Pushing to origin...")
        success, output = self.git.push()
        if not success:
            # Retry once after pull
            print("*** Push failed, trying pull --rebase again...")
            rebase_ok, rebase_output = self.git.pull_rebase()
            if not rebase_ok:
                self.git.abort_rebase_if_needed()
                return False, f"Failed to rebase during push retry: {rebase_output}"
            success, output = self.git.push()
            if not success:
                self.git.ensure_repository_ready()
                return False, f"Failed to push after retry: {output}"
        
        print("*** Pushed successfully!")
        return True, output
    
    def _get_action_hash(self, action: Dict[str, Any]) -> str:
        """Generate a hash representing the action for loop detection."""
        action_type = action.get('action', '')
        # Include relevant details that make this action unique
        if action_type == 'READ_FILE':
            return f"READ_FILE:{action.get('file_path', '')}"
        elif action_type == 'EDIT_FILE':
            return f"EDIT_FILE:{action.get('file_path', '')}"
        elif action_type == 'SET_SCOPE':
            return f"SET_SCOPE:{action.get('directory', '')}"
        elif action_type == 'NEXT_CHUNK':
            return f"NEXT_CHUNK:{self.current_chunk_index}"
        else:
            return f"{action_type}"
    
    def _check_for_loop(self, action: Dict[str, Any]) -> Optional[str]:
        """
        Check if we're stuck in a loop (same action repeated too many times).
        
        Returns:
            None if OK, warning message if loop detected (warns first),
            triggers automatic recovery after MAX_BEFORE_RECOVERY warnings
        """
        action_hash = self._get_action_hash(action)
        
        # Track consecutive identical actions
        if action_hash == self.session.last_action_hash:
            self.session.consecutive_identical_actions += 1
        else:
            self.session.consecutive_identical_actions = 1
            self.session.last_action_hash = action_hash
        
        # Add to history (keep last 20)
        self.session.action_history.append((action.get('action', ''), action_hash))
        if len(self.session.action_history) > 20:
            self.session.action_history = self.session.action_history[-20:]
        
        # Check for infinite loop pattern
        MAX_CONSECUTIVE_WARNING = 5  # Warn at 5 repetitions
        MAX_CONSECUTIVE_RECOVERY = 10  # Force recovery at 10 repetitions
        
        if self.session.consecutive_identical_actions >= MAX_CONSECUTIVE_RECOVERY:
            # Automatic recovery - things are seriously stuck
            logger.error(f"Action {action_hash} repeated {self.session.consecutive_identical_actions} times - forcing recovery")
            return self._recover_from_loop(action)
        
        elif self.session.consecutive_identical_actions >= MAX_CONSECUTIVE_WARNING:
            action_type = action.get('action', '')
            
            # Special case: if stuck on READ_FILE, it's likely trying to verify a fix that never happened
            if action_type == 'READ_FILE':
                file_path = action.get('file_path', '')
                return (
                    f"\n{'='*70}\n"
                    f"⚠️  INFINITE LOOP WARNING (attempt {self.session.consecutive_identical_actions}/{MAX_CONSECUTIVE_RECOVERY}) ⚠️\n"
                    f"{'='*70}\n\n"
                    f"You have READ the same file {self.session.consecutive_identical_actions} times in a row:\n"
                    f"  {file_path}\n\n"
                    f"This suggests you are:\n"
                    f"1. Detecting a problem in the file\n"
                    f"2. Saying you'll fix it\n"
                    f"3. But then just reading it again instead of fixing it\n\n"
                    f"BREAKING THE LOOP:\n\n"
                    f"If there's a merge conflict or error in the file:\n"
                    f"  ACTION: EDIT_FILE {file_path}\n"
                    f"  OLD:\n"
                    f"  <<<\n"
                    f"  [copy the EXACT problematic section including context]\n"
                    f"  >>>\n"
                    f"  NEW:\n"
                    f"  <<<\n"
                    f"  [corrected version]\n"
                    f"  >>>\n\n"
                    f"If the file is beyond repair:\n"
                    f"  ACTION: SKIP_FILE\n\n"
                    f"If the directory is problematic:\n"
                    f"  ACTION: SET_SCOPE <different-directory>\n\n"
                    f"WARNING: If you repeat this action {MAX_CONSECUTIVE_RECOVERY - self.session.consecutive_identical_actions} more times,\n"
                    f"automatic recovery will be triggered and progress will be lost.\n"
                    f"{'='*70}\n"
                )
            
            # Generic loop warning
            return (
                f"\n{'='*70}\n"
                f"⚠️  INFINITE LOOP WARNING (attempt {self.session.consecutive_identical_actions}/{MAX_CONSECUTIVE_RECOVERY}) ⚠️\n"
                f"{'='*70}\n\n"
                f"Action {action_type} has been repeated {self.session.consecutive_identical_actions} times.\n"
                f"Details: {action_hash}\n\n"
                f"You must take a DIFFERENT action to break the loop.\n"
                f"Consider:\n"
                f"- Moving to a different file (READ_FILE <different-file>)\n"
                f"- Skipping the current file (SKIP_FILE)\n"
                f"- Changing directory (SET_SCOPE <different-directory>)\n"
                f"- Running a build if you have changes (BUILD)\n\n"
                f"WARNING: If you repeat this action {MAX_CONSECUTIVE_RECOVERY - self.session.consecutive_identical_actions} more times,\n"
                f"automatic recovery will be triggered.\n"
                f"{'='*70}\n"
            )
        
        return None
    
    def _execute_action(self, action: Dict[str, Any]) -> str:
        """Execute an action and return the result."""
        action_type = action.get('action', '')
        
        # Check for infinite loop before executing
        loop_warning = self._check_for_loop(action)
        if loop_warning:
            logger.warning(f"Loop detected: {action_type} repeated {self.session.consecutive_identical_actions} times")
            return loop_warning
        
        if action_type == 'FIND_FILE':
            pattern = action.get('pattern', '')
            if not pattern:
                return "FIND_FILE_ERROR: No pattern specified"
            
            # Use find command to search
            # Convert simple patterns to find patterns
            if '*' not in pattern and '?' not in pattern:
                pattern = f"*{pattern}*"
            
            try:
                result = subprocess.run(
                    ['find', '.', '-name', pattern, '-type', 'f'],
                    cwd=str(self.source_root),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                files = [f.strip() for f in result.stdout.strip().split('\n') if f.strip()]
                files = files[:20]  # Limit results
                
                if files:
                    return f"FIND_FILE_RESULT for '{pattern}':\n```\n" + '\n'.join(files) + "\n```"
                else:
                    return f"FIND_FILE_RESULT: No files matching '{pattern}'"
            except subprocess.TimeoutExpired:
                return "FIND_FILE_ERROR: Search timed out"
            except Exception as e:
                return f"FIND_FILE_ERROR: {e}"
        
        elif action_type == 'GREP':
            pattern = action.get('pattern', '')
            if not pattern:
                return "GREP_ERROR: No pattern specified"
            
            try:
                # Use grep -rn with sensible defaults for C source
                result = subprocess.run(
                    ['grep', '-rn', '--include=*.c', '--include=*.h', 
                     '-m', '3',  # Max 3 matches per file
                     pattern, '.'],
                    cwd=str(self.source_root),
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                
                output = result.stdout.strip()
                lines = output.split('\n')
                if len(lines) > 50:
                    output = '\n'.join(lines[:50]) + f"\n... [{len(lines) - 50} more matches]"
                
                if output:
                    return f"GREP_RESULT for '{pattern}':\n```\n{output}\n```"
                else:
                    return f"GREP_RESULT: No matches for '{pattern}'"
            except subprocess.TimeoutExpired:
                return "GREP_ERROR: Search timed out"
            except Exception as e:
                return f"GREP_ERROR: {e}"
        
        elif action_type == 'SET_SCOPE':
            directory = action.get('directory', '').strip()
            if not directory:
                return "SET_SCOPE_ERROR: No directory specified"
            
            # Normalize path (remove leading ./ or /)
            directory = directory.lstrip('./')

            if self._should_auto_skip(directory):
                attempts = self._get_retry_record(directory).get('attempts', 0)
                self.index.mark_skipped(directory, reason=f"Auto-skipped after {attempts} retries")
                self.index.save()
                next_dir = self.index.get_next_pending()
                skip_msg = (
                    f"AUTO_SKIP: Directory {directory} skipped automatically after {attempts} failed attempts.\n\n"
                    f"To retry it later, remove or edit {self.retry_tracker_path.name}."
                )
                if next_dir:
                    skip_msg += f"\nPlease choose a different directory, e.g., ACTION: SET_SCOPE {next_dir}"
                else:
                    skip_msg += "\nNo other pending directories remain."
                return skip_msg
            
            # IMPORTANT: Prevent changing directories with uncommitted changes
            if self.session.current_directory and self.session.current_directory != directory:
                if self.session.pending_changes or self.git.has_changes():
                    return (
                        f"SET_SCOPE_ERROR: Cannot change directory with uncommitted changes\n\n"
                        f"Current directory: {self.session.current_directory}\n"
                        f"Pending changes: {len(self.session.changed_files)} files modified\n"
                        f"Files: {', '.join(self.session.changed_files)}\n\n"
                        f"You MUST complete the current directory first:\n"
                        f"1. Review all files in {self.session.current_directory}\n"
                        f"2. Run ACTION: BUILD to test changes\n"
                        f"3. If build fails: fix errors and BUILD again\n"
                        f"4. If build succeeds: changes will be committed automatically\n"
                        f"5. THEN you can move to: {directory}\n\n"
                        f"BUILD and COMMIT happen at directory level!\n"
                        f"Each directory is one logical unit.\n"
                    )
            
            # Verify directory exists
            dir_path = self.source_root / directory
            if not dir_path.exists():
                return f"SET_SCOPE_ERROR: Directory not found: {directory}\nTIP: Use LIST_DIR to see available directories"
            if not dir_path.is_dir():
                return f"SET_SCOPE_ERROR: Not a directory: {directory}"
            
            # Check for Makefile (indicates it's a proper source directory)
            has_makefile = (dir_path / 'Makefile').exists() or (dir_path / 'Makefile.inc').exists()
            
            # Discover all reviewable files in directory (respecting .gitignore)
            files_in_dir = []
            for item in sorted(dir_path.iterdir()):
                if not item.is_file():
                    continue
                # Skip hidden files and .git directory
                if item.name.startswith('.'):
                    continue
                
                rel_path = str(item.relative_to(self.source_root))
                
                # Skip files ignored by .gitignore
                if self.git.is_ignored(rel_path):
                    logger.debug(f"Skipping gitignored file: {rel_path}")
                    continue
                
                suffix = item.suffix.lower()

                # Skip excluded file types (test data, output files, etc.)
                if suffix in EXCLUDED_SUFFIXES:
                    logger.debug(f"Skipping excluded file type {suffix}: {rel_path}")
                    continue

                if suffix in REVIEWABLE_SUFFIXES or item.name in REVIEWABLE_SPECIAL_FILES:
                    files_in_dir.append(rel_path)
            
            # Update session state
            self.session.current_directory = directory
            self.session.files_in_current_directory = files_in_dir
            self.session.files_reviewed_in_directory = 0
            self.session.current_file = None
            self.session.current_file_chunks_total = 0
            self.session.current_file_chunks_reviewed = 0
            self.session.changed_files = []  # Reset changed files for new scope
            self.session.pending_changes = False
            self.session.visited_files_in_directory = set()
            
            # Update the review index to track current position
            self.index.set_current(directory)
            self.index.save()
            self._beads_mark_in_progress(directory)
            
            # Log directory start
            self.ops.directory_start(directory)
            
            progress = self.session.get_progress_summary()
            
            result = f"SET_SCOPE_OK: Now reviewing {directory}\n\n"
            result += f"HIERARCHY:\n"
            result += f"  Level 1: Source tree ({len(self.index.entries)} directories)\n"
            result += f"  Level 2: {directory} ← YOU ARE HERE\n"
            result += f"  Level 3: {len(files_in_dir)} reviewable files\n"
            result += f"  Level 4: Functions (auto-chunked for large files)\n\n"
            
            if has_makefile:
                result += f"✓ Directory has Makefile - valid source module\n\n"
            else:
                result += f"⚠ No Makefile - may be subdirectory\n\n"
            
            result += f"FILES TO REVIEW:\n"
            if files_in_dir:
                for f in files_in_dir:
                    result += f"  - {f}\n"
            else:
                result += "  (No reviewable text files detected. Directory may be an intermediate container.)\n"
            
            result += f"\n{progress}\n\n"
            result += f"WORKFLOW:\n"
            result += f"1. Review each file (READ_FILE, NEXT_CHUNK for large files)\n"
            result += f"2. Make edits as needed (EDIT_FILE)\n"
            result += f"3. When ALL files reviewed: ACTION: BUILD\n"
            result += f"4. If build succeeds: Changes committed for entire directory\n"
            result += f"5. Move to next directory (SET_SCOPE)\n"
            
            print(f"\n*** Scope set to: {directory}")

            attempt_num = self._record_directory_attempt(directory)
            if self.max_directory_retries > 0:
                remaining = max(self.max_directory_retries - attempt_num, 0)
                result += f"\nAttempts recorded for {directory}: {attempt_num}/{self.max_directory_retries}."
                if remaining == 0:
                    result += "\nNext attempt will auto-skip this directory."
                else:
                    result += f"\n{remaining} attempt(s) remain before auto-skip."
            
            # Check for cached edits from previous batch reviews
            cached_edits_for_dir = []
            if hasattr(self, '_cached_edits') and directory in self._cached_edits:
                cached_edits_for_dir = self._cached_edits.pop(directory)
                logger.info(f"Found {len(cached_edits_for_dir)} cached edits for {directory}")
            
            # Parallel review mode: automatically review all files in parallel
            if self._parallel_mode and files_in_dir:
                # Filter to only .c and .h files for parallel review (skip docs)
                code_files = [f for f in files_in_dir 
                              if f.endswith(('.c', '.h', '.cc', '.cpp', '.rs', '.go'))]
                
                # If we have cached edits, use them instead of re-reviewing
                if cached_edits_for_dir:
                    result += f"\n\n*** USING CACHED REVIEW RESULTS ***\n"
                    result += f"Found {len(cached_edits_for_dir)} pre-reviewed edits for this directory\n"
                    
                    # Apply cached edits
                    successful, failed, changed = self._apply_parallel_edits(cached_edits_for_dir)
                    
                    result += f"\n*** Applied cached review results:\n"
                    result += f"    Successfully applied: {successful}\n"
                    result += f"    Failed to apply: {failed}\n"
                    
                    if changed:
                        result += f"    Files modified: {', '.join(changed)}\n"
                        self.session.pending_changes = True
                        self.session.changed_files.extend(changed)
                    
                    for f in code_files:
                        self.session.visited_files_in_directory.add(f)
                    self.session.files_reviewed_in_directory = len(code_files)
                    
                    result += f"\nAll files from cache. Run BUILD to validate changes.\n"
                    result += f"ACTION: BUILD\n"
                    return result
                
                if code_files:
                    # Check if we should batch with additional directories for better GPU utilization
                    min_batch_size = self.max_parallel_files if self.max_parallel_files > 1 else 8
                    files_to_review = list(code_files)  # Start with current directory
                    
                    # If current directory has few files, peek at upcoming directories
                    if len(files_to_review) < min_batch_size:
                        additional_files = self._gather_additional_files_for_batch(
                            directory, 
                            min_batch_size - len(files_to_review)
                        )
                        if additional_files:
                            files_to_review.extend(additional_files)
                            result += f"\n\n*** BATCHED PARALLEL REVIEW MODE ***\n"
                            result += f"Batching {len(code_files)} files from {directory} + {len(additional_files)} from upcoming directories\n"
                            result += f"Total: {len(files_to_review)} files for concurrent review...\n"
                        else:
                            result += f"\n\n*** PARALLEL REVIEW MODE ***\n"
                            result += f"Reviewing {len(code_files)} code files concurrently...\n"
                    else:
                        result += f"\n\n*** PARALLEL REVIEW MODE ***\n"
                        result += f"Reviewing {len(code_files)} code files concurrently...\n"
                    
                    # Run parallel review
                    edits = self._parallel_review_directory(directory, files_to_review)
                    
                    if edits:
                        # Separate edits: current directory vs batched from other directories
                        current_dir_edits = [e for e in edits if e['file_path'].startswith(directory + '/') or 
                                             '/' not in e['file_path'].replace(directory, '', 1).lstrip('/')]
                        other_dir_edits = [e for e in edits if e not in current_dir_edits]
                        
                        # Cache edits from other directories for later
                        if other_dir_edits:
                            if not hasattr(self, '_cached_edits'):
                                self._cached_edits = {}
                            for edit in other_dir_edits:
                                file_dir = str(Path(edit['file_path']).parent)
                                if file_dir not in self._cached_edits:
                                    self._cached_edits[file_dir] = []
                                self._cached_edits[file_dir].append(edit)
                            logger.info(f"Cached {len(other_dir_edits)} edits for later directories")
                        
                        # Apply only current directory's edits
                        successful, failed, changed = self._apply_parallel_edits(current_dir_edits)
                        
                        result += f"\n*** Parallel review results:\n"
                        result += f"    Edits proposed: {len(current_dir_edits)} for current dir"
                        if other_dir_edits:
                            result += f" ({len(other_dir_edits)} cached for later)\n"
                        else:
                            result += "\n"
                        result += f"    Successfully applied: {successful}\n"
                        result += f"    Failed to apply: {failed}\n"
                        
                        if changed:
                            result += f"    Files modified: {', '.join(changed)}\n"
                            self.session.pending_changes = True
                            self.session.changed_files.extend(changed)
                        
                        # Mark files as reviewed
                        for f in code_files:
                            self.session.visited_files_in_directory.add(f)
                        self.session.files_reviewed_in_directory = len(code_files)
                        
                        result += f"\nAll files reviewed. Run BUILD to validate changes.\n"
                        result += f"ACTION: BUILD\n"
                    else:
                        result += f"\n*** Parallel review found no issues in {len(code_files)} files.\n"
                        for f in code_files:
                            self.session.visited_files_in_directory.add(f)
                        self.session.files_reviewed_in_directory = len(code_files)
                        result += f"Directory review complete. Move to next directory.\n"
            
            return result
        
        elif action_type == 'READ_FILE':
            try:
                path = self._resolve_path(action.get('file_path', ''))
            except ValueError as e:
                return f"READ_FILE_ERROR: {e}"
            if not path.exists():
                return f"READ_FILE_ERROR: File not found: {path}\nTIP: Use FIND_FILE to locate files"
            if path.is_dir():
                return f"READ_FILE_ERROR: {path} is a directory, not a file\nTIP: Use LIST_FILES to see directory contents"

            # Update session tracking
            rel_path = str(path.relative_to(self.source_root))
            self.session.current_file = rel_path
            self.session.visited_files_in_directory.add(rel_path)
            
            suffix = path.suffix.lower()
            if suffix in MANPAGE_SUFFIXES:
                self.session.files_reviewed_in_directory += 1
                self.session.current_file = None
                msg = (
                    f"READ_FILE_SKIPPED: {path} is documentation (suffix {suffix}).\n"
                    f"No code changes required. Marked as reviewed.\n"
                    f"Remaining files in {self.session.current_directory} you can work on:\n"
                    f"{self._remaining_files_summary()}\n\n"
                    f"Please choose another source file or SET_SCOPE to a new directory."
                )
                return msg

            # Check if file should be chunked (get appropriate chunker for file type)
            chunker = get_chunker(path, self.chunk_size, self.chunk_threshold)
            if chunker.should_chunk(path):
                # Start chunked review
                self.current_chunks = chunker.chunk_file(path)
                self.current_chunk_index = 0
                self.chunked_file_path = path
                
                # Update session tracking for chunked file
                self.session.current_file_chunks_total = len(self.current_chunks)
                self.session.current_file_chunks_reviewed = 1  # Reading chunk 1
                
                # Return first chunk
                chunk = self.current_chunks[0]
                total_chunks = len(self.current_chunks)
                
                progress = self.session.get_progress_summary()
                
                header = (
                    f"\n📋 CHUNKED FILE REVIEW MODE\n"
                    f"   File: {path}\n"
                    f"   Total chunks: {total_chunks}\n"
                    f"   Strategy: Function-by-function review\n"
                    f"   Use ACTION: NEXT_CHUNK to continue\n"
                    f"   Use ACTION: SKIP_FILE to move to next file\n\n"
                    f"PROGRESS:\n{progress}\n\n"
                )
                
                chunk_content = format_chunk_for_review(chunk, total_chunks, 1)
                return f"READ_FILE_RESULT for {path}:\n{header}```\n{chunk_content}\n```"
            
            # Small files: original behavior
            self.session.current_file_chunks_total = 1
            self.session.current_file_chunks_reviewed = 1
            
            file_size = path.stat().st_size
            line_count = len(path.read_text(encoding='utf-8', errors='replace').splitlines())
            
            progress = self.session.get_progress_summary()
            
            warning = ""
            if line_count > 500 or file_size > 20000:
                warning = (
                    f"\n⚠️  NOTE: Medium-sized file ({line_count} lines, {file_size} bytes)\n"
                    f"   Analysis may take 5-10 minutes.\n\n"
                )
            
            content = self.editor.read_file(path)
            return f"READ_FILE_RESULT for {path}:\n{warning}PROGRESS:\n{progress}\n\n```\n{content}\n```"
        
        elif action_type == 'LIST_DIR':
            try:
                path = self._resolve_path(action.get('dir_path', ''))
            except ValueError as e:
                return f"LIST_DIR_ERROR: {e}"
            if not path.exists():
                return f"LIST_DIR_ERROR: Directory not found: {path}"
            if not path.is_dir():
                return f"LIST_DIR_ERROR: Not a directory: {path}"
            items = sorted(os.listdir(path))
            return f"LIST_DIR_RESULT for {path}:\n```\n" + '\n'.join(items) + "\n```"
        
        elif action_type == 'EDIT_FILE':
            try:
                path = self._resolve_path(action.get('file_path', ''))
            except ValueError as e:
                return f"EDIT_FILE_ERROR: {e}"
            old_text = action.get('old_text', '')
            new_text = action.get('new_text', '')
            
            if not old_text or new_text is None:
                return "EDIT_FILE_ERROR: Missing OLD or NEW block"
            
            # Check if file is within current scope (warn but don't block)
            rel_path = str(path.relative_to(self.source_root))
            if self.session.current_directory:
                if not rel_path.startswith(self.session.current_directory):
                    print(f"\n*** WARNING: Editing {rel_path} outside current scope ({self.session.current_directory})")
            else:
                # Auto-detect scope from first edit
                parts = rel_path.split('/')
                if len(parts) >= 2:
                    auto_scope = '/'.join(parts[:2])  # e.g., "bin/cpuset"
                    self.session.current_directory = auto_scope
                    print(f"\n*** Auto-detected scope: {auto_scope}")
            
            success, message, diff = self.editor.edit_file(path, old_text, new_text)

            # Record edit attempt in metrics (we'll know if it caused build failure later)
            if success:
                self.metrics.record_edit(caused_build_failure=False)

            if success:
                # Reset edit failure tracking on success
                self.session.edit_failure_count = 0
                self.session.last_failed_edit_file = None

                self.session.pending_changes = True
                self.session.last_diff = diff
                if rel_path not in self.session.changed_files:
                    self.session.changed_files.append(rel_path)

                # Log edit success
                self.ops.edit_success(rel_path, message)
                
                result = f"EDIT_FILE_OK: {message}\n"
                if self.session.current_directory:
                    result += f"Scope: {self.session.current_directory}\n"
                result += "Diffs will be shown for all changed files after BUILD succeeds."
                
                print(f"\n*** Edited: {path}")
                return result
            else:
                # Track consecutive edit failures on same file
                if rel_path == self.session.last_failed_edit_file:
                    self.session.edit_failure_count += 1
                else:
                    self.session.edit_failure_count = 1
                    self.session.last_failed_edit_file = rel_path
                
                # Log edit failure
                self.ops.edit_failure(rel_path, message)
                
                # Check if stuck in edit-read-edit loop
                MAX_EDIT_FAILURES = 3
                if self.session.edit_failure_count >= MAX_EDIT_FAILURES:
                    logger.error(f"EDIT_FILE failed {self.session.edit_failure_count} times on {rel_path}")
                    
                    # File a beads issue for this edit failure pattern
                    if self.beads:
                        issue_desc = (
                            f"AI stuck in edit-read-edit failure loop.\n\n"
                            f"File: {rel_path}\n"
                            f"Failed attempts: {self.session.edit_failure_count}\n"
                            f"Error: {message}\n"
                            f"Directory: {self.session.current_directory or 'unknown'}\n"
                            f"Session: {self.session.session_id}\n\n"
                            f"This pattern suggests:\n"
                            f"- File content doesn't match AI expectations\n"
                            f"- Code already changed by previous edits\n"
                            f"- Whitespace/tab mismatches in OLD block\n"
                            f"- File too complex for reliable editing\n"
                            f"- AI not reading file carefully before editing"
                        )
                        self.beads.create_systemic_issue(
                            title=f"Edit failure loop on {rel_path}",
                            description=issue_desc,
                            issue_type='bug',
                            priority=2,
                            labels=['ai-behavior', 'edit-failure', 'file-mismatch']
                        )
                    
                    return (
                        f"EDIT_FILE_ERROR: {message}\n\n"
                        f"{'='*70}\n"
                        f"⚠️  EDIT FAILURE LOOP DETECTED ⚠️\n"
                        f"{'='*70}\n\n"
                        f"EDIT_FILE has failed {self.session.edit_failure_count} times on: {rel_path}\n"
                        f"Error: {message}\n\n"
                        f"This usually means:\n"
                        f"1. The file content doesn't match what you expect\n"
                        f"2. You're trying to edit code that's already been changed\n"
                        f"3. The OLD block has whitespace/tab mismatches\n"
                        f"4. The file is too complex to edit reliably\n\n"
                        f"BREAKING THE LOOP - Choose ONE:\n\n"
                        f"A) Skip this file and move on:\n"
                        f"   ACTION: SKIP_FILE\n\n"
                        f"B) Move to a different file:\n"
                        f"   ACTION: READ_FILE <different-file-in-directory>\n\n"
                        f"C) If directory is problematic, move to next:\n"
                        f"   ACTION: SET_SCOPE <different-directory>\n\n"
                        f"D) If you have other changes ready, build them:\n"
                        f"   ACTION: BUILD\n\n"
                        f"DO NOT:\n"
                        f"- Read the same file again (you've read it {self.session.edit_failure_count} times)\n"
                        f"- Try to edit it again without a different approach\n"
                        f"- Hallucinate code that doesn't exist in the file\n\n"
                        f"The file may already be correct, or too complex for automated editing.\n"
                        f"MOVE ON to make progress.\n"
                        f"{'='*70}\n"
                    )
                
                hint = "\n\nHINT: The OLD block must be EXACTLY copied from the file.\n" \
                       "Re-read the file and copy the exact text you want to replace,\n" \
                       "including all whitespace and indentation. Do not paraphrase."
                return f"EDIT_FILE_ERROR: {message}{hint}"
        
        elif action_type == 'WRITE_FILE':
            try:
                path = self._resolve_path(action.get('file_path', ''))
            except ValueError as e:
                return f"WRITE_FILE_ERROR: {e}"
            content = action.get('content', '')
            
            if not content:
                return "WRITE_FILE_ERROR: Missing CONTENT block"
            
            success, message, diff = self.editor.write_file(path, content)
            
            if success:
                self.session.pending_changes = True
                self.session.last_diff = diff
                if str(path) not in self.session.changed_files:
                    self.session.changed_files.append(str(path.relative_to(self.source_root)))
                
                result = f"WRITE_FILE_OK: {message}\nDiffs will be shown for all changed files after BUILD succeeds."
                
                print(f"\n*** Wrote: {path}")
                return result
            else:
                return f"WRITE_FILE_ERROR: {message}"
        
        elif action_type == 'BUILD':
            if not self.session.pending_changes:
                print("\n*** WARNING: No pending changes to build")
            
            current_dir = self.session.current_directory or "(no scope set)"
            print(f"\n*** Building with changes in: {current_dir}")
            
            # Get full diff before build
            full_diff = self.git.diff_all()
            changed_files = self.git.changed_files_list()
            
            # Run build with live output
            result = self._run_build_with_live_output()

            # Record build result in metrics
            self.metrics.record_build(success=result.success)
            self.metrics.total_iterations = self.session.action_history.__len__() if hasattr(self.session, 'action_history') else 0

            if result.success:
                # Build succeeded!
                self.session.files_fixed += len(changed_files)

                # Log build success
                self.ops.build_success(result.duration_seconds, result.warning_count)
                
                final_diffs = self._render_final_diffs(changed_files)

                # Generate commit message using AI (include directory context)
                commit_msg = self._generate_commit_message(
                    full_diff, changed_files, self.session.current_directory
                )
                
                # Update REVIEW-SUMMARY.md
                self._update_review_summary(
                    changed_files, commit_msg, self.session.current_directory
                )
                
                # Mark directory as done in the persistent index BEFORE commit
                # so updated REVIEW-INDEX.md is included in the commit
                if self.session.current_directory:
                    self.index.mark_done(self.session.current_directory, 
                                       f"Fixed by session {self.session.session_id}")
                    self.index.save()
                
                # Commit and push (includes all .ai-code-reviewer/ metadata)
                success, output = self._commit_and_push(commit_msg)
                if success:
                    # Get commit hash for logging
                    _, commit_hash = self.git._run(['rev-parse', 'HEAD'])
                    commit_hash = commit_hash.strip()[:12]
                    
                    # Log commit success
                    self.ops.commit_success(commit_hash, changed_files)
                    if self.session.current_directory:
                        self._beads_mark_completed(self.session.current_directory, commit_hash)
                    
                    # Mark directory as completed in session
                    if self.session.current_directory:
                        if self.session.current_directory not in self.session.completed_directories:
                            self.session.completed_directories.append(self.session.current_directory)
                        self.session.directories_completed += 1
                        self._clear_directory_attempt(self.session.current_directory)
                        
                        # Log directory complete
                        self.ops.directory_complete(
                            self.session.current_directory,
                            files_changed=changed_files,
                            commit_hash=commit_hash,
                        )
                    
                    self.session.pending_changes = False
                    self.session.changed_files = []
                    
                    # Get next suggested directory from index
                    next_dir = self.index.get_next_pending()
                    next_msg = f"\nNEXT: Use SET_SCOPE {next_dir}" if next_dir else "\nNo more directories pending."
                    
                    return f"BUILD_SUCCESS: Build completed successfully.\n" \
                           f"Directory {current_dir} is now complete.\n" \
                           f"Changes committed and pushed.\n" \
                           f"Metadata files in .ai-code-reviewer/ updated.\n\n" \
                           f"Completed directories so far: {self.session.directories_completed}" \
                           f"{next_msg}\n\nFINAL DIFFS:\n{final_diffs}"
                else:
                    # Log commit failure
                    self.ops.commit_failure(output)
                    return f"BUILD_SUCCESS but commit/push failed: {output}\n" \
                           "Please commit manually."
            else:
                # Build failed - use SELECTIVE REVERT to keep good changes
                self.session.build_failures += 1
                
                # Log build failure
                self.ops.build_failure(
                    result.duration_seconds,
                    error_count=result.error_count,
                    warning_count=result.warning_count,
                    error_summary=result.errors[0].message if result.errors else None,
                )
                
                print("\n*** BUILD FAILED - Analyzing which files caused errors...")
                
                # Use selective revert - only revert failing files, commit successful ones
                reverted_files, committed_files, commit_msg = self._selective_revert_and_commit(result)
                
                # Get error report for AI context
                error_report = result.get_error_report()
                
                # Mark directories with reverted files as needing retry
                reverted_dirs = set(str(Path(f).parent) for f in reverted_files)
                for dir_path in reverted_dirs:
                    self._beads_mark_open(dir_path)
                
                # Commit and push LESSONS.md so the AI has it in context
                if reverted_files:
                    self.git.add(str(self.lessons_file))
                    success, output = self.git.commit(f"LESSON: Build failure - reverted {len(reverted_files)} file(s)")
                    if success:
                        self.git.push()
                        print("*** LESSONS.md committed and pushed")
                
                # Build response for AI
                error_response = f"BUILD_FAILED: Build errors detected\n\n"
                
                if committed_files:
                    error_response += f"*** PARTIAL SUCCESS ***\n"
                    error_response += f"COMMITTED SUCCESSFULLY ({len(committed_files)} files):\n"
                    for f in committed_files:
                        error_response += f"  ✓ {f}\n"
                    error_response += f"\n"
                
                if reverted_files:
                    error_response += f"REVERTED DUE TO ERRORS ({len(reverted_files)} files):\n"
                    for f in reverted_files:
                        error_response += f"  ✗ {f}\n"
                    error_response += f"\n"
                
                error_response += f"BUILD ERROR REPORT:\n{error_report}\n\n"
                error_response += f"LESSON RECORDED: The failed approach has been documented in LESSONS.md.\n\n"
                error_response += f"NEXT STEPS:\n"
                if committed_files:
                    error_response += f"1. Good news: {len(committed_files)} file(s) were committed successfully!\n"
                    error_response += f"2. Only {len(reverted_files)} file(s) need to be re-done\n"
                else:
                    error_response += f"1. All {len(reverted_files)} files were reverted\n"
                error_response += f"3. Re-read the failing file(s) and try a DIFFERENT approach\n"
                error_response += f"4. Check LESSONS.md to avoid repeating the same mistake\n"
                error_response += f"5. BUILD again when ready\n\n"
                
                # Suggest next action
                next_dir = self.index.get_next_pending()
                if reverted_files:
                    error_response += f"REVERTED DIRECTORIES WILL BE RETRIED:\n"
                    for d in reverted_dirs:
                        error_response += f"  - {d}\n"
                if next_dir and not reverted_files:
                    error_response += f"\nNEXT: Use SET_SCOPE {next_dir}\n"
                
                return error_response
        
        elif action_type == 'HALT':
            # Check for incomplete work before allowing HALT

            # 0. In forever mode, reject HALT if open beads remain
            if self.forever_mode and self.beads and self.beads.has_open_work():
                open_dirs = self.beads.get_open_directories()
                open_count = len(open_dirs)
                suggestion_lines = "\n".join(f"  - {d}" for d in open_dirs[:5])
                if open_count > 5:
                    suggestion_lines += f"\n  ... and {open_count - 5} more"
                next_dir = self.index.get_next_pending() or open_dirs[0]
                return (
                    f"HALT_REJECTED: Forever mode is active with {open_count} directories still requiring review.\n\n"
                    f"Open directories:\n{suggestion_lines}\n\n"
                    f"Use ACTION: SET_SCOPE {next_dir} to continue reviewing."
                )

            # 1. Check for uncommitted changes
            if self.session.pending_changes:
                return f"HALT_REJECTED: You have uncommitted changes in {self.session.current_directory}.\n" \
                       f"Changed files: {', '.join(self.session.changed_files)}\n" \
                       f"Run BUILD to validate and commit these changes first."
            
            # 2. Check if no directories have been completed
            if self.session.directories_completed == 0:
                pending_dirs = [path for path, entry in self.index.entries.items()
                                if entry.status == 'pending']
                if pending_dirs:
                    suggestion_lines = "\n".join(f"  - {d}" for d in pending_dirs[:5])
                    return (
                        "HALT_REJECTED: No directories have been completed yet.\n"
                        "You must successfully review at least one directory before halting.\n\n"
                        "Suggested directories to review next:\n"
                        f"{suggestion_lines}\n\n"
                        "Use ACTION: SET_SCOPE <dir> to continue."
                    )
                else:
                    return "HALT_ACKNOWLEDGED (no reviewable directories found)"
            
            # 3. Check if there are more directories that should be reviewed (using the index)
            next_pending = self.index.get_next_pending()
            pending_count = sum(1 for e in self.index.entries.values() if e.status == 'pending')
            
            if next_pending and self.session.directories_completed < 3:
                # Encourage more work if less than 3 directories done this session
                return f"HALT_REJECTED: Only {self.session.directories_completed} directory(ies) completed this session.\n" \
                       f"There are {pending_count} more directories pending review.\n" \
                       f"Next directory: {next_pending}\n\n" \
                       f"Continue reviewing or provide a reason why you cannot proceed."
            
            # Allow HALT - save final state to index
            self.index.save()
            print(f"\n*** Session ending. Completed {self.session.directories_completed} directories this session.")
            return "HALT_ACKNOWLEDGED"
        
        elif action_type == 'NEXT_CHUNK':
            if not self.current_chunks or self.chunked_file_path is None:
                return "NEXT_CHUNK_ERROR: No chunked file in progress. Use READ_FILE first."
            
            self.current_chunk_index += 1
            
            if self.current_chunk_index >= len(self.current_chunks):
                # File complete
                file_path = self.chunked_file_path
                self.current_chunks = []
                self.current_chunk_index = 0
                self.chunked_file_path = None
                
                # Update session: file complete
                self.session.files_reviewed_in_directory += 1
                self.session.current_file = None
                self.session.current_file_chunks_total = 0
                self.session.current_file_chunks_reviewed = 0
                rel_path = str(file_path.relative_to(self.source_root))
                self.session.visited_files_in_directory.add(rel_path)
                
                progress = self.session.get_progress_summary()
                return f"NEXT_CHUNK_COMPLETE: All chunks of {file_path} reviewed.\n\nPROGRESS:\n{progress}\n\n" \
                       f"ACTION OPTIONS:\n" \
                       f"- Review another file in {self.session.current_directory}\n" \
                       f"- Run ACTION: BUILD to test all changes in directory\n"
            
            chunk = self.current_chunks[self.current_chunk_index]
            total_chunks = len(self.current_chunks)
            chunk_num = self.current_chunk_index + 1
            
            # Update session: chunk progress
            self.session.current_file_chunks_reviewed = chunk_num
            
            progress = self.session.get_progress_summary()
            
            chunk_content = format_chunk_for_review(chunk, total_chunks, chunk_num)
            return f"NEXT_CHUNK_RESULT:\n\nPROGRESS:\n{progress}\n\n```\n{chunk_content}\n```"
        
        elif action_type == 'SKIP_FILE':
            if self.chunked_file_path:
                file_path = self.chunked_file_path
                rel_path = str(file_path.relative_to(self.source_root))
                self.session.visited_files_in_directory.add(rel_path)
                self.current_chunks = []
                self.current_chunk_index = 0
                self.chunked_file_path = None
                self.session.current_file = None
                next_hint = self._remaining_files_summary()
                return (
                    f"SKIP_FILE_OK: Skipped remaining chunks of {file_path}.\n"
                    f"Remaining files in {self.session.current_directory}:\n{next_hint}\n"
                    "Pick another file with ACTION: READ_FILE <path> or move to a different directory with ACTION: SET_SCOPE <dir>."
                )
            else:
                skipped = self.session.current_file
                self.session.current_file = None
                if skipped:
                    self.session.visited_files_in_directory.add(skipped)
                next_hint = self._remaining_files_summary()
                return (
                    "SKIP_FILE_OK: Marked current file as skipped.\n"
                    f"Remaining files in {self.session.current_directory}:\n{next_hint}\n"
                    "Please READ_FILE another source file or SET_SCOPE to continue progress."
                )
        
        else:
            return f"UNKNOWN_ACTION: {action_type}"
    
    def _find_reviewable_directories(self) -> List[str]:
        """Return a sample of pending directories from the review index."""
        pending = [path for path, entry in self.index.entries.items()
                   if entry.status == 'pending']
        return pending[:20]

    def _remaining_files_summary(self, limit: int = 5) -> str:
        if not self.session.files_in_current_directory:
            return "  (No files recorded for current directory)"
        remaining = [f for f in self.session.files_in_current_directory
                     if f not in self.session.visited_files_in_directory]
        if not remaining:
            return "  (All tracked files reviewed or skipped in this directory)"
        lines = []
        for path in remaining[:limit]:
            lines.append(f"  - {path}")
        if len(remaining) > limit:
            lines.append(f"  ... plus {len(remaining) - limit} more")
        return '\n'.join(lines)

    def _render_final_diffs(self, files: List[str]) -> str:
        if not files:
            return "No files were modified in this directory."
        sections = []
        for rel_path in files:
            diff = self.git.diff(rel_path)
            header = f"--- {rel_path} ---"
            body = diff if diff.strip() else "(no changes)"
            sections.append(f"{header}\n```diff\n{body}\n```")
        return "\n\n".join(sections)
    
    # Error classification for forever mode
    # Recoverable errors can be retried; unrecoverable trigger emergency stop
    RECOVERABLE_ERRORS = {
        'timeout': 'LLM request timed out',
        'connection': 'Connection to LLM server failed',
        'rate_limit': 'Rate limited by LLM server',
        'temporary': 'Temporary server error',
    }
    UNRECOVERABLE_ERRORS = {
        'model_not_found': 'Model does not exist on server',
        'auth_failed': 'Authentication failed',
        'config_error': 'Configuration is invalid',
        'disk_full': 'Disk full - cannot write files',
        'git_corrupt': 'Git repository is corrupted',
    }
    
    def _classify_llm_error(self, error_msg: str) -> tuple:
        """
        Classify an LLM error as recoverable or unrecoverable.
        
        Returns:
            (is_recoverable, error_type, description)
        """
        error_lower = error_msg.lower()
        
        # Check for recoverable errors first
        if 'timed out' in error_lower or 'timeout' in error_lower:
            return (True, 'timeout', self.RECOVERABLE_ERRORS['timeout'])
        if 'connection' in error_lower or 'connect' in error_lower:
            return (True, 'connection', self.RECOVERABLE_ERRORS['connection'])
        if 'rate' in error_lower and 'limit' in error_lower:
            return (True, 'rate_limit', self.RECOVERABLE_ERRORS['rate_limit'])
        if '503' in error_lower or '502' in error_lower or 'unavailable' in error_lower:
            return (True, 'temporary', self.RECOVERABLE_ERRORS['temporary'])
        if 'temporarily' in error_lower or 'try again' in error_lower:
            return (True, 'temporary', self.RECOVERABLE_ERRORS['temporary'])
        
        # Check for unrecoverable errors
        if 'does not exist' in error_lower or 'not found' in error_lower:
            if 'model' in error_lower:
                return (False, 'model_not_found', self.UNRECOVERABLE_ERRORS['model_not_found'])
        if 'auth' in error_lower or 'unauthorized' in error_lower or '401' in error_lower:
            return (False, 'auth_failed', self.UNRECOVERABLE_ERRORS['auth_failed'])
        if 'disk' in error_lower and ('full' in error_lower or 'space' in error_lower):
            return (False, 'disk_full', self.UNRECOVERABLE_ERRORS['disk_full'])
        
        # Default: treat unknown errors as recoverable (try before giving up)
        return (True, 'unknown', f'Unknown error: {error_msg[:100]}')
    
    def _commit_lessons_and_continue(self, reason: str) -> bool:
        """
        Commit any pending lessons to the source tree repo before continuing.
        
        This ensures lessons are not lost if we need to recover from an error.
        In forever mode, we commit lessons and keep going rather than stopping.
        
        Returns:
            True if lessons were committed, False otherwise
        """
        # Check if there are any uncommitted changes to lessons file
        if not self.lessons_file.exists():
            return False
        
        try:
            # Check if lessons file has changes
            diff = self.git.diff(str(self.lessons_file))
            if not diff:
                # Check untracked
                status = self.git.show_status()
                if str(self.lessons_file.relative_to(self.source_root)) not in status:
                    return False
            
            # Stage and commit the lessons
            self.git.add(str(self.lessons_file))
            commit_msg = f"LESSONS: {reason}\n\nAuto-committed lessons learned before recovery."
            success, output = self.git.commit(commit_msg)
            
            if success:
                self.git.push()
                print(f"*** Lessons committed and pushed: {reason}")
                logger.info(f"Auto-committed lessons: {reason}")
                return True
            else:
                logger.warning(f"Failed to commit lessons: {output}")
                return False
                
        except Exception as e:
            logger.warning(f"Error committing lessons: {e}")
            return False
    
    def _emergency_stop(self, error_type: str, error_msg: str, context: str = "") -> None:
        """
        Perform an emergency stop with clear user instructions.
        
        This should ONLY be called for truly unrecoverable errors.
        """
        print("\n" + "="*70)
        print("🛑 EMERGENCY STOP - UNRECOVERABLE ERROR")
        print("="*70)
        print(f"\nError Type: {error_type}")
        print(f"Error: {error_msg}")
        if context:
            print(f"Context: {context}")
        
        print("\n" + "-"*70)
        print("WHY THIS CANNOT BE RECOVERED:")
        print("-"*70)
        
        if error_type == 'model_not_found':
            print("""
The specified AI model does not exist on the LLM server.
This is a configuration issue that requires manual intervention.

TO FIX:
1. Check your LLM server for available models:
   - For Ollama: ollama list
   - For vLLM: check your server's model directory
2. Update config.yaml with a valid model name:
   llm:
     models:
       - "your-model-name-here"
3. Restart the review: make run-forever
""")
        elif error_type == 'auth_failed':
            print("""
Authentication to the LLM server failed.
This requires updating credentials.

TO FIX:
1. Check your API key or credentials in config.yaml
2. Verify the LLM server is accessible
3. Restart the review: make run-forever
""")
        elif error_type == 'disk_full':
            print("""
The disk is full. Cannot write files or git commits.

TO FIX:
1. Free up disk space on this system
2. Consider cleaning git history or old logs
3. Restart the review: make run-forever
""")
        elif error_type == 'git_corrupt':
            print("""
The git repository appears to be corrupted.

TO FIX:
1. Try: git fsck --full
2. If needed: git reflog expire --expire=now --all && git gc --prune=now
3. As last resort: re-clone the repository
4. Restart the review: make run-forever
""")
        else:
            print(f"""
An unrecoverable error occurred that requires manual intervention.

TO FIX:
1. Review the error message above
2. Check config.yaml settings
3. Verify LLM server status
4. Restart the review: make run-forever
""")
        
        print("-"*70)
        print("SESSION STATE:")
        print("-"*70)
        print(f"Directories completed: {self.session.directories_completed}")
        print(f"Current directory: {self.session.current_directory or 'None'}")
        print(f"Session ID: {self.session.session_id}")
        
        # Try to commit any pending lessons before stopping
        self._commit_lessons_and_continue("Emergency stop - preserving lessons")
        
        # Clean up any dirty state
        self._cleanup_dirty_state()
        
        print("\n" + "="*70)
        print("The review has stopped. Follow the instructions above to resolve.")
        print("="*70 + "\n")
        
        # Log to ops
        if self.ops:
            self.ops.ai_error(f"EMERGENCY_STOP: {error_type} - {error_msg}")
    
    def _recover_from_loop(self, action: Dict[str, Any]) -> str:
        """
        Automatically recover when stuck in an infinite loop.
        
        Actions taken:
        1. Revert any uncommitted changes
        2. Skip the current file if one is active
        3. Force the AI to move on
        
        Returns:
            Recovery message for the AI
        """
        logger.error("Infinite loop detected - initiating automatic recovery")
        print("\n" + "="*70)
        print("⚠️  AUTOMATIC LOOP RECOVERY INITIATED")
        print("="*70)
        
        action_type = action.get('action', '')
        recovery_actions = []
        
        # 1. Revert uncommitted changes if any
        if self.session.pending_changes or self.git.has_changes():
            print("Step 1: Reverting uncommitted changes...")
            self._cleanup_dirty_state()
            recovery_actions.append("Reverted all uncommitted changes")
        
        # 2. Clear chunked file state if stuck on chunks
        if self.chunked_file_path:
            stuck_file = str(self.chunked_file_path)
            print(f"Step 2: Abandoning chunked file: {stuck_file}")
            self.current_chunks = []
            self.current_chunk_index = 0
            self.chunked_file_path = None
            recovery_actions.append(f"Abandoned chunked file: {stuck_file}")
        
        # 3. Force skip current file if stuck on READ_FILE
        if action_type == 'READ_FILE' and self.session.current_file:
            stuck_file = self.session.current_file
            print(f"Step 3: Force-skipping stuck file: {stuck_file}")
            self.session.current_file = None
            recovery_actions.append(f"Force-skipped file: {stuck_file}")
        
        # 4. Reset loop detection
        print("Step 4: Resetting loop detection counters")
        self.session.consecutive_identical_actions = 0
        self.session.last_action_hash = None
        
        # 5. File a beads issue for this systemic problem
        if self.beads:
            action_hash = self._get_action_hash(action)
            issue_desc = (
                f"AI got stuck in an infinite loop during code review.\n\n"
                f"Action repeated: {action_type} - {action_hash}\n"
                f"Repetitions: {len([a for a in self.session.action_history if a[1] == action_hash])}\n"
                f"Directory: {self.session.current_directory or 'unknown'}\n"
                f"Session: {self.session.session_id}\n\n"
                f"Recovery actions taken:\n" + "\n".join(f"- {a}" for a in recovery_actions) +
                f"\n\nThis indicates a problem with:\n"
                f"- AI instruction clarity\n"
                f"- File/directory complexity\n"
                f"- Loop detection thresholds\n"
                f"- Error handling logic"
            )
            self.beads.create_systemic_issue(
                title=f"Loop detection triggered: {action_type} in {self.session.current_directory or 'unknown'}",
                description=issue_desc,
                issue_type='bug',
                priority=1,
                labels=['ai-behavior', 'loop-detection', 'automatic-recovery']
            )
        
        print("="*70)
        print("✓ RECOVERY COMPLETE - You must now take a different approach")
        print("="*70 + "\n")
        
        # Build recovery message for AI
        recovery_msg = (
            f"\n{'='*70}\n"
            f"🔄 AUTOMATIC RECOVERY COMPLETED\n"
            f"{'='*70}\n\n"
            f"You were stuck in an infinite loop. The system has automatically:\n"
        )
        for action in recovery_actions:
            recovery_msg += f"  - {action}\n"
        
        recovery_msg += (
            f"\n{'='*70}\n"
            f"MANDATORY NEXT STEPS:\n"
            f"{'='*70}\n\n"
            f"You MUST choose ONE of these actions (no other action will be accepted):\n\n"
            f"1. Move to a DIFFERENT file in the same directory:\n"
            f"   ACTION: READ_FILE <different-file-path>\n\n"
            f"2. Skip to the next directory:\n"
            f"   ACTION: SET_SCOPE <different-directory>\n\n"
            f"3. If you have files to review in current directory, list them:\n"
            f"   ACTION: LIST_DIR {self.session.current_directory or '.'}\n\n"
            f"4. If completely stuck, halt:\n"
            f"   ACTION: HALT\n\n"
            f"DO NOT attempt to read the same file again.\n"
            f"DO NOT repeat the action that caused the loop.\n"
            f"{'='*70}\n"
        )
        
        return recovery_msg
    
    def _validate_response(self, response: str) -> Optional[str]:
        """
        Validate that AI response is complete and not truncated.
        
        Returns:
            None if response is OK, warning message if problematic
        """
        # Check for signs of truncation
        truncation_indicators = [
            (lambda r: len(r) < 50, "Response is suspiciously short (< 50 chars)"),
            (lambda r: r.endswith("Here'") or r.endswith("Let'"), "Response ends mid-word (truncated)"),
            (lambda r: r.count("<<<") != r.count(">>>") and ("<<<" in r or ">>>" in r), 
             "Mismatched <<< >>> delimiters (incomplete EDIT_FILE)"),
            (lambda r: "ACTION: EDIT_FILE" in r and "OLD:" not in r, 
             "EDIT_FILE action without OLD block"),
            (lambda r: "OLD:" in r and "NEW:" not in r and "<<<" in r, 
             "OLD block started but no NEW block"),
        ]
        
        for check_fn, message in truncation_indicators:
            if check_fn(response):
                return (
                    f"\n⚠️  INCOMPLETE RESPONSE DETECTED\n\n"
                    f"Problem: {message}\n"
                    f"Response length: {len(response)} chars\n"
                    f"Last 100 chars: ...{response[-100:]}\n\n"
                    f"Your response appears to have been cut off before completion.\n"
                    f"Please provide a COMPLETE response including:\n"
                    f"1. Your analysis\n"
                    f"2. A SINGLE, COMPLETE ACTION directive\n"
                    f"3. For EDIT_FILE: both complete OLD and NEW blocks\n\n"
                    f"If you're working with a large file causing timeouts:\n"
                    f"- Focus on ONE specific issue at a time\n"
                    f"- Use smaller OLD/NEW blocks\n"
                    f"- Consider SKIP_FILE if the file is too complex\n"
                )
        
        return None

    def _rescan_for_new_directories(self) -> int:
        """Re-scan the source tree for new directories not yet tracked.

        Returns the number of new directories discovered and filed as beads.
        """
        print("\n*** Forever mode: Re-scanning source tree for new directories...")
        new_index = generate_index(self.source_root, force_rebuild=True)
        existing_dirs = set(self.index.entries.keys())
        new_dirs = [
            path for path in new_index.entries
            if path not in existing_dirs
        ]
        if not new_dirs:
            print("    No new directories found.")
            return 0

        # Merge new entries into the live index
        for path in new_dirs:
            self.index.entries[path] = new_index.entries[path]
        self.index.save()
        print(f"    Found {len(new_dirs)} new directories in source tree")

        # File beads for the new directories
        if self.beads:
            created = self.beads.ensure_directories(new_dirs)
            print(f"    Created {created} new beads for review")
            return created

        return len(new_dirs)

    def _cleanup_dirty_state(self) -> None:
        """Clean up any uncommitted changes before ending session."""
        if self.session.pending_changes or self.git.has_changes():
            print("\n*** Cleaning up uncommitted changes...")
            # Revert any modified source files
            for file_path in self.session.changed_files:
                full_path = self.source_root / file_path
                if full_path.exists():
                    code, output = self.git._run(['checkout', str(full_path)])
                    if code == 0:
                        print(f"    Reverted: {file_path}")
            
            # Also revert REVIEW-INDEX.md if modified (check both locations)
            for index_path in ['REVIEW-INDEX.md', '.ai-code-reviewer/REVIEW-INDEX.md']:
                code, output = self.git._run(['checkout', index_path])
                if code == 0:
                    print(f"    Reverted: {index_path}")
            
            self.session.pending_changes = False
            self.session.changed_files = []
            print("*** Working tree cleaned")
    
    def run(self) -> None:
        """Run the main review loop until target directories completed."""
        logger.info("Starting review loop...")
        
        # Log session start
        self.ops.session_start({
            "source_root": str(self.source_root),
            "persona": str(self.persona_dir.name),
            "target_directories": self.target_directories,
            "max_iterations_per_directory": self.max_iterations_per_directory,
        })
        
        # Show initial git status
        try:
            status = self.git.show_status()
        except GitCommandError as exc:
            status = ""
            print("\n*** WARNING: Unable to read git status:")
            print(f"    {exc}")
            print("    (repository may be in the middle of a rebase or have a corrupt index)")
            if self.git.recover_repository():
                try:
                    status = self.git.show_status()
                except GitCommandError as exc2:
                    print("    Recovery attempt failed to repair git status:"
                          f" {exc2}")
        if status:
            print("\n*** Git status at start:")
            print(status)
        
        step = 0
        directory_iterations = 0  # Iterations spent on current directory
        last_directory = None

        # Continue until we've completed target directories (or 0 = unlimited, or forever mode)
        while (self.forever_mode or
               self.target_directories == 0 or
               self.session.directories_completed < self.target_directories):
            step += 1
            
            # Track iterations per directory
            if self.session.current_directory != last_directory:
                directory_iterations = 0
                last_directory = self.session.current_directory
            directory_iterations += 1
            
            # Check if stuck on current directory too long
            if directory_iterations > self.max_iterations_per_directory:
                print(f"\n*** WARNING: Exceeded {self.max_iterations_per_directory} iterations on {self.session.current_directory}")
                print("*** Cleaning up and moving to next directory...")
                self._cleanup_dirty_state()
                # Force AI to move on
                self.history.append({
                    "role": "user", 
                    "content": f"TIMEOUT: You have spent too many iterations on {self.session.current_directory}. "
                              f"This directory is being skipped. Use SET_SCOPE to move to the next directory."
                })
                directory_iterations = 0
                continue
            
            # Show hierarchical progress
            progress_summary = self.session.get_progress_summary()
            if self.forever_mode:
                dir_progress = f"{self.session.directories_completed} (forever mode)"
            else:
                dir_progress = f"{self.session.directories_completed}/{self.target_directories}" if self.target_directories > 0 else f"{self.session.directories_completed}"
            logger.info(f"Step {step} | Dir {dir_progress} | {self.session.current_directory or 'No scope'} ({directory_iterations}/{self.max_iterations_per_directory})")
            print(f"\n{'='*70}")
            print(f"STEP {step} | Directories: {dir_progress} | Current: {self.session.current_directory or 'None'} ({directory_iterations}/{self.max_iterations_per_directory})")
            if progress_summary:
                print(f"\n{progress_summary}")
            print('='*70)
            
            # Track retry state for recoverable errors
            if not hasattr(self.session, 'llm_retry_count'):
                self.session.llm_retry_count = 0
                self.session.llm_retry_backoff = 5  # Start with 5 second backoff
            
            try:
                response = self.ollama.chat(self.history)
                # Reset retry counters on success
                self.session.llm_retry_count = 0
                self.session.llm_retry_backoff = 5
            except Exception as e:
                error_msg = str(e)
                is_recoverable, error_type, description = self._classify_llm_error(error_msg)
                
                logger.error(f"LLM error ({error_type}): {error_msg}")
                self.ops.ai_error(error_msg)
                
                if is_recoverable and self.forever_mode:
                    # In forever mode, recoverable errors should retry, not stop
                    self.session.llm_retry_count += 1
                    max_retries = 10  # Give up after 10 consecutive failures
                    
                    if self.session.llm_retry_count > max_retries:
                        # Too many retries - commit lessons and emergency stop
                        self._commit_lessons_and_continue(f"LLM error after {max_retries} retries")
                        self._emergency_stop(
                            error_type, 
                            error_msg,
                            f"Failed {max_retries} consecutive times. Error type: {description}"
                        )
                        break
                    
                    # Calculate backoff with exponential increase, capped at 5 minutes
                    backoff = min(self.session.llm_retry_backoff, 300)
                    self.session.llm_retry_backoff = min(self.session.llm_retry_backoff * 2, 300)
                    
                    print(f"\n{'='*60}")
                    print(f"⚠️  RECOVERABLE ERROR (attempt {self.session.llm_retry_count}/{max_retries})")
                    print('='*60)
                    print(f"Error type: {error_type}")
                    print(f"Description: {description}")
                    print(f"\nThis error is recoverable. Retrying in {backoff} seconds...")
                    print(f"Forever mode continues automatically.")
                    print('='*60)
                    
                    # Commit any pending lessons before we wait
                    if self.session.llm_retry_count == 1:
                        self._commit_lessons_and_continue(f"Preserving lessons before retry ({error_type})")
                    
                    # Wait with backoff
                    time.sleep(backoff)
                    continue  # Retry the loop iteration
                    
                elif not is_recoverable:
                    # Unrecoverable error - emergency stop with instructions
                    self._commit_lessons_and_continue(f"Unrecoverable error: {error_type}")
                    self._emergency_stop(error_type, error_msg)
                    break
                    
                else:
                    # Not in forever mode - show error and stop (legacy behavior)
                    print(f"\n{'='*60}")
                    print(f"ERROR: {description}")
                    print('='*60)
                    print(f"{error_msg}")
                    if error_type == 'timeout':
                        print(f"\nThe model took longer than {self.ollama.config.timeout}s to respond.")
                        print("Solutions:")
                        print(f"1. Increase timeout in config.yaml: llm.timeout")
                        print("2. Review smaller files first")
                        print("3. Use --forever mode for automatic retry")
                    elif error_type == 'model_not_found':
                        print("\nTo see available models, check your LLM server.")
                        print("Then update config.yaml llm.models with valid model names.")
                    print('='*60)
                    break
            
            last_user_msg = self.history[-1]['content'] if self.history else ""
            self._log_exchange(step, last_user_msg, response)
            
            print(f"\n{'='*60}")
            print("AI RESPONSE:")
            print('='*60)
            print(self._format_response_for_console(response))
            print('='*60)
            
            self.history.append({"role": "assistant", "content": response})
            
            # Validate response completeness
            response_warning = self._validate_response(response)
            if response_warning:
                logger.warning("Response appears truncated or incomplete")
                self.history.append({
                    "role": "user",
                    "content": response_warning
                })
                continue
            
            action = self.parser.parse(response)
            
            # CRITICAL: Check for loops even if action parsing failed
            # This catches the case where AI keeps saying the same thing but parser can't extract it
            if not action:
                # Track failed parse attempts
                if not hasattr(self.session, 'consecutive_parse_failures'):
                    self.session.consecutive_parse_failures = 0
                    self.session.last_failed_response = ""
                
                # Check if we're getting the same unparseable response repeatedly
                if response.strip() == self.session.last_failed_response.strip():
                    self.session.consecutive_parse_failures += 1
                else:
                    self.session.consecutive_parse_failures = 1
                    self.session.last_failed_response = response.strip()
                
                # If same unparseable response repeated too many times, force recovery
                if self.session.consecutive_parse_failures >= 5:
                    logger.error(f"Same unparseable response repeated {self.session.consecutive_parse_failures} times")
                    
                    # File a beads issue for this parsing problem
                    if self.beads:
                        issue_desc = (
                            f"AI repeatedly provided unparseable responses.\n\n"
                            f"Failed response (truncated):\n{response[:500]}...\n\n"
                            f"Repetitions: {self.session.consecutive_parse_failures}\n"
                            f"Directory: {self.session.current_directory or 'unknown'}\n"
                            f"Session: {self.session.session_id}\n\n"
                            f"This suggests:\n"
                            f"- Wrong format being used (e.g., '### Action:' instead of 'ACTION:')\n"
                            f"- Response truncation issues\n"
                            f"- LLM not following instructions\n"
                            f"- Parser regex needs improvement"
                        )
                        self.beads.create_systemic_issue(
                            title=f"Unparseable response loop in {self.session.current_directory or 'unknown'}",
                            description=issue_desc,
                            issue_type='bug',
                            priority=1,
                            labels=['ai-behavior', 'parsing-failure', 'format-error']
                        )
                    
                    recovery_msg = (
                        f"\n{'='*70}\n"
                        f"⚠️  CRITICAL: UNPARSEABLE LOOP DETECTED ⚠️\n"
                        f"{'='*70}\n\n"
                        f"You have provided the same response {self.session.consecutive_parse_failures} times,\n"
                        f"but the system cannot parse any valid ACTION from it.\n\n"
                        f"Your response: \"{response[:100]}...\"\n\n"
                        f"The problem is likely:\n"
                        f"1. Using wrong format like '### Action:' instead of 'ACTION:'\n"
                        f"2. Response is truncated mid-action\n"
                        f"3. Action keyword is misspelled\n\n"
                        f"CORRECT FORMAT:\n"
                        f"  ACTION: READ_FILE path/to/file\n"
                        f"  ACTION: EDIT_FILE path/to/file\n"
                        f"  ACTION: LIST_DIR path/to/dir\n"
                        f"  ACTION: SET_SCOPE directory\n"
                        f"  ACTION: BUILD\n"
                        f"  ACTION: HALT\n\n"
                        f"Provide ONE valid action now using the correct format.\n"
                        f"{'='*70}\n"
                    )
                    self.history.append({"role": "user", "content": recovery_msg})
                    continue
            
            if not action:
                # Check for common mistakes
                feedback = "No valid ACTION found in your response.\n\n"
                
                if "EDIT " in response and "ACTION: EDIT_FILE" not in response:
                    feedback += "ERROR: Use 'ACTION: EDIT_FILE path/to/file' not 'EDIT filename'\n"
                if "OLD:" in response and "<<<" not in response:
                    feedback += "ERROR: OLD and NEW blocks must use <<< and >>> delimiters\n"
                
                feedback += "\nCorrect format:\nACTION: EDIT_FILE bin/cpuset/cpuset.c\nOLD:\n<<<\nexact text\n>>>\nNEW:\n<<<\nnew text\n>>>"
                
                self.history.append({
                    "role": "user",
                    "content": feedback
                })
                continue
            
            if action['action'] == 'HALT':
                # In forever mode, reject HALT if open beads remain
                if self.forever_mode and self.beads and self.beads.has_open_work():
                    open_dirs = self.beads.get_open_directories()
                    next_dir = self.index.get_next_pending() or open_dirs[0]
                    msg = (
                        f"HALT_REJECTED: Forever mode active with {len(open_dirs)} "
                        f"directories still open.\n"
                        f"Next: ACTION: SET_SCOPE {next_dir}\n"
                    )
                    self.history.append({"role": "user", "content": msg})
                    continue
                logger.info("Received HALT. Stopping.")
                break
            
            result = self._execute_action(action)
            logger.info(f"Action result: {result[:100]}...")

            self.history.append({"role": "user", "content": result})

            # Check if we're in forever mode and no more work remains
            if self.forever_mode:
                next_pending = self.index.get_next_pending()
                beads_open = self.beads.has_open_work() if self.beads else False
                if next_pending is None and not beads_open and self.session.current_directory is None:
                    # All known work is done - re-scan for new directories
                    new_count = self._rescan_for_new_directories()
                    if new_count > 0:
                        logger.info(f"Forever mode: Re-scan found {new_count} new directories")
                        print(f"\n*** Forever mode: Re-scan discovered {new_count} new directories to review")
                        # Continue the loop to process them
                        continue
                    logger.info("Forever mode: No more pending directories. Stopping.")
                    print("\n" + "="*60)
                    print("FOREVER MODE COMPLETE")
                    print("="*60)
                    print("All directories have been reviewed.")
                    print(f"Total directories completed: {self.session.directories_completed}")
                    print("Re-run the directory scan if new source code has been added.")
                    print("="*60)
                    break
                elif next_pending is None and beads_open and self.session.current_directory is None:
                    # Index shows all done but beads still open - desync;
                    # guide the AI to the next open bead
                    open_dirs = self.beads.get_open_directories()
                    logger.info(f"Forever mode: Index exhausted but {len(open_dirs)} beads still open")
                    self.history.append({
                        "role": "user",
                        "content": (
                            f"There are {len(open_dirs)} directories with open beads remaining.\n"
                            f"Next: ACTION: SET_SCOPE {open_dirs[0]}\n"
                        )
                    })

            # Prune history if getting too long
            if len(self.history) > 42:
                self.history = self.history[:2] + self.history[-40:]
        
        # ALWAYS clean up dirty state before ending
        self._cleanup_dirty_state()
        
        # Update and save persona metrics
        elapsed_seconds = (datetime.datetime.now() - self.session.start_time).total_seconds()
        self.metrics.update_from_session(self.session)
        self.metrics.total_time_seconds = elapsed_seconds
        self.metrics_tracker.save_session()

        # Log session end
        self.ops.session_end(
            directories_completed=self.session.directories_completed,
            files_fixed=self.session.files_fixed,
            build_failures=self.session.build_failures,
        )

        # Final status
        print("\n" + "=" * 60)
        print("REVIEW SESSION COMPLETE")
        print("=" * 60)
        print(f"Session: {self.session.session_id}")
        print(f"Duration: {datetime.datetime.now() - self.session.start_time}")
        print(f"Directories completed: {self.session.directories_completed}")
        if self.session.completed_directories:
            for d in self.session.completed_directories:
                print(f"  ✓ {d}")
        print(f"Files fixed: {self.session.files_fixed}")
        print(f"Build failures: {self.session.build_failures}")
        print("=" * 60)

        # Show persona effectiveness metrics
        print("\nPersona Effectiveness Metrics:")
        print("-" * 60)
        print(self.metrics.get_summary())
        print("=" * 60)


def preflight_sanity_check(
    builder: Any,
    source_root: Path,
    git: GitHelper,
    max_reverts: int = 100,
    ops_logger: Optional[OpsLogger] = None,
) -> bool:
    """
    Pre-flight sanity check: Verify source builds before starting review.
    
    If the build fails, this indicates previous AI review runs may have
    introduced breaking changes. We revert commits one by one until the
    source builds again, then continue from that point.
    
    Args:
        builder: BuildExecutor instance
        source_root: Path to source root
        git: GitHelper instance
        max_reverts: Maximum number of commits to revert before giving up (default: 100)
        ops_logger: Optional OpsLogger for metrics
        
    Returns:
        True if source builds (or was fixed by reverting), False if unfixable
    """
    try:
        tool_root = Path(__file__).resolve().parent
        BeadsManager(source_root, tool_root=tool_root, git_helper=git)
    except BeadsMigrationError as exc:
        print("\nWARNING: Beads migration failed; continuing without beads integration")
        print("-" * 70)
        print(str(exc))
        print("-" * 70)

    print("\n" + "=" * 70)
    print("PRE-FLIGHT SANITY CHECK")
    print("=" * 70)
    print("Testing if source builds with configured build command...")
    print(f"Command: {builder.config.build_command}")
    print("=" * 70 + "\n")
    
    # Check for uncommitted changes (excluding .beads/ which we'll auto-stash)
    try:
        changes = git.show_status()
    except GitCommandError as exc:
        print("ERROR: Unable to read git status for pre-flight:")
        print(f"  {exc}")
        print("\nThe FreeBSD source tree appears to have a corrupt git index or an interrupted rebase.")
        recovered = git.recover_repository()
        if recovered:
            try:
                changes = git.show_status()
            except GitCommandError as exc2:
                print("Recovery attempt failed to restore git status:"
                      f" {exc2}")
                print("Manual intervention required. Re-run make run after fixing the tree.")
                if ops_logger:
                    ops_logger.error(
                        "git status failed during preflight after auto-recover",
                        details={"error": str(exc2)},
                    )
                return False
        else:
            print("Automatic git recovery did not succeed. Manual repair required.")
            print("Suggested commands: 'git clean -fdx', 'git rebase --abort', 'git fetch', 'git reset --hard origin/main'.")
            if ops_logger:
                ops_logger.error(
                    "git status failed during preflight and auto-recover",
                    details={"error": str(exc)},
                )
            return False

    if changes:
        def _is_ignored_change(line: str) -> bool:
            trimmed = line.strip()
            if not trimmed:
                return True
            path = trimmed.split()[-1]
            if path.startswith('.beads/'):
                return True
            if path.startswith('.ai-code-reviewer/'):
                return True
            if path.startswith('.angry-ai/'):  # Legacy location
                return True
            if path == 'REVIEW-INDEX.md':  # Legacy location
                return True
            return False

        non_tool_changes = [line for line in changes.split('\n') 
                            if line.strip() and not _is_ignored_change(line)]
        
        if non_tool_changes:
            print("WARNING: Uncommitted changes detected (excluding tool-managed files):")
            print('\n'.join(non_tool_changes))
            print("\nCannot run pre-flight check with uncommitted changes.")
            print("Please commit or stash changes first.")
            print("Note: .beads/ and .ai-code-reviewer/ changes are auto-managed by the tool.")
            return False
        
        print("Note: Ignoring .beads/ and .ai-code-reviewer/ changes (managed by tool)")
    
    # Get current commit for reference
    code, current_commit = git._run(['rev-parse', 'HEAD'])
    current_commit = current_commit.strip()
    
    # Attempt initial build
    from build_executor import BuildResult
    
    try:
        result = builder.run_build(capture_output=True)
        
        if result.success:
            print("\n" + "=" * 70)
            print("✓ PRE-FLIGHT CHECK PASSED")
            print("=" * 70)
            print(f"Source builds successfully in {result.duration_seconds:.1f}s")
            print(f"Warnings: {result.warning_count}")
            print("Proceeding with review workflow...")
            print("=" * 70 + "\n")
            if ops_logger:
                ops_logger.preflight_pass(result.duration_seconds, result.warning_count)
            return True
        
        # Build failed - attempt recovery
        print("\n" + "=" * 70)
        print("✗ PRE-FLIGHT CHECK FAILED")
        print("=" * 70)
        print(f"Build failed with {result.error_count} errors, {result.warning_count} warnings")
        if ops_logger:
            ops_logger.preflight_fail(result.error_count, result.warning_count)
        print(f"Build return code: {result.return_code}")
        
        if result.error_count == 0:
            print("\nNote: No C/C++ compilation errors detected by parser.")
            print("This could be:")
            print("  - Makefile syntax errors (unclosed conditionals, etc.)")
            print("  - Linker errors")
            print("  - Build system configuration errors")
            print("  - Shell script errors")
        
        print("\nAttempting to recover by reverting recent commits...")
        print(f"(Will revert up to {max_reverts} commits to find a working state)\n")
        
        # Stash any .beads/ and .ai-code-reviewer/ changes before reverting
        tool_files_stashed = False
        if git.has_changes():
            print("Stashing .beads/ and .ai-code-reviewer/ changes before reverting...")
            code, output = git._run(['stash', 'push', '-m', 'preflight-tool-backup', '.beads/', '.ai-code-reviewer/', '.angry-ai/'])
            if code == 0:
                tool_files_stashed = True
                print("✓ Tool-managed files stashed")
            else:
                print(f"WARNING: Could not stash tool-managed files: {output}")
        
        reverted_commits = []
        
        # Use reset strategy instead of revert to actually go back in history
        # We'll test going back N commits and reset to the first working one
        for attempt in range(1, max_reverts + 1):
            print(f"\n--- Recovery Attempt {attempt}/{max_reverts} ---")
            
            # Get commit info at HEAD~(attempt-1)
            code, commit_info = git._run(['log', '-1', '--oneline', f'HEAD~{attempt-1}'])
            if code != 0:
                print(f"ERROR: Cannot access HEAD~{attempt-1}: {commit_info}")
                print("Reached beginning of git history.")
                break
            commit_info = commit_info.strip()
            
            print(f"Testing state at: {commit_info}")
            
            # Reset to this commit (destructive, but we're in recovery mode)
            code, output = git._run(['reset', '--hard', f'HEAD~{attempt-1}'])
            if code != 0:
                print(f"ERROR: Git reset failed: {output}")
                print("Manual intervention required.")
                # Try to restore to original state
                git._run(['reset', '--hard', current_commit])
                return False
            
            reverted_commits.append(commit_info)
            
            # Try building again
            print("Testing build...")
            result = builder.run_build(capture_output=True)
            
            if result.success:
                print("\n" + "=" * 70)
                print("✓ BUILD RECOVERED")
                print("=" * 70)
                print(f"Reset back {attempt} commit(s) to find working state:")
                print(f"  Now at: {commit_info}")
                print(f"\nSource now builds successfully in {result.duration_seconds:.1f}s")
                
                # Show what commits were skipped
                if attempt > 1:
                    print(f"\nSkipped {attempt} broken commit(s) (use 'git log' to see them)")
                    code, skipped = git._run(['log', '--oneline', f'{current_commit}..HEAD'])
                    if code == 0 and skipped.strip():
                        print("Note: These commits still exist but are not on your current branch")
                
                # Restore tool-managed files if we stashed them
                if tool_files_stashed:
                    print("\nRestoring tool-managed files...")
                    code, output = git._run(['stash', 'pop'])
                    if code == 0:
                        print("✓ Tool-managed files restored")
                    else:
                        print(f"WARNING: Could not restore tool-managed files: {output}")
                        print("You may need to manually restore: git stash list")
                
                print("\nProceeding with review workflow from this point...")
                print("=" * 70 + "\n")
                if ops_logger:
                    ops_logger.preflight_recovery(attempt, commit_info.split()[0])
                return True
            else:
                print(f"Build still fails ({result.error_count} errors). Trying another revert...")
        
        # Max reverts reached without success - restore original state
        print("\n" + "=" * 70)
        print("✗ RECOVERY FAILED")
        print("=" * 70)
        print(f"Tested {max_reverts} commits back but source still doesn't build.")
        print("Restoring original state...")
        
        # Reset back to where we started
        code, output = git._run(['reset', '--hard', current_commit])
        if code == 0:
            print(f"✓ Restored to original commit: {current_commit[:12]}")
        else:
            print(f"ERROR: Could not restore original state: {output}")
            print(f"Manually reset with: git reset --hard {current_commit}")
        
        # Restore tool-managed files if we stashed them
        if tool_files_stashed:
            print("\nRestoring tool-managed files...")
            code, output = git._run(['stash', 'pop'])
            if code == 0:
                print("✓ Tool-managed files restored")
            else:
                print(f"WARNING: Could not restore tool-managed files: {output}")
        
        print("\nManual intervention required.")
        print("The build has been broken for more than the last 100 commits.")
        print("=" * 70 + "\n")
        return False
        
    except Exception as e:
        print(f"\n✗ PRE-FLIGHT CHECK ERROR: {e}")
        print("Cannot verify build status. Proceeding with caution...\n")
        return False


def validate_source_tree(source_root: Path) -> Tuple[bool, str]:
    """
    Validate that source_root points to a buildable source tree.
    
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not source_root.exists():
        return False, f"Source root does not exist: {source_root}"
    
    if not source_root.is_dir():
        return False, f"Source root is not a directory: {source_root}"
    
    # Check for common indicators of a source tree
    # FreeBSD: Makefile with buildworld target
    # Linux kernel: Makefile with vmlinux target  
    # Other: any Makefile or CMakeLists.txt
    makefile = source_root / "Makefile"
    cmake = source_root / "CMakeLists.txt"
    
    if not makefile.exists() and not cmake.exists():
        return False, (
            f"Source root does not appear to be a buildable project: {source_root}\n"
            f"Expected to find Makefile or CMakeLists.txt but found neither.\n"
            f"Please set source.root in config.yaml to point to your source tree."
        )
    
    return True, ""


def check_beads_installation() -> Tuple[bool, Optional[str]]:
    """
    Check if beads (bd) CLI is installed.
    
    Returns:
        Tuple of (is_installed, bd_path)
    """
    bd_path = shutil.which(os.environ.get('BD_CMD', 'bd'))
    return (bd_path is not None, bd_path)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Angry AI - FreeBSD Code Reviewer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python reviewer.py                     # Use default config.yaml
    python reviewer.py --config my.yaml    # Use custom config
    python reviewer.py --forever           # Run until all directories reviewed
    python reviewer.py --validate-only     # Just validate LLM connection
    python reviewer.py --skip-preflight    # Skip pre-flight build check
        """
    )
    
    parser.add_argument(
        '--config',
        default='config.yaml',
        help='Path to configuration file (default: config.yaml)'
    )
    
    parser.add_argument(
        '--validate-only',
        action='store_true',
        help='Only validate Ollama connection, then exit'
    )
    
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Enable verbose logging'
    )
    
    parser.add_argument(
        '--skip-preflight',
        action='store_true',
        help='Skip pre-flight build sanity check (use with caution)'
    )

    parser.add_argument(
        '--forever',
        action='store_true',
        help='Run until all directories are reviewed (ignores target_directories setting)'
    )

    args = parser.parse_args()
    
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s %(levelname)s [%(name)s] %(message)s',
        datefmt='%H:%M:%S'
    )
    
    config_path = Path(args.config)
    defaults_path = config_path.parent / "config.yaml.defaults"
    
    created_new_config = False
    if not config_path.exists():
        # Try to copy from defaults
        if defaults_path.exists():
            import shutil
            shutil.copy(defaults_path, config_path)
            logger.warning(f"No {config_path} found - copied from {defaults_path}")
            print(f"\n*** Created {config_path} from defaults")
            print("*** IMPORTANT: Edit config.yaml to configure:")
            print(f"***   1. Ollama server URL (ollama.url)")
            print(f"***   2. Source root path (source.root)")
            print(f"***   3. Build command (source.build_command)")
            print(f"***")
            print(f"***   vim {config_path}\n")
            created_new_config = True
        else:
            logger.error(f"Configuration file not found: {config_path}")
            logger.error(f"Defaults file also not found: {defaults_path}")
            logger.info("Create a config.yaml with Ollama server URL and model settings.")
            sys.exit(1)
    
    logger.info(f"Loading configuration from {config_path}")
    config = load_yaml_config(config_path)
    
    # Check if beads (bd) CLI is installed
    bd_installed, bd_path = check_beads_installation()
    if not bd_installed:
        print("\n" + "=" * 70)
        print("WARNING: Beads (bd) CLI not found")
        print("=" * 70)
        print("The 'bd' command is not available in your PATH.")
        print("This project uses beads for issue tracking and progress management.")
        print()
        print("To install beads:")
        print("  1. Visit: https://github.com/steveyegge/beads")
        print("  2. Follow installation instructions")
        print("  3. Run: bd onboard")
        print()
        print("Without beads:")
        print("  - Issue tracking will be disabled")
        print("  - Directory work items won't be created")
        print("  - Progress tracking will be limited")
        print()
        print("Continuing without beads integration...")
        print("=" * 70 + "\n")
        logger.warning("Beads CLI not found - continuing without beads integration")
    
    from llm_client import create_client_from_config, LLMError, LLMConnectionError
    from build_executor import create_executor_from_config

    # Validate source.root early (directory existence) to fail fast before LLM probing.
    # Note: Full source-tree validation (Makefile/CMakeLists.txt) still happens below.
    if not args.validate_only:
        build_section = config.get('build', config.get('source', {}))
        source_root_raw = build_section.get('source_root', build_section.get('root', '..'))
        source_root_str = os.path.expandvars(str(source_root_raw)).strip()
        source_root = Path(source_root_str).expanduser()
        if not source_root.is_absolute():
            source_root = Path(__file__).resolve().parent / source_root
        source_root = source_root.resolve()
        if not source_root.is_dir():
            print("\n" + "=" * 70)
            print("ERROR: Invalid Source Tree Configuration")
            print("=" * 70)
            print(f"Source root is not a directory: {source_root}")
            print()
            print("Please fix config.yaml:")
            print(f"  1. Open: {config_path}")
            print("  2. Set source.root (or build.source_root) to a valid directory")
            print(f"  3. Example: source.root: \"{Path.home()}/freebsd-src\"")
            print("=" * 70 + "\n")
            sys.exit(1)
    
    try:
        logger.info("Connecting to LLM server(s)...")
        llm_client = create_client_from_config(config)
        logger.info("LLM connection validated successfully!")

        if args.validate_only:
            print("\n✓ LLM connection validated!")
            host_status = llm_client.get_host_status()
            for host in host_status:
                print(f"  Host: {host['url']} ({host['backend']}) -> model: {host['model']}")
            print(f"  Available models: {', '.join(llm_client.list_models())}")
            sys.exit(0)

    except LLMError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    
    try:
        builder = create_executor_from_config(config)
        logger.info(f"Build executor ready: {builder.config.source_root}")
    except Exception as e:
        logger.error(f"Failed to create build executor: {e}")
        sys.exit(1)

    source_root = builder.config.source_root

    # Validate build command against detected project type
    print("*** Validating build command...")
    build_command = builder.config.build_command
    validation = BuildValidator.validate_build_command(build_command, source_root)

    if validation.detected_project:
        print(f"    Detected project: {validation.detected_project.project_type} "
              f"(confidence: {validation.detected_project.confidence})")

    if validation.is_valid:
        print(f"    ✓ Build command validated")
    else:
        print(f"\n{'='*70}")
        print("WARNING: Build Command May Be Incorrect")
        print(f"{'='*70}")
        print(f"Build command: {build_command}")
        print()
        if validation.warnings:
            print("Warnings:")
            for warning in validation.warnings:
                print(f"  • {warning}")
        print()
        if validation.suggestions:
            print("Suggested commands:")
            for suggestion in validation.suggestions:
                print(f"  • {suggestion}")
        print()
        print("You can continue, but the build validation may not work correctly.")
        print(f"To fix: edit {config_path} and update source.build_command")
        print(f"{'='*70}\n")

        # Ask user if they want to continue
        response = input("Continue anyway? [y/N]: ").strip().lower()
        if response not in ('y', 'yes'):
            print("Exiting. Please fix build_command in config.yaml")
            sys.exit(1)
    
    # Validate source tree before proceeding
    is_valid, error_msg = validate_source_tree(source_root)
    if not is_valid:
        print("\n" + "=" * 70)
        print("ERROR: Invalid Source Tree Configuration")
        print("=" * 70)
        print(error_msg)
        print()
        if created_new_config:
            print("You just created a new config.yaml from defaults.")
            print("The default source.root setting is '..' which may not be correct.")
            print()
        print("Please fix config.yaml:")
        print(f"  1. Open: {config_path}")
        print(f"  2. Set source.root to your source tree path")
        print(f"  3. Example: source.root: \"{Path.home()}/freebsd-src\"")
        print(f"  4. Set source.build_command to your build command")
        print()
        print(f"Current source.root: {source_root}")
        print("=" * 70 + "\n")
        sys.exit(1)
    
    logger.info(f"Source tree validated: {source_root}")
    git_helper = GitHelper(source_root)
    
    review_config = config.get('review', {})
    
    # Load persona directory (contains all agent files)
    persona_name = review_config.get('persona', 'personas/default')
    
    # Resolve persona directory (relative to angry-ai directory)
    config_dir = Path(args.config).parent
    persona_dir = config_dir / persona_name
    
    if not persona_dir.exists():
        logger.error(f"Persona directory not found: {persona_dir}")
        logger.error(f"Expected structure: {persona_dir}/")
        logger.error(f"  - AI_START_HERE.md (bootstrap)")
        logger.error(f"  - LESSONS.md (learned patterns)")
        logger.error(f"  - REVIEW-SUMMARY.md (progress)")
        sys.exit(1)
    
    # Validate required persona files
    bootstrap_file = persona_dir / "AI_START_HERE.md"
    if not bootstrap_file.exists():
        logger.error(f"Bootstrap file not found: {bootstrap_file}")
        logger.error(f"Persona must contain AI_START_HERE.md")
        sys.exit(1)
    
    logger.info(f"Using persona: {persona_name}")
    
    # Create operations logger for internal metrics
    ops_logger = create_logger_from_config(config, source_root=source_root)
    
    # PRE-FLIGHT SANITY CHECK: Verify source builds before starting review
    # If build fails, revert commits until it builds again
    if not args.skip_preflight:
        # Get max_reverts from config, default to 100
        max_reverts = review_config.get('max_reverts', 100)
        
        if not preflight_sanity_check(builder, source_root, git_helper, max_reverts=max_reverts, ops_logger=ops_logger):
            logger.error("Pre-flight check failed. Cannot proceed safely.")
            logger.error("Use --skip-preflight to bypass this check (not recommended)")
            sys.exit(1)
    else:
        logger.warning("Skipping pre-flight build check (--skip-preflight)")
        print("\n⚠️  WARNING: Pre-flight check skipped!")
        print("   If source doesn't build, AI may make things worse.\n")

    source_config = config.get('source', {})
    preferred_branch = (
        source_config.get('branch')
        or source_config.get('preferred_branch')
        or None
    )
    ready, ready_msg = git_helper.ensure_repository_ready(
        preferred_branch=preferred_branch or 'main',
        allow_rebase=not git_helper.has_changes(),
    )
    if not ready:
        logger.error(f"Unable to prepare source tree: {ready_msg}")
        sys.exit(1)
    print(f"\n*** Source tree ready: {ready_msg}")
    
    loop = ReviewLoop(
        ollama_client=llm_client,
        build_executor=builder,
        source_root=source_root,
        persona_dir=persona_dir,
        review_config=review_config,
        ops_logger=ops_logger,
        target_directories=review_config.get('target_directories', 10) if not args.forever else 0,
        max_iterations_per_directory=review_config.get('max_iterations_per_directory', 200),
        max_parallel_files=review_config.get('max_parallel_files', 1),
        forever_mode=args.forever,
    )
    
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\n")
        print("*** Shutting down gracefully...")
        # Mark interrupted to stop any parallel work
        loop._interrupted = True
        # Cancel any active futures
        for future in loop._active_futures:
            if not future.done():
                future.cancel()
        print("*** No partial edits applied - source tree unchanged")
        logger.info("Interrupted by user - graceful shutdown")
        sys.exit(130)


if __name__ == "__main__":
    main()
