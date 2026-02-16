#!/usr/bin/env python3
"""
Index Generator for Angry AI

Generates and maintains a structured index of all reviewable directories
in the FreeBSD source tree. This gives the AI a "file browser" view of
the entire codebase with progress tracking.

The index file (REVIEW-INDEX.md) contains:
- All directories with C source code
- Status markers: [ ] pending, [>] current, [x] done, [-] skipped
- File counts and metadata
- Current position pointer for resuming work
"""

import os
import subprocess
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Iterator
from datetime import datetime
import re


# ============================================================================
# Constants
# ============================================================================

class Status:
    """Status values for directory entries."""
    PENDING = "pending"
    CURRENT = "current"
    DONE = "done"
    SKIPPED = "skipped"


class StatusMarker:
    """Markdown checkbox markers for each status."""
    PENDING = " "
    CURRENT = ">"
    DONE = "x"
    SKIPPED = "-"


# Bidirectional mappings between status and marker
STATUS_TO_MARKER = {
    Status.PENDING: StatusMarker.PENDING,
    Status.CURRENT: StatusMarker.CURRENT,
    Status.DONE: StatusMarker.DONE,
    Status.SKIPPED: StatusMarker.SKIPPED,
}

MARKER_TO_STATUS = {
    StatusMarker.PENDING: Status.PENDING,
    StatusMarker.CURRENT: Status.CURRENT,
    StatusMarker.DONE: Status.DONE,
    StatusMarker.SKIPPED: Status.SKIPPED,
}

# Regex patterns for parsing index file
CURRENT_POSITION_PATTERN = re.compile(r'CURRENT POSITION: `([^`]+)`')
INDEX_ENTRY_PATTERN = re.compile(
    r'- \[(.)\] `([^`]+)` \((\d+) \.c, (\d+) \.h, (\d+) lines\)'
    r'(?: - (\d{4}-\d{2}-\d{2}))?'
    r'(?: - (.+))?$'
)


# ============================================================================
# Utility Functions
# ============================================================================

def is_git_ignored(repo_root: Path, path: str) -> bool:
    """Check if a path is ignored by .gitignore."""
    # Always ignore .git directory
    if path.startswith('.git/') or path == '.git' or '/.git/' in path:
        return True
    
    try:
        result = subprocess.run(
            ['git', '-C', str(repo_root), 'check-ignore', '-q', path],
            capture_output=True,
            text=True
        )
        return result.returncode == 0
    except Exception:
        return False


@dataclass
class DirectoryEntry:
    """Represents a reviewable directory."""
    path: str                    # e.g., "bin/cpuset"
    c_files: int = 0             # Number of .c files
    h_files: int = 0             # Number of .h files
    total_lines: int = 0         # Approximate line count
    status: str = Status.PENDING # pending, current, done, skipped
    reviewed_date: Optional[str] = None
    notes: str = ""


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


class DirectoryEntryMap(dict):
    """Dictionary of DirectoryEntry keyed by path, iterates over values."""

    def __iter__(self) -> Iterator['DirectoryEntry']:
        return iter(self.values())


class ReviewIndex:
    """
    Manages the review index file.
    
    The index provides a persistent view of:
    - What directories exist and need review
    - Current progress (what's done, what's next)
    - Where to resume after interruption
    """
    
    INDEX_FILE = "REVIEW-INDEX.md"
    META_DIR = ".ai-code-reviewer"
    
    # Directories to scan for reviewable code
    TOP_DIRS = [
        'bin', 'sbin', 'usr.bin', 'usr.sbin', 'lib', 'libexec',
        'cddl', 'contrib', 'crypto', 'gnu', 'include', 'kerberos5', 'krb5',
        'release', 'secure', 'share', 'stand', 'sys', 'targets', 'tests', 'tools'
    ]
    
    def __init__(self, source_root: Path):
        self.source_root = source_root
        # Store index in .ai-code-reviewer/ metadata directory
        self.meta_dir = source_root / self.META_DIR
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.meta_dir / self.INDEX_FILE
        
        # Check for legacy index location and migrate
        self._migrate_legacy_index()
        
        self.entries: DirectoryEntryMap = DirectoryEntryMap()
        self.current_position: Optional[str] = None
    
    def _migrate_legacy_index(self) -> None:
        """Migrate REVIEW-INDEX.md from source root to .ai-code-reviewer/"""
        legacy_path = self.source_root / self.INDEX_FILE
        
        if not legacy_path.exists():
            return  # No legacy file to migrate
        
        if self.index_path.exists():
            return  # Already migrated
        
        try:
            import shutil
            shutil.move(str(legacy_path), str(self.index_path))
            print(f"*** Migrated {self.INDEX_FILE} to {self.META_DIR}/")
        except Exception as e:
            print(f"WARNING: Could not migrate {self.INDEX_FILE}: {e}")
    
    def generate(self) -> None:
        """
        Scan the source tree and generate a fresh index.
        Preserves existing status if index already exists.
        """
        # Load existing status if available
        existing_status = {}
        if self.index_path.exists():
            self._load()
            existing_status = {e.path: (e.status, e.reviewed_date, e.notes) 
                             for e in self.entries.values()}
        
        self.entries = DirectoryEntryMap()
        
        for top_dir in self.TOP_DIRS:
            top_path = self.source_root / top_dir
            if not top_path.exists():
                continue
            
            self._scan_directory(top_path, top_dir, existing_status)
        
        # Sort entries
        self.entries = DirectoryEntryMap(dict(sorted(self.entries.items())))
    
    def _scan_directory(self, path: Path, prefix: str,
                        existing_status: Dict) -> None:
        """Recursively scan for directories with C source."""
        try:
            for item in sorted(path.iterdir()):
                if not self._should_process_directory(item):
                    continue

                rel_path = f"{prefix}/{item.name}"

                # Skip directories that are gitignored
                if is_git_ignored(self.source_root, rel_path):
                    continue

                # Scan directory contents
                c_files, h_files, reviewable_found = self._scan_directory_contents(item, rel_path)

                if reviewable_found:
                    total_lines = self._count_lines(c_files + h_files)

                    entry = DirectoryEntry(
                        path=rel_path,
                        c_files=len(c_files),
                        h_files=len(h_files),
                        total_lines=total_lines,
                    )

                    # Restore existing status
                    self._restore_existing_status(entry, rel_path, existing_status)

                    self.entries[rel_path] = entry

                # Recurse into subdirectories (for lib/libc/*, etc.)
                if item.is_dir():
                    self._scan_directory(item, rel_path, existing_status)

        except PermissionError:
            pass

    def _should_process_directory(self, item: Path) -> bool:
        """Check if a directory should be processed."""
        if not item.is_dir():
            return False
        if item.name.startswith('.'):
            return False
        # CRITICAL: Prevent self-review if ai-code-reviewer is in the source tree
        if item.name in ['ai-code-reviewer', 'angry-ai']:
            return False
        return True

    def _scan_directory_contents(self, directory: Path, rel_path: str) -> tuple:
        """Scan a directory's contents and identify reviewable files."""
        c_files = []
        h_files = []
        reviewable_found = False

        try:
            for child in directory.iterdir():
                if not child.is_file() or child.name.startswith('.'):
                    continue

                # Skip gitignored files
                child_rel = f"{rel_path}/{child.name}"
                if is_git_ignored(self.source_root, child_rel):
                    continue

                if self._is_file_reviewable(child, c_files, h_files):
                    reviewable_found = True

        except PermissionError:
            reviewable_found = False

        return c_files, h_files, reviewable_found

    def _is_file_reviewable(self, file_path: Path, c_files: List[Path],
                           h_files: List[Path]) -> bool:
        """Determine if a file is reviewable and update file lists."""
        name = file_path.name
        suffix = file_path.suffix.lower()

        # Skip excluded file types (test data, output files, etc.)
        if suffix in EXCLUDED_SUFFIXES:
            return False

        if name in REVIEWABLE_SPECIAL_FILES:
            return True

        if suffix == '.c':
            c_files.append(file_path)
            return True
        elif suffix == '.h':
            h_files.append(file_path)
            return True
        elif suffix in REVIEWABLE_SUFFIXES:
            return True

        return False

    def _count_lines(self, files: List[Path]) -> int:
        """Count total lines across all files."""
        total_lines = 0
        for f in files:
            try:
                with open(f, 'rb') as fh:
                    total_lines += sum(1 for _ in fh)
            except OSError:
                pass
        return total_lines

    def _restore_existing_status(self, entry: DirectoryEntry, rel_path: str,
                                 existing_status: Dict) -> None:
        """Restore existing status from previous index."""
        if rel_path in existing_status:
            entry.status, entry.reviewed_date, entry.notes = existing_status[rel_path]
    
    def _load(self) -> None:
        """Load index from file."""
        if not self.index_path.exists():
            return

        content = self.index_path.read_text()
        self.current_position = self._parse_current_position(content)
        self._parse_entries(content)

    def _parse_current_position(self, content: str) -> Optional[str]:
        """Parse the current position marker from index content."""
        pos_match = CURRENT_POSITION_PATTERN.search(content)
        if pos_match:
            return pos_match.group(1)
        return None

    def _parse_entries(self, content: str) -> None:
        """Parse all directory entries from index content."""
        for line in content.split('\n'):
            entry = self._parse_entry_line(line.strip())
            if entry:
                self.entries[entry.path] = entry

    def _parse_entry_line(self, line: str) -> Optional[DirectoryEntry]:
        """Parse a single entry line into a DirectoryEntry object."""
        match = INDEX_ENTRY_PATTERN.match(line)
        if not match:
            return None

        marker, path, c_files, h_files, lines, date, notes = match.groups()
        status = MARKER_TO_STATUS.get(marker, Status.PENDING)

        return DirectoryEntry(
            path=path,
            c_files=int(c_files),
            h_files=int(h_files),
            total_lines=int(lines),
            status=status,
            reviewed_date=date,
            notes=notes.strip() if notes else "",
        )
    
    def save(self) -> None:
        """Save index to file."""
        lines = []
        lines.extend(self._format_header())
        lines.extend(self._format_statistics())
        lines.extend(self._format_current_position())
        lines.extend(self._format_directory_groups())

        new_content = '\n'.join(lines)
        if self._should_write_file(new_content):
            self.index_path.write_text(new_content)

    def _format_header(self) -> List[str]:
        """Format the file header section."""
        return [
            "# FreeBSD Source Review Index",
            "",
            "This file tracks review progress across the source tree.",
            "**DO NOT EDIT MANUALLY** - Updated automatically by the review tool.",
            "",
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "",
            "## Status Legend",
            "- `[ ]` Pending - needs review",
            "- `[>]` Current - being reviewed now",
            "- `[x]` Done - reviewed and committed",
            "- `[-]` Skipped - no changes needed or deferred",
            "",
        ]

    def _format_statistics(self) -> List[str]:
        """Format the progress statistics section."""
        total = len(self.entries)
        done = sum(1 for e in self.entries.values() if e.status == Status.DONE)
        skipped = sum(1 for e in self.entries.values() if e.status == Status.SKIPPED)
        pending = total - done - skipped

        return [
            "## Progress",
            f"- Total directories: {total}",
            f"- Completed: {done} ({100*done//total if total else 0}%)",
            f"- Skipped: {skipped}",
            f"- Remaining: {pending}",
            "",
        ]

    def _format_current_position(self) -> List[str]:
        """Format the current position section."""
        if self.current_position:
            return [
                "## Current Position",
                f"CURRENT POSITION: `{self.current_position}`",
                "",
            ]
        return []

    def _format_directory_groups(self) -> List[str]:
        """Format all directory group sections."""
        lines = []
        for top_dir in self.TOP_DIRS:
            group_lines = self._format_directory_group(top_dir)
            lines.extend(group_lines)
        return lines

    def _format_directory_group(self, top_dir: str) -> List[str]:
        """Format a single directory group section."""
        group = {k: v for k, v in self.entries.items()
                if k.startswith(f"{top_dir}/")}

        if not group:
            return []

        group_done = sum(1 for e in group.values() if e.status == Status.DONE)
        lines = [
            f"## {top_dir}/ ({group_done}/{len(group)} done)",
            "",
        ]

        for path, entry in sorted(group.items()):
            lines.append(self._format_entry_line(entry))

        lines.append("")
        return lines

    def _format_entry_line(self, entry: DirectoryEntry) -> str:
        """Format a single entry line."""
        marker = STATUS_TO_MARKER.get(entry.status, StatusMarker.PENDING)
        line = f"- [{marker}] `{entry.path}` ({entry.c_files} .c, {entry.h_files} .h, {entry.total_lines} lines)"

        if entry.reviewed_date:
            line += f" - {entry.reviewed_date}"
        if entry.notes:
            line += f" - {entry.notes}"

        return line

    def _should_write_file(self, new_content: str) -> bool:
        """Determine if the file should be written based on content changes."""
        if not self.index_path.exists():
            return True

        try:
            current_content = self.index_path.read_text()
        except Exception:
            return True

        # Only write if content has changed (ignoring timestamp)
        return self._normalize_index_text(current_content) != self._normalize_index_text(new_content)

    @staticmethod
    def _normalize_index_text(content: str) -> str:
        """Strip volatile headers so that pure timestamp changes do not dirty git."""
        return re.sub(r'^Generated: .*$','Generated: <normalized>', content, flags=re.MULTILINE)
    
    def get_next_pending(self) -> Optional[str]:
        """Get the next pending directory to review."""
        for path, entry in self.entries.items():
            if entry.status == Status.PENDING:
                return path
        return None

    def get_current(self) -> Optional[str]:
        """Get the directory currently being reviewed."""
        for path, entry in self.entries.items():
            if entry.status == Status.CURRENT:
                return path
        return self.current_position

    def set_current(self, path: str) -> None:
        """Set a directory as currently being reviewed."""
        # Clear any existing current
        for entry in self.entries.values():
            if entry.status == Status.CURRENT:
                entry.status = Status.PENDING

        if path in self.entries:
            self.entries[path].status = Status.CURRENT
        self.current_position = path

    def mark_done(self, path: str, notes: str = "") -> None:
        """Mark a directory as completed."""
        if path in self.entries:
            self.entries[path].status = Status.DONE
            self.entries[path].reviewed_date = datetime.now().strftime('%Y-%m-%d')
            if notes:
                self.entries[path].notes = notes

        # Move current position to next
        self.current_position = self.get_next_pending()

    def mark_skipped(self, path: str, reason: str = "") -> None:
        """Mark a directory as skipped."""
        if path in self.entries:
            self.entries[path].status = Status.SKIPPED
            self.entries[path].notes = reason

    def get_summary_for_ai(self) -> str:
        """
        Generate a concise summary for the AI to understand position.
        """
        current = self.get_current()
        next_pending = self.get_next_pending()

        done = sum(1 for e in self.entries.values() if e.status == Status.DONE)
        total = len(self.entries)

        lines = [
            "=== REVIEW INDEX SUMMARY ===",
            f"Progress: {done}/{total} directories completed ({100*done//total if total else 0}%)",
            "",
        ]

        if current:
            lines.append(f"CURRENT: {current}")

        if next_pending and next_pending != current:
            lines.append(f"NEXT: {next_pending}")

        # Show recent completions
        recent = [e for e in self.entries.values()
                 if e.status == Status.DONE and e.reviewed_date]
        recent.sort(key=lambda e: e.reviewed_date or '', reverse=True)

        if recent[:3]:
            lines.append("")
            lines.append("Recently completed:")
            for e in recent[:3]:
                lines.append(f"  ✓ {e.path} ({e.reviewed_date})")

        # Show next few pending
        pending = [e.path for e in self.entries.values() if e.status == Status.PENDING]
        if pending[:5]:
            lines.append("")
            lines.append("Next in queue:")
            for p in pending[:5]:
                lines.append(f"  → {p}")

        return '\n'.join(lines)


def generate_index(source_root: Path, force_rebuild: bool = False) -> ReviewIndex:
    """Generate or load the review index."""
    index = ReviewIndex(source_root)
    if index.index_path.exists() and not force_rebuild:
        try:
            index._load()
            print(f"*** Using existing review index at {index.index_path}")
            return index
        except Exception as exc:
            print(f"WARNING: Failed to load review index ({exc}); regenerating")
    index.generate()
    index.save()
    return index


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        source_root = Path(sys.argv[1])
    else:
        source_root = Path(__file__).parent.parent
    
    print(f"Generating index for: {source_root}")
    index = generate_index(source_root, force_rebuild=True)
    print(f"Found {len(index.entries)} reviewable directories")
    print(f"Index saved to: {index.index_path}")
    print()
    print(index.get_summary_for_ai())

