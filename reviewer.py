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
import subprocess
import sys

from index_generator import generate_index
from build_executor import BuildResult
from chunker import CFileChunker, format_chunk_for_review
from dataclasses import dataclass, field
from ops_logger import OpsLogger, create_logger_from_config
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

logger = logging.getLogger(__name__)


def load_yaml_config(config_path: Path) -> Dict[str, Any]:
    """Load YAML configuration file."""
    try:
        import yaml
        with open(config_path, 'r') as f:
            return yaml.safe_load(f)
    except ImportError:
        logger.warning("PyYAML not installed, using basic parser (install with: pip install pyyaml)")
        return _basic_yaml_parse(config_path)


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


class GitHelper:
    """Helper for git operations."""
    
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
    
    def _run(self, args: List[str], capture: bool = True) -> Tuple[int, str]:
        """Run a git command and return (returncode, output)."""
        cmd = ['git', '-C', str(self.repo_root)] + args
        if capture:
            result = subprocess.run(cmd, capture_output=True, text=True)
            return result.returncode, (result.stdout + result.stderr).strip()
        else:
            result = subprocess.run(cmd)
            return result.returncode, ""
    
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
    
    def commit(self, message: str) -> Tuple[bool, str]:
        """Commit staged changes."""
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
        return output
    
    def changed_files_list(self) -> List[str]:
        """Get list of changed files."""
        code, output = self._run(['diff', '--name-only', 'HEAD'])
        if output:
            return [f.strip() for f in output.split('\n') if f.strip()]
        return []


class FileEditor:
    """Handles file editing operations."""
    
    def __init__(self, git: GitHelper):
        self.git = git
    
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
                return False, f"OLD text not found in {file_path}", ""
            
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
    
    # Match both "ACTION:" and markdown "### Action:" variants
    ACTION_RE = re.compile(r'^(?:###\s+)?ACTION:\s*([A-Z_]+)(.*)$', re.MULTILINE | re.IGNORECASE)
    
    @classmethod
    def parse(cls, response: str) -> Optional[Dict[str, Any]]:
        """Parse an AI response for action directives."""
        matches = list(cls.ACTION_RE.finditer(response))
        if not matches:
            return None
        
        match = matches[-1]
        action = match.group(1).strip()
        arg = match.group(2).strip()
        body = response[match.end():].strip()
        
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
        target_directories: int = 10,
        max_iterations_per_directory: int = 200,
        log_dir: Optional[Path] = None,
        ops_logger: Optional[OpsLogger] = None,
    ):
        self.ollama = ollama_client
        self.builder = build_executor
        self.source_root = source_root
        self.persona_dir = persona_dir
        self.target_directories = target_directories
        self.max_iterations_per_directory = max_iterations_per_directory
        
        # All persona files live in persona_dir (keeps source tree clean!)
        self.bootstrap_file = persona_dir / "AI_START_HERE.md"
        self.lessons_file = persona_dir / "LESSONS.md"
        self.review_summary_file = persona_dir / "REVIEW-SUMMARY.md"
        
        # Logs go in persona directory too (or override)
        self.log_dir = log_dir or (persona_dir / 'logs')
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        self.git = GitHelper(source_root)
        self.editor = FileEditor(self.git)
        self.parser = ActionParser()
        self.chunker = CFileChunker(max_chunk_lines=500, small_file_threshold=800)
        
        self.session = ReviewSession(
            session_id=datetime.datetime.now().strftime("%Y%m%d_%H%M%S"),
            start_time=datetime.datetime.now(),
        )
        
        # Operations logger for internal metrics
        self.ops = ops_logger or OpsLogger(session_id=self.session.session_id)
        
        # Chunk tracking for large files
        self.current_chunks: List[Any] = []  # Chunks for current file
        self.current_chunk_index: int = 0  # Which chunk we're on
        self.chunked_file_path: Optional[Path] = None  # Path of file being chunked
        
        # Load bootstrap content
        self.bootstrap_content = self.bootstrap_file.read_text(encoding='utf-8')
        
        # Load or generate the review index
        print("*** Loading review index...")
        self.index = generate_index(source_root)
        print(f"    Found {len(self.index.entries)} reviewable directories")
        
        # Conversation history
        self.history: List[Dict[str, str]] = []
        
        self._init_conversation()
    
    def _init_conversation(self) -> None:
        """Initialize the conversation with system prompt, bootstrap, and index."""
        system_prompt = self._build_system_prompt()
        
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
  - Large files (>800 lines) are automatically chunked by function
  - You'll review function-by-function for better focus and performance

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

Respond with analysis followed by a single ACTION line.
"""
    
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
    
    def _resolve_path(self, path_str: str) -> Path:
        """Resolve a relative path within the source tree."""
        path = Path(path_str)
        if path.is_absolute():
            return path
        resolved = (self.source_root / path).resolve()
        
        try:
            resolved.relative_to(self.source_root)
        except ValueError:
            raise ValueError(f"Path escapes source root: {path_str}")
        
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
1. First line: "{component}: <short summary>" (50 chars max total)
2. Blank line
3. Body: explain WHAT changed and WHY (wrap at 72 chars)
4. Focus on the security/correctness fixes, not style changes
5. Use imperative mood ("Fix" not "Fixed")
6. This commit covers ALL changes in the {component} directory

Example format:
cpuset: Replace atoi() with strtonum() for safe integer parsing

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
            print(f"*** Commit message:\n{message}\n")
            return message
        else:
            return f"{component}: Code review fixes\n\nFiles: {files_list}"
    
    def _record_lesson(self, error_report: str, failed_fix_attempt: str = "") -> None:
        """Record a lesson learned from a build failure to LESSONS.md."""
        print("\n*** Recording lesson learned...")
        
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
            print(f"*** Lesson recorded to {self.lessons_file.relative_to(self.persona_dir.parent)}")
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
            print(f"*** Updated {self.review_summary_file.relative_to(self.persona_dir.parent)}")
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
            # Continue anyway, push might still work
        
        print("*** Pushing to origin...")
        success, output = self.git.push()
        if not success:
            # Retry once after pull
            print("*** Push failed, trying pull --rebase again...")
            self.git.pull_rebase()
            success, output = self.git.push()
            if not success:
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
            
            # Discover all reviewable files in directory
            files_in_dir = []
            for item in sorted(dir_path.iterdir()):
                if item.is_file() and not item.name.startswith('.'):
                    # Include source files, headers, man pages
                    if item.suffix in ['.c', '.h', '.cc', '.cpp', '.8', '.9', '.1', '.5'] or item.name == 'Makefile':
                        files_in_dir.append(str(item.relative_to(self.source_root)))
            
            # Update session state
            self.session.current_directory = directory
            self.session.files_in_current_directory = files_in_dir
            self.session.files_reviewed_in_directory = 0
            self.session.current_file = None
            self.session.current_file_chunks_total = 0
            self.session.current_file_chunks_reviewed = 0
            self.session.changed_files = []  # Reset changed files for new scope
            self.session.pending_changes = False
            
            # Update the review index to track current position
            self.index.set_current(directory)
            self.index.save()
            
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
            for f in files_in_dir[:10]:  # Show first 10
                result += f"  - {f}\n"
            if len(files_in_dir) > 10:
                result += f"  ... and {len(files_in_dir) - 10} more\n"
            
            result += f"\n{progress}\n\n"
            result += f"WORKFLOW:\n"
            result += f"1. Review each file (READ_FILE, NEXT_CHUNK for large files)\n"
            result += f"2. Make edits as needed (EDIT_FILE)\n"
            result += f"3. When ALL files reviewed: ACTION: BUILD\n"
            result += f"4. If build succeeds: Changes committed for entire directory\n"
            result += f"5. Move to next directory (SET_SCOPE)\n"
            
            print(f"\n*** Scope set to: {directory}")
            return result
        
        elif action_type == 'READ_FILE':
            path = self._resolve_path(action.get('file_path', ''))
            if not path.exists():
                return f"READ_FILE_ERROR: File not found: {path}\nTIP: Use FIND_FILE to locate files"
            
            # Update session tracking
            rel_path = str(path.relative_to(self.source_root))
            self.session.current_file = rel_path
            
            # Check if file should be chunked
            if self.chunker.should_chunk(path):
                # Start chunked review
                self.current_chunks = self.chunker.chunk_file(path)
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
            path = self._resolve_path(action.get('dir_path', ''))
            if not path.exists():
                return f"LIST_DIR_ERROR: Directory not found: {path}"
            if not path.is_dir():
                return f"LIST_DIR_ERROR: Not a directory: {path}"
            items = sorted(os.listdir(path))
            return f"LIST_DIR_RESULT for {path}:\n```\n" + '\n'.join(items) + "\n```"
        
        elif action_type == 'EDIT_FILE':
            path = self._resolve_path(action.get('file_path', ''))
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
                
                result = f"EDIT_FILE_OK: {message}\n\n"
                if self.session.current_directory:
                    result += f"Scope: {self.session.current_directory}\n"
                result += "GIT DIFF (verify this looks correct):\n"
                result += "```diff\n"
                result += diff if diff else "(no diff - file may be new or unchanged)"
                result += "\n```"
                
                print(f"\n*** Edited: {path}")
                print("*** Git diff:")
                print(diff if diff else "(no diff)")
                
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
            path = self._resolve_path(action.get('file_path', ''))
            content = action.get('content', '')
            
            if not content:
                return "WRITE_FILE_ERROR: Missing CONTENT block"
            
            success, message, diff = self.editor.write_file(path, content)
            
            if success:
                self.session.pending_changes = True
                self.session.last_diff = diff
                if str(path) not in self.session.changed_files:
                    self.session.changed_files.append(str(path.relative_to(self.source_root)))
                
                result = f"WRITE_FILE_OK: {message}\n\n"
                result += "GIT DIFF:\n```diff\n"
                result += diff if diff else "(new file)"
                result += "\n```"
                
                print(f"\n*** Wrote: {path}")
                print("*** Git diff:")
                print(diff if diff else "(new file)")
                
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
            
            if result.success:
                # Build succeeded!
                self.session.files_fixed += len(changed_files)
                
                # Log build success
                self.ops.build_success(result.duration_seconds, result.warning_count)
                
                # Generate commit message using AI (include directory context)
                commit_msg = self._generate_commit_message(
                    full_diff, changed_files, self.session.current_directory
                )
                
                # Update REVIEW-SUMMARY.md
                self._update_review_summary(
                    changed_files, commit_msg, self.session.current_directory
                )
                
                # Mark directory as done in the persistent index BEFORE commit
                # so REVIEW-INDEX.md is included in the commit
                if self.session.current_directory:
                    self.index.mark_done(self.session.current_directory, 
                                       f"Fixed by session {self.session.session_id}")
                    self.index.save()
                
                # Commit and push (includes REVIEW-SUMMARY.md and REVIEW-INDEX.md)
                success, output = self._commit_and_push(commit_msg)
                if success:
                    # Get commit hash for logging
                    _, commit_hash = self.git._run(['rev-parse', 'HEAD'])
                    commit_hash = commit_hash.strip()[:12]
                    
                    # Log commit success
                    self.ops.commit_success(commit_hash, changed_files)
                    
                    # Mark directory as completed in session
                    if self.session.current_directory:
                        if self.session.current_directory not in self.session.completed_directories:
                            self.session.completed_directories.append(self.session.current_directory)
                        self.session.directories_completed += 1
                        
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
                           f"REVIEW-SUMMARY.md and REVIEW-INDEX.md updated.\n\n" \
                           f"Completed directories so far: {self.session.directories_completed}" \
                           f"{next_msg}"
                else:
                    # Log commit failure
                    self.ops.commit_failure(output)
                    return f"BUILD_SUCCESS but commit/push failed: {output}\n" \
                           "Please commit manually."
            else:
                # Build failed - REVERT changes and record lesson learned
                self.session.build_failures += 1
                
                # Log build failure
                self.ops.build_failure(
                    result.duration_seconds,
                    error_count=result.error_count,
                    warning_count=result.warning_count,
                    error_summary=result.errors[0].message if result.errors else None,
                )
                
                # Get error report before reverting
                error_report = result.get_error_report()
                reverted_files = list(self.session.changed_files)
                
                # REVERT all changes - this is cheaper than trying to fix
                print("\n*** BUILD FAILED - Reverting changes...")
                for file_path in self.session.changed_files:
                    full_path = self.source_root / file_path
                    if full_path.exists():
                        code, output = self.git._run(['checkout', str(full_path)])
                        if code == 0:
                            print(f"    Reverted: {file_path}")
                        else:
                            print(f"    WARNING: Could not revert {file_path}: {output}")
                
                # Clear pending changes state
                self.session.pending_changes = False
                self.session.changed_files = []
                
                # Record lesson learned from the failure
                print("*** Recording lesson learned...")
                self._record_lesson(error_report, failed_fix_attempt=", ".join(reverted_files))
                
                # Commit and push LESSONS.md so the AI has it in context
                self.git.add(str(self.lessons_file))
                success, output = self.git.commit(f"LESSON: Build failure in {current_dir} - reverted changes")
                if success:
                    self.git.push()
                    print("*** LESSONS.md committed and pushed")
                
                # Build response for AI
                error_response = f"BUILD_FAILED: Build errors detected\n\n"
                error_response += f"REVERTED FILES:\n"
                for f in reverted_files:
                    error_response += f"  - {f}\n"
                error_response += f"\nBUILD ERROR REPORT:\n{error_report}\n\n"
                error_response += f"LESSON RECORDED: The failed approach has been documented in LESSONS.md.\n\n"
                error_response += f"NEXT STEPS:\n"
                error_response += f"1. The broken changes have been reverted automatically\n"
                error_response += f"2. Re-read the file(s) you were trying to fix\n"
                error_response += f"3. Try a DIFFERENT approach based on the error\n"
                error_response += f"4. Make smaller, more targeted changes\n"
                error_response += f"5. BUILD again when ready\n\n"
                error_response += f"You are still in: {self.session.current_directory}\n"
                
                return error_response
        
        elif action_type == 'HALT':
            # Check for incomplete work before allowing HALT
            
            # 1. Check for uncommitted changes
            if self.session.pending_changes:
                return f"HALT_REJECTED: You have uncommitted changes in {self.session.current_directory}.\n" \
                       f"Changed files: {', '.join(self.session.changed_files)}\n" \
                       f"Run BUILD to validate and commit these changes first."
            
            # 2. Check if no directories have been completed
            if self.session.directories_completed == 0:
                # Find directories that could be reviewed
                suggestions = self._find_reviewable_directories()
                if suggestions:
                    return f"HALT_REJECTED: No directories have been completed yet.\n" \
                           f"You must review at least one directory before halting.\n\n" \
                           f"Suggested directories to review:\n" + \
                           "\n".join(f"  - {d}" for d in suggestions[:5]) + \
                           f"\n\nUse SET_SCOPE to begin reviewing one of these directories."
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
                self.current_chunks = []
                self.current_chunk_index = 0
                self.chunked_file_path = None
                return f"SKIP_FILE_OK: Skipped remaining chunks of {file_path}"
            else:
                return "SKIP_FILE_OK: No chunked file to skip"
        
        else:
            return f"UNKNOWN_ACTION: {action_type}"
    
    def _find_reviewable_directories(self) -> List[str]:
        """
        Find directories in the source tree that contain C source and could be reviewed.
        Focuses on bin/, sbin/, usr.bin/, usr.sbin/ directories.
        """
        reviewable = []
        
        for top_dir in ['bin', 'sbin', 'usr.bin', 'usr.sbin']:
            top_path = self.source_root / top_dir
            if not top_path.exists():
                continue
            
            try:
                for subdir in sorted(top_path.iterdir()):
                    if not subdir.is_dir():
                        continue
                    
                    # Skip if already reviewed
                    rel_path = f"{top_dir}/{subdir.name}"
                    if rel_path in self.session.completed_directories:
                        continue
                    
                    # Check if it has C source files
                    has_c_files = any(subdir.glob('*.c'))
                    has_makefile = (subdir / 'Makefile').exists()
                    
                    if has_c_files and has_makefile:
                        reviewable.append(rel_path)
                    
                    if len(reviewable) >= 20:  # Limit results
                        break
            except Exception:
                continue
            
            if len(reviewable) >= 20:
                break
        
        return reviewable
    
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
            
            # Also revert REVIEW-INDEX.md if modified
            code, output = self.git._run(['checkout', 'REVIEW-INDEX.md'])
            if code == 0:
                print("    Reverted: REVIEW-INDEX.md")
            
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
        status = self.git.show_status()
        if status:
            print("\n*** Git status at start:")
            print(status)
        
        step = 0
        directory_iterations = 0  # Iterations spent on current directory
        last_directory = None
        
        # Continue until we've completed target directories (or 0 = unlimited)
        while self.target_directories == 0 or self.session.directories_completed < self.target_directories:
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
            dir_progress = f"{self.session.directories_completed}/{self.target_directories}" if self.target_directories > 0 else f"{self.session.directories_completed}"
            logger.info(f"Step {step} | Dir {dir_progress} | {self.session.current_directory or 'No scope'} ({directory_iterations}/{self.max_iterations_per_directory})")
            print(f"\n{'='*70}")
            print(f"STEP {step} | Directories: {dir_progress} | Current: {self.session.current_directory or 'None'} ({directory_iterations}/{self.max_iterations_per_directory})")
            if progress_summary:
                print(f"\n{progress_summary}")
            print('='*70)
            
            try:
                response = self.ollama.chat(self.history)
            except Exception as e:
                error_msg = str(e)
                if "timed out" in error_msg.lower() or "timeout" in error_msg.lower():
                    logger.error(f"Ollama timeout after {self.ollama.config.timeout}s")
                    self.ops.ai_timeout(self.ollama.config.timeout, f"Step {step}")
                    print(f"\n{'='*60}")
                    print("ERROR: Request timed out")
                    print('='*60)
                    print(f"The model took longer than {self.ollama.config.timeout}s to respond.")
                    print("This usually happens with large files (1000+ lines).")
                    print("\nSolutions:")
                    print(f"1. Increase timeout in config.yaml: ollama.timeout (currently {self.ollama.config.timeout}s)")
                    print("2. Review smaller files first")
                    print("3. Break large files into sections")
                    print('='*60)
                else:
                    logger.error(f"Ollama error: {e}")
                    self.ops.ai_error(error_msg)
                break
            
            last_user_msg = self.history[-1]['content'] if self.history else ""
            self._log_exchange(step, last_user_msg, response)
            
            print(f"\n{'='*60}")
            print("AI RESPONSE:")
            print('='*60)
            print(response)
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
                logger.info("Received HALT. Stopping.")
                break
            
            result = self._execute_action(action)
            logger.info(f"Action result: {result[:100]}...")
            
            self.history.append({"role": "user", "content": result})
            
            # Prune history if getting too long
            if len(self.history) > 42:
                self.history = self.history[:2] + self.history[-40:]
        
        # ALWAYS clean up dirty state before ending
        self._cleanup_dirty_state()
        
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
    print("\n" + "=" * 70)
    print("PRE-FLIGHT SANITY CHECK")
    print("=" * 70)
    print("Testing if source builds with configured build command...")
    print(f"Command: {builder.config.build_command}")
    print("=" * 70 + "\n")
    
    # Check for uncommitted changes (excluding .beads/ which we'll auto-stash)
    changes = git.show_status()
    if changes:
        # Check if only .beads/ files are modified
        non_beads_changes = [line for line in changes.split('\n') 
                            if line.strip() and not '.beads/' in line]
        
        if non_beads_changes:
            print("WARNING: Uncommitted changes detected (excluding .beads/):")
            print('\n'.join(non_beads_changes))
            print("\nCannot run pre-flight check with uncommitted changes.")
            print("Please commit or stash changes first.")
            print("Note: .beads/ changes are auto-stashed during recovery if needed.")
            return False
        
        print("Note: .beads/ changes detected - will be auto-stashed if recovery needed")
    
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
        
        # Stash any .beads/ changes before reverting
        beads_stashed = False
        if git.has_changes():
            print("Stashing .beads/ changes before reverting...")
            code, output = git._run(['stash', 'push', '-m', 'preflight-beads-backup', '.beads/'])
            if code == 0:
                beads_stashed = True
                print("✓ .beads/ changes stashed")
            else:
                print(f"WARNING: Could not stash .beads/ changes: {output}")
        
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
                
                # Restore beads changes if we stashed them
                if beads_stashed:
                    print("\nRestoring .beads/ changes...")
                    code, output = git._run(['stash', 'pop'])
                    if code == 0:
                        print("✓ .beads/ changes restored")
                    else:
                        print(f"WARNING: Could not restore .beads/ changes: {output}")
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
        
        # Restore beads changes if we stashed them
        if beads_stashed:
            print("\nRestoring .beads/ changes...")
            code, output = git._run(['stash', 'pop'])
            if code == 0:
                print("✓ .beads/ changes restored")
            else:
                print(f"WARNING: Could not restore .beads/ changes: {output}")
        
        print("\nManual intervention required.")
        print("The build has been broken for more than the last 100 commits.")
        print("=" * 70 + "\n")
        return False
        
    except Exception as e:
        print(f"\n✗ PRE-FLIGHT CHECK ERROR: {e}")
        print("Cannot verify build status. Proceeding with caution...\n")
        return False


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Angry AI - FreeBSD Code Reviewer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python reviewer.py                     # Use default config.yaml
    python reviewer.py --config my.yaml    # Use custom config
    python reviewer.py --validate-only     # Just validate Ollama connection
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
    
    args = parser.parse_args()
    
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s %(levelname)s [%(name)s] %(message)s',
        datefmt='%H:%M:%S'
    )
    
    config_path = Path(args.config)
    defaults_path = config_path.parent / "config.yaml.defaults"
    
    if not config_path.exists():
        # Try to copy from defaults
        if defaults_path.exists():
            import shutil
            shutil.copy(defaults_path, config_path)
            logger.warning(f"No {config_path} found - copied from {defaults_path}")
            print(f"\n*** Created {config_path} from defaults")
            print("*** IMPORTANT: Edit config.yaml to set your Ollama server URL!")
            print(f"***   vim {config_path}\n")
        else:
            logger.error(f"Configuration file not found: {config_path}")
            logger.error(f"Defaults file also not found: {defaults_path}")
            logger.info("Create a config.yaml with Ollama server URL and model settings.")
            sys.exit(1)
    
    logger.info(f"Loading configuration from {config_path}")
    config = load_yaml_config(config_path)
    
    from ollama_client import create_client_from_config, OllamaError
    from build_executor import create_executor_from_config
    
    try:
        logger.info("Connecting to Ollama server...")
        ollama = create_client_from_config(config)
        logger.info("Ollama connection validated successfully!")
        
        if args.validate_only:
            print("\n✓ Ollama connection validated!")
            print(f"  Server: {ollama.config.url}")
            print(f"  Model: {ollama.config.model}")
            print(f"  Available models: {', '.join(ollama.list_models())}")
            sys.exit(0)
        
    except OllamaError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    
    try:
        builder = create_executor_from_config(config)
        logger.info(f"Build executor ready: {builder.config.source_root}")
    except Exception as e:
        logger.error(f"Failed to create build executor: {e}")
        sys.exit(1)
    
    source_root = builder.config.source_root
    
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
        git_helper = GitHelper(source_root)
        
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
    
    loop = ReviewLoop(
        ollama_client=ollama,
        build_executor=builder,
        source_root=source_root,
        persona_dir=persona_dir,
        ops_logger=ops_logger,
        target_directories=review_config.get('target_directories', 10),
        max_iterations_per_directory=review_config.get('max_iterations_per_directory', 200),
    )
    
    try:
        loop.run()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(130)


if __name__ == "__main__":
    main()
