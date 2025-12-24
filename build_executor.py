#!/usr/bin/env python3
"""
Build Executor for Angry AI

Handles running FreeBSD build commands and parsing compiler errors.
Designed to run locally on a FreeBSD system.

This module is cross-platform Python but the build commands are FreeBSD-specific.
"""

import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

logger = logging.getLogger(__name__)


@dataclass
class CompilerError:
    """Represents a single compiler error or warning."""
    file_path: str
    line_number: int
    column: Optional[int]
    severity: str  # 'error', 'warning', 'note'
    message: str
    context: Optional[str] = None  # Surrounding source lines if available
    
    def __str__(self) -> str:
        col = f":{self.column}" if self.column else ""
        return f"{self.file_path}:{self.line_number}{col}: {self.severity}: {self.message}"


@dataclass
class BuildResult:
    """Result of a build attempt."""
    success: bool
    return_code: int
    duration_seconds: float
    errors: List[CompilerError] = field(default_factory=list)
    warnings: List[CompilerError] = field(default_factory=list)
    raw_output: str = ""
    truncated: bool = False
    
    @property
    def error_count(self) -> int:
        return len(self.errors)
    
    @property
    def warning_count(self) -> int:
        return len(self.warnings)
    
    def get_summary(self) -> str:
        """Get a brief summary of the build result."""
        if self.success:
            return f"Build succeeded in {self.duration_seconds:.1f}s ({self.warning_count} warnings)"
        else:
            return f"Build FAILED in {self.duration_seconds:.1f}s ({self.error_count} errors, {self.warning_count} warnings)"
    
    def get_error_report(self, max_errors: int = 10) -> str:
        """
        Generate a formatted error report for the AI to analyze.
        
        Args:
            max_errors: Maximum number of errors to include
            
        Returns:
            Formatted error report string
        """
        if self.success:
            return "Build succeeded with no errors.\n"
        
        lines = [
            f"BUILD FAILED: {self.error_count} errors, {self.warning_count} warnings",
            f"Duration: {self.duration_seconds:.1f} seconds",
            "",
            "=" * 60,
            "ERRORS (fix these first):",
            "=" * 60,
            ""
        ]
        
        for i, error in enumerate(self.errors[:max_errors], 1):
            lines.append(f"[{i}] {error}")
            if error.context:
                for ctx_line in error.context.split('\n'):
                    lines.append(f"    {ctx_line}")
            lines.append("")
        
        if len(self.errors) > max_errors:
            lines.append(f"... and {len(self.errors) - max_errors} more errors")
            lines.append("")
        
        if self.warnings:
            lines.extend([
                "=" * 60,
                f"WARNINGS ({len(self.warnings)} total, showing first 5):",
                "=" * 60,
                ""
            ])
            for warning in self.warnings[:5]:
                lines.append(f"  {warning}")
            lines.append("")
        
        return '\n'.join(lines)


class ErrorParser:
    """
    Parses build output to extract structured error information.
    
    Handles output from:
    - GCC/Clang (standard FreeBSD compilers)
    - Makefile syntax errors (unclosed conditionals, missing separators, etc.)
    - Make build failures
    - Linker errors
    """
    
    # Regex patterns for different error formats
    # Clang/GCC: file.c:123:45: error: message
    CLANG_ERROR_RE = re.compile(
        r'^(?P<file>[^:]+):(?P<line>\d+):(?:(?P<col>\d+):)?\s*'
        r'(?P<severity>error|warning|note):\s*(?P<message>.+)$'
    )
    
    # Make errors: make[N]: *** [target] Error N
    MAKE_ERROR_RE = re.compile(
        r'^make\[\d+\]:\s*\*\*\*\s*\[(?P<target>[^\]]+)\]\s*Error\s*(?P<code>\d+)'
    )
    
    # Makefile syntax errors: make[N]: path/to/Makefile:line: message
    MAKEFILE_SYNTAX_RE = re.compile(
        r'^make\[\d+\]:\s*(?P<file>[^:]+):(?P<line>\d+):\s*(?P<message>.+)$'
    )
    
    # Linker errors: ld: error: undefined symbol: xxx
    LINKER_ERROR_RE = re.compile(
        r'^ld:\s*(?P<severity>error|warning):\s*(?P<message>.+)$'
    )
    
    @classmethod
    def parse_output(cls, output: str) -> Tuple[List[CompilerError], List[CompilerError]]:
        """
        Parse build output and extract errors and warnings.
        
        Args:
            output: Raw build output
            
        Returns:
            Tuple of (errors, warnings) lists
        """
        errors = []
        warnings = []
        
        lines = output.split('\n')
        
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            
            # Try Clang/GCC format first (most common)
            match = cls.CLANG_ERROR_RE.match(line)
            if match:
                error = CompilerError(
                    file_path=match.group('file'),
                    line_number=int(match.group('line')),
                    column=int(match.group('col')) if match.group('col') else None,
                    severity=match.group('severity'),
                    message=match.group('message'),
                )
                
                # Try to get context (next few lines might be source snippet)
                context_lines = []
                for j in range(i + 1, min(i + 4, len(lines))):
                    ctx = lines[j]
                    if ctx.strip().startswith('^') or ctx.strip().startswith('|'):
                        context_lines.append(ctx)
                    elif cls.CLANG_ERROR_RE.match(ctx):
                        break
                if context_lines:
                    error.context = '\n'.join(context_lines)
                
                if error.severity == 'error':
                    errors.append(error)
                elif error.severity == 'warning':
                    warnings.append(error)
                continue
            
            # Try Makefile syntax errors
            match = cls.MAKEFILE_SYNTAX_RE.match(line)
            if match:
                error = CompilerError(
                    file_path=match.group('file'),
                    line_number=int(match.group('line')),
                    column=None,
                    severity='error',  # Makefile errors are always fatal
                    message=match.group('message'),
                )
                errors.append(error)
                continue
            
            # Try linker errors
            match = cls.LINKER_ERROR_RE.match(line)
            if match:
                error = CompilerError(
                    file_path="(linker)",
                    line_number=0,
                    column=None,
                    severity=match.group('severity'),
                    message=match.group('message'),
                )
                if error.severity == 'error':
                    errors.append(error)
                else:
                    warnings.append(error)
                continue
        
        return errors, warnings


@dataclass
class BuildConfig:
    """Configuration for the build executor."""
    source_root: Path
    build_command: str
    build_timeout: int = 7200  # 2 hours default
    pre_build_command: str = "sudo -v"  # Run once at startup to cache sudo creds


class BuildExecutor:
    """
    Executes builds and parses results.
    
    Runs locally on the FreeBSD system where the source tree is mounted.
    """
    
    def __init__(self, config: BuildConfig, run_pre_build: bool = True):
        """
        Initialize the build executor.
        
        Args:
            config: BuildConfig with source root and build command
            run_pre_build: If True, run pre_build_command (e.g., sudo -v) on init
        """
        self.config = config
        
        if not config.source_root.is_dir():
            raise ValueError(f"Source root is not a directory: {config.source_root}")
        
        logger.info(f"Build executor initialized: {config.source_root}")
        
        # Run pre-build command (typically sudo -v to cache credentials)
        if run_pre_build and config.pre_build_command:
            self._run_pre_build()
    
    def _run_pre_build(self) -> None:
        """
        Run the pre-build command to set up the environment.
        
        Typically used to cache sudo credentials so the user only needs
        to enter their password once at the start of the session.
        """
        cmd = self.config.pre_build_command
        logger.info(f"Running pre-build command: {cmd}")
        print(f"\n*** Running: {cmd}")
        print("*** You may be prompted for your sudo password.\n")
        
        try:
            # Run interactively (no capture) so user can enter password
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=str(self.config.source_root),
                timeout=60,
            )
            if result.returncode != 0:
                logger.warning(f"Pre-build command exited with code {result.returncode}")
            else:
                logger.info("Pre-build command completed successfully")
        except subprocess.TimeoutExpired:
            logger.warning("Pre-build command timed out")
        except Exception as e:
            logger.warning(f"Pre-build command failed: {e}")
    
    def run_build(self, capture_output: bool = True) -> BuildResult:
        """
        Run the build command and parse results.
        
        IMPORTANT: We chdir to source_root before running make because
        FreeBSD's build system does path detection that won't work
        properly with "cd /path && make" chaining.
        
        Args:
            capture_output: If True, capture stdout/stderr for parsing
            
        Returns:
            BuildResult with parsed errors and warnings
        """
        command = self.config.build_command
        logger.info(f"Running build in {self.config.source_root}: {command}")
        
        start_time = time.time()
        
        try:
            # Run the build command with cwd set to source root
            # This is critical - FreeBSD's make buildworld needs to be
            # run FROM the source directory, not via "cd ... && make"
            result = subprocess.run(
                command,
                shell=True,
                cwd=str(self.config.source_root),
                capture_output=capture_output,
                text=True,
                timeout=self.config.build_timeout,
            )
            
            elapsed = time.time() - start_time
            
            # Combine stdout and stderr
            raw_output = ""
            if capture_output:
                raw_output = (result.stdout or "") + "\n" + (result.stderr or "")
            
            # Truncate if very long
            truncated = False
            if len(raw_output) > 100000:
                raw_output = raw_output[-100000:]  # Keep last 100KB
                truncated = True
            
            # Parse errors and warnings
            errors, warnings = ErrorParser.parse_output(raw_output)
            
            build_result = BuildResult(
                success=(result.returncode == 0),
                return_code=result.returncode,
                duration_seconds=elapsed,
                errors=errors,
                warnings=warnings,
                raw_output=raw_output,
                truncated=truncated,
            )
            
            logger.info(build_result.get_summary())
            return build_result
            
        except subprocess.TimeoutExpired:
            elapsed = time.time() - start_time
            logger.error(f"Build timed out after {elapsed:.1f}s")
            return BuildResult(
                success=False,
                return_code=-1,
                duration_seconds=elapsed,
                errors=[CompilerError(
                    file_path="(build system)",
                    line_number=0,
                    column=None,
                    severity="error",
                    message=f"Build timed out after {self.config.build_timeout} seconds",
                )],
            )
        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"Build failed with exception: {e}")
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
    
    def quick_syntax_check(self, file_path: Path) -> BuildResult:
        """
        Run a quick syntax check on a single file using the compiler.
        
        This is faster than a full build and useful for validating individual edits.
        
        Args:
            file_path: Path to the C file to check
            
        Returns:
            BuildResult with any syntax errors
        """
        if not file_path.exists():
            return BuildResult(
                success=False,
                return_code=-1,
                duration_seconds=0,
                errors=[CompilerError(
                    file_path=str(file_path),
                    line_number=0,
                    column=None,
                    severity="error",
                    message="File does not exist",
                )],
            )
        
        # Use cc (system compiler) with -fsyntax-only
        command = f"cc -fsyntax-only -Wall -Werror {file_path}"
        logger.info(f"Syntax check: {file_path}")
        
        start_time = time.time()
        
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=str(self.config.source_root),
                capture_output=True,
                text=True,
                timeout=60,  # 1 minute timeout for syntax check
            )
            
            elapsed = time.time() - start_time
            raw_output = (result.stdout or "") + "\n" + (result.stderr or "")
            errors, warnings = ErrorParser.parse_output(raw_output)
            
            return BuildResult(
                success=(result.returncode == 0),
                return_code=result.returncode,
                duration_seconds=elapsed,
                errors=errors,
                warnings=warnings,
                raw_output=raw_output,
            )
            
        except subprocess.TimeoutExpired:
            return BuildResult(
                success=False,
                return_code=-1,
                duration_seconds=60,
                errors=[CompilerError(
                    file_path=str(file_path),
                    line_number=0,
                    column=None,
                    severity="error",
                    message="Syntax check timed out",
                )],
            )
        except Exception as e:
            return BuildResult(
                success=False,
                return_code=-1,
                duration_seconds=0,
                errors=[CompilerError(
                    file_path=str(file_path),
                    line_number=0,
                    column=None,
                    severity="error",
                    message=f"Syntax check failed: {e}",
                )],
            )


def create_executor_from_config(config_dict: Dict[str, Any], run_pre_build: bool = True) -> BuildExecutor:
    """
    Create a BuildExecutor from a configuration dictionary.
    
    Args:
        config_dict: Dictionary with 'source' section
        run_pre_build: If True, run pre_build_command on init
        
    Returns:
        Configured BuildExecutor
    """
    # Support both 'build' (new) and 'source' (old) config sections
    build_config = config_dict.get('build', config_dict.get('source', {}))
    
    # Resolve source root relative to config file location
    # Support both 'source_root' (new) and 'root' (old) keys
    source_root_str = build_config.get('source_root', build_config.get('root', '..'))
    source_root = Path(source_root_str)
    
    if not source_root.is_absolute():
        # Default: assume config is in angry-ai/ subdirectory
        source_root = Path(__file__).parent / source_root
    source_root = source_root.resolve()
    
    config = BuildConfig(
        source_root=source_root,
        build_command=build_config.get('build_command', 
            "sudo make -j$(sysctl -n hw.ncpu) buildworld"),
        build_timeout=build_config.get('build_timeout', 7200),
        pre_build_command=build_config.get('pre_build_command', 'sudo -v'),
    )
    
    return BuildExecutor(config, run_pre_build=run_pre_build)


if __name__ == "__main__":
    # Self-test
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    
    print("Build Executor Self-Test")
    print("=" * 40)
    
    # Test error parsing
    test_output = """
/usr/src/bin/pkill/pkill.c:163:14: error: use of undeclared identifier 'INT_MAX'
                    lval > INT_MAX)
                           ^
/usr/src/bin/pkill/pkill.c:297:14: warning: unused variable 'gname' [-Wunused-variable]
        const char *gname;
                     ^~~~~
make[5]: *** [pkill.o] Error 1
"""
    
    errors, warnings = ErrorParser.parse_output(test_output)
    
    print(f"\nParsed {len(errors)} errors and {len(warnings)} warnings from test output:")
    for e in errors:
        print(f"  ERROR: {e}")
    for w in warnings:
        print(f"  WARNING: {w}")
    
    print("\n✓ Error parser working")

