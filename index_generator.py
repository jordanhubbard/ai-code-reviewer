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
    status: str = "pending"      # pending, current, done, skipped
    reviewed_date: Optional[str] = None
    notes: str = ""


REVIEWABLE_SUFFIXES = {
    '.c', '.h', '.cc', '.cpp', '.cxx', '.s', '.S', '.sh', '.py', '.awk', '.ksh',
    '.mk', '.m4', '.rs', '.go', '.m', '.mm', '.1', '.2', '.3', '.4', '.5', '.6',
    '.7', '.8', '.9', '.txt', '.md', '.in'
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
                if not item.is_dir():
                    continue
                if item.name.startswith('.'):
                    continue
                
                # CRITICAL: Prevent self-review if ai-code-reviewer is in the source tree
                if item.name in ['ai-code-reviewer', 'angry-ai']:
                    continue
                
                rel_path = f"{prefix}/{item.name}"
                
                # Skip directories that are gitignored
                if is_git_ignored(self.source_root, rel_path):
                    continue
                
                c_files = []
                h_files = []
                reviewable_found = False
                try:
                    for child in item.iterdir():
                        if not child.is_file() or child.name.startswith('.'):
                            continue
                        
                        # Skip gitignored files
                        child_rel = f"{rel_path}/{child.name}"
                        if is_git_ignored(self.source_root, child_rel):
                            continue
                        
                        name = child.name
                        suffix = child.suffix.lower()
                        if name in REVIEWABLE_SPECIAL_FILES:
                            reviewable_found = True
                        if suffix == '.c':
                            c_files.append(child)
                            reviewable_found = True
                        elif suffix == '.h':
                            h_files.append(child)
                            reviewable_found = True
                        elif suffix in REVIEWABLE_SUFFIXES:
                            reviewable_found = True
                except PermissionError:
                    reviewable_found = False
                
                if reviewable_found:
                    total_lines = 0
                    for f in c_files + h_files:
                        try:
                            total_lines += sum(1 for _ in open(f, 'rb'))
                        except OSError:
                            pass
                    
                    entry = DirectoryEntry(
                        path=rel_path,
                        c_files=len(c_files),
                        h_files=len(h_files),
                        total_lines=total_lines,
                    )
                    
                    # Restore existing status
                    if rel_path in existing_status:
                        entry.status, entry.reviewed_date, entry.notes = \
                            existing_status[rel_path]
                    
                    self.entries[rel_path] = entry
                
                # Recurse into subdirectories (for lib/libc/*, etc.)
                if item.is_dir():
                    self._scan_directory(item, rel_path, existing_status)
                    
        except PermissionError:
            pass
    
    def _load(self) -> None:
        """Load index from file."""
        if not self.index_path.exists():
            return
        
        content = self.index_path.read_text()
        
        # Parse current position
        pos_match = re.search(r'CURRENT POSITION: `([^`]+)`', content)
        if pos_match:
            self.current_position = pos_match.group(1)
        
        # Parse entries
        # Format: - [x] `bin/cat` (2 .c, 1 .h, 450 lines) - 2024-01-15 - notes
        entry_re = re.compile(
            r'- \[(.)\] `([^`]+)` \((\d+) \.c, (\d+) \.h, (\d+) lines\)'
            r'(?: - (\d{4}-\d{2}-\d{2}))?'
            r'(?: - (.+))?$'
        )
        
        for line in content.split('\n'):
            match = entry_re.match(line.strip())
            if match:
                marker, path, c_files, h_files, lines, date, notes = match.groups()
                
                status_map = {'x': 'done', '>': 'current', '-': 'skipped', ' ': 'pending'}
                
                self.entries[path] = DirectoryEntry(
                    path=path,
                    c_files=int(c_files),
                    h_files=int(h_files),
                    total_lines=int(lines),
                    status=status_map.get(marker, 'pending'),
                    reviewed_date=date,
                    notes=notes.strip() if notes else "",
                )
    
    def save(self) -> None:
        """Save index to file."""
        lines = [
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
        
        # Statistics
        total = len(self.entries)
        done = sum(1 for e in self.entries.values() if e.status == 'done')
        skipped = sum(1 for e in self.entries.values() if e.status == 'skipped')
        pending = total - done - skipped
        
        lines.extend([
            "## Progress",
            f"- Total directories: {total}",
            f"- Completed: {done} ({100*done//total if total else 0}%)",
            f"- Skipped: {skipped}",
            f"- Remaining: {pending}",
            "",
        ])
        
        # Current position
        if self.current_position:
            lines.extend([
                "## Current Position",
                f"CURRENT POSITION: `{self.current_position}`",
                "",
            ])
        
        # Group by top-level directory
        for top_dir in self.TOP_DIRS:
            group = {k: v for k, v in self.entries.items() 
                    if k.startswith(f"{top_dir}/")}
            
            if not group:
                continue
            
            group_done = sum(1 for e in group.values() if e.status == 'done')
            
            lines.extend([
                f"## {top_dir}/ ({group_done}/{len(group)} done)",
                "",
            ])
            
            for path, entry in sorted(group.items()):
                marker_map = {'done': 'x', 'current': '>', 'skipped': '-', 'pending': ' '}
                marker = marker_map.get(entry.status, ' ')
                
                line = f"- [{marker}] `{entry.path}` ({entry.c_files} .c, {entry.h_files} .h, {entry.total_lines} lines)"
                
                if entry.reviewed_date:
                    line += f" - {entry.reviewed_date}"
                if entry.notes:
                    line += f" - {entry.notes}"
                
                lines.append(line)
            
            lines.append("")
        
        new_content = '\n'.join(lines)
        if self.index_path.exists():
            try:
                current_content = self.index_path.read_text()
            except Exception:
                current_content = None
            if current_content and self._normalize_index_text(current_content) == self._normalize_index_text(new_content):
                return
        self.index_path.write_text(new_content)

    @staticmethod
    def _normalize_index_text(content: str) -> str:
        """Strip volatile headers so that pure timestamp changes do not dirty git."""
        return re.sub(r'^Generated: .*$','Generated: <normalized>', content, flags=re.MULTILINE)
    
    def get_next_pending(self) -> Optional[str]:
        """Get the next pending directory to review."""
        for path, entry in self.entries.items():
            if entry.status == 'pending':
                return path
        return None
    
    def get_current(self) -> Optional[str]:
        """Get the directory currently being reviewed."""
        for path, entry in self.entries.items():
            if entry.status == 'current':
                return path
        return self.current_position
    
    def set_current(self, path: str) -> None:
        """Set a directory as currently being reviewed."""
        # Clear any existing current
        for entry in self.entries.values():
            if entry.status == 'current':
                entry.status = 'pending'
        
        if path in self.entries:
            self.entries[path].status = 'current'
        self.current_position = path
    
    def mark_done(self, path: str, notes: str = "") -> None:
        """Mark a directory as completed."""
        if path in self.entries:
            self.entries[path].status = 'done'
            self.entries[path].reviewed_date = datetime.now().strftime('%Y-%m-%d')
            if notes:
                self.entries[path].notes = notes
        
        # Move current position to next
        self.current_position = self.get_next_pending()
    
    def mark_skipped(self, path: str, reason: str = "") -> None:
        """Mark a directory as skipped."""
        if path in self.entries:
            self.entries[path].status = 'skipped'
            self.entries[path].notes = reason
    
    def get_summary_for_ai(self) -> str:
        """
        Generate a concise summary for the AI to understand position.
        """
        current = self.get_current()
        next_pending = self.get_next_pending()
        
        done = sum(1 for e in self.entries.values() if e.status == 'done')
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
                 if e.status == 'done' and e.reviewed_date]
        recent.sort(key=lambda e: e.reviewed_date or '', reverse=True)
        
        if recent[:3]:
            lines.append("")
            lines.append("Recently completed:")
            for e in recent[:3]:
                lines.append(f"  ✓ {e.path} ({e.reviewed_date})")
        
        # Show next few pending
        pending = [e.path for e in self.entries.values() if e.status == 'pending']
        if pending[:5]:
            lines.append("")
            lines.append("Next in queue:")
            for p in pending[:5]:
                lines.append(f"  → {p}")
        
        return '\n'.join(lines)


def generate_index(source_root: Path) -> ReviewIndex:
    """Generate or load the review index."""
    index = ReviewIndex(source_root)
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
    index = generate_index(source_root)
    print(f"Found {len(index.entries)} reviewable directories")
    print(f"Index saved to: {index.index_path}")
    print()
    print(index.get_summary_for_ai())

