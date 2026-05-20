#!/usr/bin/env python3
"""
Index Generator for Angry AI

Generates and maintains a structured index of all workable directories
in a source tree. This gives the AI a "file browser" view of the entire
codebase with progress tracking.

The index file contains:
- All directories with reviewable source code
- Status markers: [ ] pending, [>] current, [x] done, [-] skipped
- File counts and metadata
- Current position pointer for resuming work
"""

import os
import subprocess
import json
import shlex
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Dict, Optional, Iterator, Set
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

INDEX_WORKFLOWS = {
    "review": {
        "index_file": "REVIEW-INDEX.md",
        "title": "Source Review Index",
        "noun": "review",
        "verb": "review",
        "gerund": "reviewing",
        "done": "reviewed",
        "summary_heading": "REVIEW INDEX SUMMARY",
    },
    "rewrite": {
        "index_file": "REWRITE-INDEX.md",
        "title": "Source Rewrite Index",
        "noun": "rewrite",
        "verb": "rewrite",
        "gerund": "rewriting",
        "done": "rewritten",
        "summary_heading": "REWRITE INDEX SUMMARY",
    },
}


def normalize_index_workflow(workflow_mode: str = "review") -> str:
    """Normalize and validate a workflow mode for index storage."""
    mode = (workflow_mode or "review").strip().lower().replace("-", "_")
    if mode == "rewriter":
        mode = "rewrite"
    if mode not in INDEX_WORKFLOWS:
        supported = ", ".join(sorted(INDEX_WORKFLOWS))
        raise ValueError(f"Unsupported workflow mode '{workflow_mode}'. Supported modes: {supported}")
    return mode

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
UNIT_METADATA_PATTERN = re.compile(r'<!--\s*unit:\s*(\{.*\})\s*-->\s*$')

REWRITE_STAGE_ORDER = {
    "foundation": 0,
    "bootstrap": 1,
    "application": 2,
    "validation": 3,
    "integration": 4,
    "kernel": 5,
    "unknown": 99,
}

REWRITE_SELECTION_POLICY_ALIASES = {
    "": "bottom_up",
    "default": "bottom_up",
    "bottomup": "bottom_up",
    "bottom_up": "bottom_up",
    "stage": "bottom_up",
    "stage_order": "bottom_up",
    "small": "small_first",
    "smallest": "small_first",
    "small_first": "small_first",
    "quick": "small_first",
    "smoke": "small_first",
    "smoke_test": "small_first",
}

REWRITE_SMALL_FIRST_KIND_ORDER = {
    "freebsd-command": 0,
    "rust-binary": 0,
    "rust-library": 1,
    "rust-module": 1,
    "freebsd-library": 2,
    "makefile-module": 3,
    "bootstrap-tool": 4,
    "rust-tests": 5,
    "freebsd-tests": 5,
    "directory": 6,
    "freebsd-kernel": 9,
}

REWRITE_IMPLEMENTATION_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cxx",
    ".m", ".mm",
    ".s",
    ".l", ".y", ".ll", ".yy",
    ".rs", ".go",
    ".sh", ".bash", ".ksh", ".zsh",
    ".py", ".awk", ".sed", ".perl", ".pl",
}


# ============================================================================
# Utility Functions
# ============================================================================

def normalize_rewrite_selection_policy(policy: Optional[str] = None) -> str:
    """Normalize the configured rewrite work-unit selection policy."""
    raw = (policy or "bottom_up").strip().lower().replace("-", "_")
    return REWRITE_SELECTION_POLICY_ALIASES.get(raw, "bottom_up")


def normalize_rewrite_source_suffixes(value: Any = None) -> Optional[Set[str]]:
    """Normalize optional source suffix filters for rewrite work-unit selection."""
    if value is None:
        return None

    if isinstance(value, str):
        raw_items: Iterable[Any] = re.split(r"[\s,]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw_items = value
    else:
        return None

    suffixes: Set[str] = set()
    for item in raw_items:
        text = str(item).strip().lower()
        if not text:
            continue
        suffixes.add(text if text.startswith(".") else f".{text}")

    return suffixes or None


class GitIgnoreIndex:
    """Fast in-process matcher for paths ignored by Git."""

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.ignored_paths: Set[str] = set()
        self.ignored_dirs: Set[str] = set()
        self._load()

    @staticmethod
    def _normalize(path: str) -> str:
        return path.strip("/").replace("\\", "/")

    def _load(self) -> None:
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.repo_root),
                    "ls-files",
                    "--others",
                    "--ignored",
                    "--exclude-standard",
                    "--directory",
                    "-z",
                ],
                capture_output=True,
            )
        except Exception:
            return

        if result.returncode != 0:
            return

        for raw_path in result.stdout.split(b"\0"):
            if not raw_path:
                continue
            decoded = raw_path.decode("utf-8", errors="replace")
            is_dir = decoded.endswith("/") or decoded.endswith("\\")
            path = self._normalize(decoded)
            if not path:
                continue
            if is_dir:
                self.ignored_dirs.add(path)
            else:
                self.ignored_paths.add(path)

    def is_ignored(self, path: str) -> bool:
        path = self._normalize(path)
        if not path:
            return False
        if path == ".git" or path.startswith(".git/") or "/.git/" in path:
            return True
        if path in self.ignored_paths or path in self.ignored_dirs:
            return True

        parts = path.split("/")
        for idx in range(1, len(parts)):
            if "/".join(parts[:idx]) in self.ignored_dirs:
                return True
        return False


_GIT_IGNORE_INDEXES: Dict[str, GitIgnoreIndex] = {}


def is_git_ignored(repo_root: Path, path: str) -> bool:
    """Check if a path is ignored by .gitignore."""
    root = str(repo_root.resolve())
    checker = _GIT_IGNORE_INDEXES.get(root)
    if checker is None:
        checker = GitIgnoreIndex(repo_root)
        _GIT_IGNORE_INDEXES[root] = checker
    return checker.is_ignored(path)


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
    unit_kind: str = "directory"
    stage: str = "unknown"
    depends_on: List[str] = field(default_factory=list)
    files: List[str] = field(default_factory=list)
    build_command: Optional[str] = None
    test_command: Optional[str] = None
    install_command: Optional[str] = None


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
    Manages the workflow index file.
    
    The index provides a persistent view of:
    - What directories exist and need work
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
    GENERIC_SKIP_DIRS = {
        'target', 'node_modules', '__pycache__', '.venv', 'venv',
    }
    
    def __init__(self, source_root: Path, workflow_mode: str = "review"):
        self.source_root = source_root
        self.workflow_mode = normalize_index_workflow(workflow_mode)
        self.workflow = INDEX_WORKFLOWS[self.workflow_mode]
        self.INDEX_FILE = self.workflow["index_file"]
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
        if self.workflow_mode != "review":
            return

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
        
        scanned_known_layout = False
        for top_dir in self.TOP_DIRS:
            top_path = self.source_root / top_dir
            if not top_path.exists():
                continue
            scanned_known_layout = True
            
            self._scan_directory(top_path, top_dir, existing_status)

        if not scanned_known_layout or not self.entries:
            self._scan_directory(self.source_root, "", existing_status)

        self._infer_rewrite_work_units()
        
        # Sort entries
        self.entries = self._ordered_entries()
    
    def _scan_directory(self, path: Path, prefix: str,
                        existing_status: Dict) -> None:
        """Recursively scan for directories with reviewable source."""
        try:
            for item in sorted(path.iterdir()):
                if not self._should_process_directory(item):
                    continue

                rel_path = self._join_rel_path(prefix, item.name)

                # Skip directories that are gitignored
                if is_git_ignored(self.source_root, rel_path):
                    continue

                # Scan directory contents
                c_files, h_files, line_files, reviewable_found = self._scan_directory_contents(item, rel_path)

                if reviewable_found:
                    total_lines = self._count_lines(line_files)

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
        if item.name in self.GENERIC_SKIP_DIRS:
            return False
        # CRITICAL: Prevent self-review if ai-code-reviewer is in the source tree
        if item.name in ['ai-code-reviewer', 'angry-ai']:
            return False
        return True

    @staticmethod
    def _join_rel_path(prefix: str, name: str) -> str:
        """Join a scanned prefix and child name without leading slashes."""
        return f"{prefix}/{name}" if prefix else name

    def _scan_directory_contents(self, directory: Path, rel_path: str) -> tuple:
        """Scan a directory's contents and identify reviewable files."""
        c_files = []
        h_files = []
        line_files = []
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
                    line_files.append(child)

        except PermissionError:
            reviewable_found = False

        return c_files, h_files, line_files, reviewable_found

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

    def ensure_rewrite_work_units(self) -> bool:
        """Refresh inferred rewrite work-unit metadata for a loaded index."""
        if self.workflow_mode != "rewrite":
            return False

        before = self._metadata_snapshot()
        self._infer_rewrite_work_units()
        self.entries = self._ordered_entries()
        return before != self._metadata_snapshot()

    def _metadata_snapshot(self) -> Dict[str, Any]:
        """Return work-unit metadata for change detection."""
        return {
            "__order__": list(self.entries.keys()),
            "entries": {
                path: self._unit_metadata(entry)
                for path, entry in self.entries.items()
            },
        }

    def _infer_rewrite_work_units(self) -> None:
        """Infer functional rewrite units from the source tree layout."""
        if self.workflow_mode != "rewrite":
            return

        for entry in self.entries.values():
            entry.files = self._candidate_files_for_entry(entry.path)
            entry.unit_kind = "directory"
            entry.stage = "unknown"
            entry.depends_on = []
            entry.build_command = None
            entry.test_command = None
            entry.install_command = None

            if self._infer_rust_work_unit(entry):
                continue
            self._infer_makefile_work_unit(entry)

    def _candidate_files_for_entry(self, rel_path: str) -> List[str]:
        """Return direct source/build files that belong to a directory entry."""
        directory = self.source_root / rel_path
        files: List[str] = []
        if not directory.is_dir():
            return files

        try:
            for child in sorted(directory.iterdir()):
                if not child.is_file() or child.name.startswith("."):
                    continue
                child_rel = str(child.relative_to(self.source_root))
                if is_git_ignored(self.source_root, child_rel):
                    continue
                if self._is_file_reviewable(child, [], []):
                    files.append(child_rel)
        except PermissionError:
            return files

        return files

    def _infer_rust_work_unit(self, entry: DirectoryEntry) -> bool:
        """Infer Rust package/module/test work-unit metadata."""
        directory = self.source_root / entry.path
        cargo_root = self._find_cargo_root(directory)
        if cargo_root is None:
            return False

        cargo_rel = self._rel_or_dot(cargo_root)
        cargo_manifest = "Cargo.toml" if cargo_rel == "." else f"{cargo_rel}/Cargo.toml"
        package_name = self._read_cargo_package_name(cargo_root / "Cargo.toml")
        command = self._cargo_test_command(cargo_root, package_name)

        entry.files = self._unique_paths([*entry.files, cargo_manifest])
        entry.build_command = command
        entry.test_command = command

        parts = Path(entry.path).parts
        direct_names = {Path(f).name for f in entry.files}
        src_unit = self._rust_src_unit_for(cargo_root)

        if parts and parts[-1] == "tests":
            entry.unit_kind = "rust-tests"
            entry.stage = "validation"
            if src_unit and src_unit in self.entries and src_unit != entry.path:
                entry.depends_on = [src_unit]
            return True

        if Path(entry.path).name == "src":
            tests_dir = cargo_root / "tests"
            if tests_dir.is_dir():
                try:
                    test_files = [
                        str(p.relative_to(self.source_root))
                        for p in sorted(tests_dir.glob("*.rs"))
                        if p.is_file()
                    ]
                    entry.files = self._unique_paths([*entry.files, *test_files])
                except ValueError:
                    pass

        has_lib = "lib.rs" in direct_names or (directory / "lib.rs").exists()
        has_main = "main.rs" in direct_names or (directory / "main.rs").exists()

        if has_lib and not has_main:
            entry.unit_kind = "rust-library"
            entry.stage = "foundation"
        elif has_main or "bin" in parts:
            entry.unit_kind = "rust-binary"
            entry.stage = "application"
            if src_unit and src_unit in self.entries and src_unit != entry.path:
                entry.depends_on = [src_unit]
        else:
            entry.unit_kind = "rust-module"
            entry.stage = "foundation"
            if src_unit and src_unit in self.entries and src_unit != entry.path:
                entry.depends_on = [src_unit]

        return True

    def _infer_makefile_work_unit(self, entry: DirectoryEntry) -> None:
        """Infer FreeBSD/Makefile-oriented rewrite-unit metadata."""
        directory = self.source_root / entry.path
        has_build_makefile = (
            (directory / "Makefile").exists()
            or (directory / "BSDmakefile").exists()
        )
        top = entry.path.split("/", 1)[0]

        if top in {"include", "lib"}:
            entry.stage = "foundation"
            entry.unit_kind = "freebsd-library"
        elif top in {"tools", "targets"}:
            entry.stage = "bootstrap"
            entry.unit_kind = "bootstrap-tool"
        elif top in {"bin", "sbin", "usr.bin", "usr.sbin", "libexec"}:
            entry.stage = "application"
            entry.unit_kind = "freebsd-command"
        elif top == "tests":
            entry.stage = "validation"
            entry.unit_kind = "freebsd-tests"
        elif top == "sys":
            entry.stage = "kernel"
            entry.unit_kind = "freebsd-kernel"
        else:
            entry.stage = "integration"
            entry.unit_kind = "makefile-module" if has_build_makefile else "directory"

        if has_build_makefile:
            entry.build_command = f"make -C {shlex.quote(entry.path)}"

    def _find_cargo_root(self, directory: Path) -> Optional[Path]:
        """Find the nearest Cargo.toml at or above a directory."""
        current = directory
        while True:
            try:
                current.relative_to(self.source_root)
            except ValueError:
                return None

            if (current / "Cargo.toml").exists():
                return current
            if current == self.source_root:
                return None
            current = current.parent

    def _rust_src_unit_for(self, cargo_root: Path) -> Optional[str]:
        """Return the relative src unit path for a Cargo package if it exists."""
        src_dir = cargo_root / "src"
        if not src_dir.exists():
            return None
        return self._rel_or_dot(src_dir)

    def _cargo_test_command(self, cargo_root: Path, package_name: Optional[str]) -> str:
        """Build a Cargo command that validates a package-sized rewrite unit."""
        if cargo_root == self.source_root:
            return "cargo test"

        root_manifest = self.source_root / "Cargo.toml"
        if package_name and root_manifest.exists() and self._cargo_has_workspace(root_manifest):
            return f"cargo test -p {shlex.quote(package_name)}"

        manifest_rel = self._rel_or_dot(cargo_root / "Cargo.toml")
        return f"cargo test --manifest-path {shlex.quote(manifest_rel)}"

    @staticmethod
    def _read_cargo_package_name(manifest: Path) -> Optional[str]:
        """Extract package.name from Cargo.toml without requiring TOML dependencies."""
        try:
            text = manifest.read_text(encoding="utf-8")
        except OSError:
            return None

        in_package = False
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith("]"):
                in_package = line == "[package]"
                continue
            if in_package and line.startswith("name"):
                match = re.match(r'name\s*=\s*["\']([^"\']+)["\']', line)
                if match:
                    return match.group(1)
        return None

    @staticmethod
    def _cargo_has_workspace(manifest: Path) -> bool:
        try:
            text = manifest.read_text(encoding="utf-8")
        except OSError:
            return False
        return bool(re.search(r"^\s*\[workspace\]\s*$", text, flags=re.MULTILINE))

    def _rel_or_dot(self, path: Path) -> str:
        rel = path.relative_to(self.source_root)
        return "." if str(rel) == "." else rel.as_posix()

    @staticmethod
    def _unique_paths(paths: List[str]) -> List[str]:
        seen = set()
        result = []
        for path in paths:
            if not path or path in seen:
                continue
            seen.add(path)
            result.append(path)
        return result

    def _ordered_entries(self) -> DirectoryEntryMap:
        """Return entries in the order the workflow should process them."""
        if self.workflow_mode != "rewrite":
            return DirectoryEntryMap(dict(sorted(self.entries.items())))

        remaining = dict(self.entries)
        emitted = set()
        ordered: Dict[str, DirectoryEntry] = {}

        while remaining:
            ready = [
                (path, entry)
                for path, entry in remaining.items()
                if all(dep not in self.entries or dep in emitted for dep in entry.depends_on)
            ]
            if not ready:
                ready = list(remaining.items())

            path, entry = min(
                ready,
                key=lambda item: (
                    REWRITE_STAGE_ORDER.get(item[1].stage, REWRITE_STAGE_ORDER["unknown"]),
                    item[0],
                ),
            )
            ordered[path] = entry
            emitted.add(path)
            del remaining[path]

        return DirectoryEntryMap(ordered)
    
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
        last_entry = None
        for line in content.split('\n'):
            stripped = line.strip()
            entry = self._parse_entry_line(stripped)
            if entry:
                self.entries[entry.path] = entry
                last_entry = entry
                continue

            if last_entry:
                metadata = self._parse_unit_metadata_line(stripped)
                if metadata:
                    self._apply_unit_metadata(last_entry, metadata)

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

    def _parse_unit_metadata_line(self, line: str) -> Optional[Dict[str, Any]]:
        match = UNIT_METADATA_PATTERN.match(line)
        if not match:
            return None
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def _apply_unit_metadata(self, entry: DirectoryEntry, data: Dict[str, Any]) -> None:
        entry.unit_kind = str(data.get("kind") or entry.unit_kind)
        entry.stage = str(data.get("stage") or entry.stage)
        entry.depends_on = self._string_list(data.get("depends_on"))
        entry.files = self._string_list(data.get("files"))
        entry.build_command = self._optional_string(data.get("build_command"))
        entry.test_command = self._optional_string(data.get("test_command"))
        entry.install_command = self._optional_string(data.get("install_command"))

    @staticmethod
    def _string_list(value: Any) -> List[str]:
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if item]

    @staticmethod
    def _optional_string(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None
    
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
        noun = self.workflow["noun"]
        verb = self.workflow["verb"]
        gerund = self.workflow["gerund"]
        done = self.workflow["done"]
        return [
            f"# {self.workflow['title']}",
            "",
            f"This file tracks {noun} progress across the source tree.",
            "**DO NOT EDIT MANUALLY** - Updated automatically by the review tool.",
            "",
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "",
            "## Status Legend",
            f"- `[ ]` Pending - needs {verb}",
            f"- `[>]` Current - being {gerund} now",
            f"- `[x]` Done - {done} and committed",
            "- `[-]` Skipped - no changes needed or deferred",
            "",
        ]

    def _format_statistics(self) -> List[str]:
        """Format the progress statistics section."""
        total = len(self.entries)
        done = sum(1 for e in self.entries.values() if e.status == Status.DONE)
        skipped = sum(1 for e in self.entries.values() if e.status == Status.SKIPPED)
        pending = total - done - skipped
        unit_label = "work units" if self.workflow_mode == "rewrite" else "directories"

        return [
            "## Progress",
            f"- Total {unit_label}: {total}",
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
        if self.workflow_mode == "rewrite":
            return self._format_rewrite_work_unit_groups()

        lines = []
        grouped_paths = set()
        for top_dir in self.TOP_DIRS:
            group = {k: v for k, v in self.entries.items()
                     if k.startswith(f"{top_dir}/")}
            grouped_paths.update(group)
            group_lines = self._format_directory_group(f"{top_dir}/", group)
            lines.extend(group_lines)

        remaining = {k: v for k, v in self.entries.items()
                     if k not in grouped_paths}
        lines.extend(self._format_directory_group("Project", remaining))
        return lines

    def _format_directory_group(self, title: str, group: Dict[str, DirectoryEntry]) -> List[str]:
        """Format a single directory group section."""
        if not group:
            return []

        group_done = sum(1 for e in group.values() if e.status == Status.DONE)
        lines = [
            f"## {title} ({group_done}/{len(group)} done)",
            "",
        ]

        for path, entry in sorted(group.items()):
            lines.extend(self._format_entry_block(entry))

        lines.append("")
        return lines

    def _format_rewrite_work_unit_groups(self) -> List[str]:
        """Format rewrite entries by inferred bottom-up stages."""
        lines = []
        stages = sorted(
            {entry.stage for entry in self.entries.values()},
            key=lambda stage: (REWRITE_STAGE_ORDER.get(stage, REWRITE_STAGE_ORDER["unknown"]), stage),
        )

        for stage in stages:
            group = {k: v for k, v in self.entries.items() if v.stage == stage}
            group_done = sum(1 for e in group.values() if e.status == Status.DONE)
            lines.extend([
                f"## Stage: {stage} ({group_done}/{len(group)} done)",
                "",
            ])
            for entry in group.values():
                lines.extend(self._format_entry_block(entry))
            lines.append("")

        return lines

    def _format_entry_block(self, entry: DirectoryEntry) -> List[str]:
        lines = [self._format_entry_line(entry)]
        if self.workflow_mode == "rewrite":
            metadata = json.dumps(self._unit_metadata(entry), sort_keys=True)
            lines.append(f"  <!-- unit: {metadata} -->")
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

    def _unit_metadata(self, entry: DirectoryEntry) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "kind": entry.unit_kind,
            "stage": entry.stage,
        }
        if entry.depends_on:
            data["depends_on"] = entry.depends_on
        if entry.files:
            data["files"] = entry.files
        if entry.build_command:
            data["build_command"] = entry.build_command
        if entry.test_command:
            data["test_command"] = entry.test_command
        if entry.install_command:
            data["install_command"] = entry.install_command
        return data

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
    
    def get_next_pending(
        self,
        selection_policy: Optional[str] = None,
        required_source_suffixes: Any = None,
    ) -> Optional[str]:
        """Get the next pending directory for this workflow."""
        suffixes = normalize_rewrite_source_suffixes(required_source_suffixes)
        if self.workflow_mode == "rewrite":
            policy = normalize_rewrite_selection_policy(selection_policy)
            if policy == "small_first":
                return self._get_next_pending_small_first(suffixes)

        for path, entry in self.entries.items():
            if entry.status == Status.PENDING and self._matches_required_suffixes(entry, suffixes):
                return path
        return None

    def _get_next_pending_small_first(
        self,
        required_source_suffixes: Optional[Set[str]] = None,
    ) -> Optional[str]:
        """Prefer quick, buildable rewrite units while respecting dependencies."""
        pending = [
            (path, entry)
            for path, entry in self.entries.items()
            if entry.status == Status.PENDING
            and self._matches_required_suffixes(entry, required_source_suffixes)
        ]
        if not pending:
            return None

        complete_statuses = {Status.DONE, Status.SKIPPED}
        ready = [
            (path, entry)
            for path, entry in pending
            if all(
                dep not in self.entries
                or self.entries[dep].status in complete_statuses
                for dep in entry.depends_on
            )
        ]
        candidates = ready or pending
        path, _ = min(candidates, key=self._small_first_selection_key)
        return path

    @staticmethod
    def _small_first_selection_key(item: Any) -> tuple:
        path, entry = item
        return (
            0 if entry.build_command else 1,
            0 if ReviewIndex._has_rewrite_implementation_source(entry) else 1,
            REWRITE_SMALL_FIRST_KIND_ORDER.get(entry.unit_kind, 50),
            entry.total_lines,
            len(entry.files),
            REWRITE_STAGE_ORDER.get(entry.stage, REWRITE_STAGE_ORDER["unknown"]),
            path,
        )

    @staticmethod
    def _matches_required_suffixes(
        entry: DirectoryEntry,
        required_source_suffixes: Optional[Set[str]] = None,
    ) -> bool:
        if required_source_suffixes is None:
            return True
        return ReviewIndex._has_rewrite_implementation_source(entry, required_source_suffixes)

    @staticmethod
    def _has_rewrite_implementation_source(
        entry: DirectoryEntry,
        required_source_suffixes: Optional[Set[str]] = None,
    ) -> bool:
        """Return True when a rewrite unit contains real implementation source."""
        if required_source_suffixes is None and entry.c_files > 0:
            return True

        suffixes = required_source_suffixes or REWRITE_IMPLEMENTATION_SUFFIXES
        for file_path in entry.files:
            suffix = Path(file_path).suffix.lower()
            if suffix in suffixes:
                return True

        return False

    def get_current(self) -> Optional[str]:
        """Get the directory currently being worked."""
        for path, entry in self.entries.items():
            if entry.status == Status.CURRENT:
                return path
        return self.current_position

    def set_current(self, path: str) -> None:
        """Set a directory as currently being worked."""
        # Clear any existing current
        for entry in self.entries.values():
            if entry.status == Status.CURRENT:
                entry.status = Status.PENDING

        if path in self.entries:
            self.entries[path].status = Status.CURRENT
        self.current_position = path

    def mark_done(
        self,
        path: str,
        notes: str = "",
        selection_policy: Optional[str] = None,
    ) -> None:
        """Mark a directory as completed."""
        if path in self.entries:
            self.entries[path].status = Status.DONE
            self.entries[path].reviewed_date = datetime.now().strftime('%Y-%m-%d')
            if notes:
                self.entries[path].notes = notes

        # Move current position to next
        self.current_position = self.get_next_pending(selection_policy=selection_policy)

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
        unit_label = "work units" if self.workflow_mode == "rewrite" else "directories"

        lines = [
            f"=== {self.workflow['summary_heading']} ===",
            f"Progress: {done}/{total} {unit_label} completed ({100*done//total if total else 0}%)",
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
                entry = self.entries[p]
                if self.workflow_mode == "rewrite":
                    lines.append(f"  → {p} [{entry.stage}/{entry.unit_kind}]")
                else:
                    lines.append(f"  → {p}")

        return '\n'.join(lines)


def generate_index(
    source_root: Path,
    force_rebuild: bool = False,
    workflow_mode: str = "review",
) -> ReviewIndex:
    """Generate or load the workflow index."""
    index = ReviewIndex(source_root, workflow_mode=workflow_mode)
    if index.index_path.exists() and not force_rebuild:
        try:
            index._load()
            if index.ensure_rewrite_work_units():
                index.save()
            print(f"*** Using existing {index.workflow['noun']} index at {index.index_path}")
            return index
        except Exception as exc:
            print(f"WARNING: Failed to load {index.workflow['noun']} index ({exc}); regenerating")
    index.generate()
    index.save()
    return index


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        source_root = Path(sys.argv[1])
    else:
        source_root = Path(__file__).parent.parent
    workflow_mode = sys.argv[2] if len(sys.argv) > 2 else "review"
    
    print(f"Generating {workflow_mode} index for: {source_root}")
    index = generate_index(source_root, force_rebuild=True, workflow_mode=workflow_mode)
    unit_label = "work units" if index.workflow_mode == "rewrite" else "directories"
    print(f"Found {len(index.entries)} {unit_label}")
    print(f"Index saved to: {index.index_path}")
    print()
    print(index.get_summary_for_ai())
