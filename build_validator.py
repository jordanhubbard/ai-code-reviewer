#!/usr/bin/env python3
"""
Build Command Validator for AI Code Reviewer

Validates that the build command is appropriate for the detected project type
and provides suggestions for common project structures.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)


# Build command templates for common project types
BUILD_COMMAND_TEMPLATES = {
    "freebsd": {
        "command": "sudo make -j$(sysctl -n hw.ncpu) buildworld",
        "description": "FreeBSD buildworld",
        "pre_build": "sudo -v",
    },
    "freebsd-kernel": {
        "command": "sudo make -j$(sysctl -n hw.ncpu) buildkernel KERNCONF=GENERIC",
        "description": "FreeBSD kernel build",
        "pre_build": "sudo -v",
    },
    "linux-kernel": {
        "command": "make -j$(nproc) bzImage modules",
        "description": "Linux kernel build",
        "pre_build": "",
    },
    "linux-make": {
        "command": "make -j$(nproc)",
        "description": "Linux Makefile project",
        "pre_build": "",
    },
    "cmake": {
        "command": "cmake --build build -j$(nproc)",
        "description": "CMake project",
        "pre_build": "cmake -B build -S .",
    },
    "cmake-test": {
        "command": "cmake --build build -j$(nproc) && ctest --test-dir build",
        "description": "CMake with tests",
        "pre_build": "cmake -B build -S .",
    },
    "autotools": {
        "command": "make -j$(nproc)",
        "description": "Autotools project",
        "pre_build": "./configure",
    },
    "rust": {
        "command": "cargo build --release",
        "description": "Rust project",
        "pre_build": "",
    },
    "rust-test": {
        "command": "cargo build --release && cargo test",
        "description": "Rust with tests",
        "pre_build": "",
    },
    "go": {
        "command": "go build ./...",
        "description": "Go project",
        "pre_build": "",
    },
    "go-test": {
        "command": "go build ./... && go test ./...",
        "description": "Go with tests",
        "pre_build": "",
    },
    "python-pytest": {
        "command": "python -m pytest tests/",
        "description": "Python with pytest",
        "pre_build": "",
    },
    "python-unittest": {
        "command": "python -m unittest discover",
        "description": "Python with unittest",
        "pre_build": "",
    },
    "python-tox": {
        "command": "tox",
        "description": "Python with tox",
        "pre_build": "",
    },
    "node-npm": {
        "command": "npm test",
        "description": "Node.js with npm",
        "pre_build": "npm install",
    },
    "node-yarn": {
        "command": "yarn test",
        "description": "Node.js with yarn",
        "pre_build": "yarn install",
    },
}


@dataclass
class ProjectDetection:
    """Result of project type detection."""
    project_type: str
    confidence: str  # 'high', 'medium', 'low'
    detected_files: List[str]
    suggested_commands: List[Dict[str, str]]

    def get_primary_suggestion(self) -> Optional[Dict[str, str]]:
        """Get the most recommended build command."""
        return self.suggested_commands[0] if self.suggested_commands else None


@dataclass
class BuildValidation:
    """Result of build command validation."""
    is_valid: bool
    warnings: List[str]
    suggestions: List[str]
    detected_project: Optional[ProjectDetection] = None


class BuildValidator:
    """Validates build commands and suggests appropriate commands."""

    @classmethod
    def detect_project_type(cls, source_root: Path) -> Optional[ProjectDetection]:
        """
        Detect the project type based on files present in source_root.

        Args:
            source_root: Path to source code root

        Returns:
            ProjectDetection or None if type cannot be determined
        """
        detected_files = []
        suggested_commands = []
        project_type = None
        confidence = "low"

        # Check for Rust
        if (source_root / "Cargo.toml").exists():
            detected_files.append("Cargo.toml")
            project_type = "rust"
            confidence = "high"
            suggested_commands.append(BUILD_COMMAND_TEMPLATES["rust"])
            suggested_commands.append(BUILD_COMMAND_TEMPLATES["rust-test"])

        # Check for Go
        elif (source_root / "go.mod").exists():
            detected_files.append("go.mod")
            project_type = "go"
            confidence = "high"
            suggested_commands.append(BUILD_COMMAND_TEMPLATES["go"])
            suggested_commands.append(BUILD_COMMAND_TEMPLATES["go-test"])

        # Check for Node.js
        elif (source_root / "package.json").exists():
            detected_files.append("package.json")
            project_type = "node"
            confidence = "high"
            if (source_root / "yarn.lock").exists():
                suggested_commands.append(BUILD_COMMAND_TEMPLATES["node-yarn"])
            else:
                suggested_commands.append(BUILD_COMMAND_TEMPLATES["node-npm"])

        # Check for CMake
        elif (source_root / "CMakeLists.txt").exists():
            detected_files.append("CMakeLists.txt")
            project_type = "cmake"
            confidence = "high"
            suggested_commands.append(BUILD_COMMAND_TEMPLATES["cmake"])
            suggested_commands.append(BUILD_COMMAND_TEMPLATES["cmake-test"])

        # Check for Autotools
        elif (source_root / "configure.ac").exists() or (source_root / "configure").exists():
            if (source_root / "configure.ac").exists():
                detected_files.append("configure.ac")
            if (source_root / "configure").exists():
                detected_files.append("configure")
            project_type = "autotools"
            confidence = "high"
            suggested_commands.append(BUILD_COMMAND_TEMPLATES["autotools"])

        # Check for FreeBSD source tree
        elif (source_root / "Makefile").exists() and (source_root / "sys").exists() and (source_root / "bin").exists():
            detected_files.extend(["Makefile", "sys/", "bin/"])
            project_type = "freebsd"
            confidence = "high"
            suggested_commands.append(BUILD_COMMAND_TEMPLATES["freebsd"])
            suggested_commands.append(BUILD_COMMAND_TEMPLATES["freebsd-kernel"])

        # Check for Linux kernel
        elif (source_root / "Makefile").exists() and (source_root / "Kconfig").exists():
            detected_files.extend(["Makefile", "Kconfig"])
            project_type = "linux-kernel"
            confidence = "high"
            suggested_commands.append(BUILD_COMMAND_TEMPLATES["linux-kernel"])

        # Check for generic Makefile
        elif (source_root / "Makefile").exists():
            detected_files.append("Makefile")
            project_type = "make"
            confidence = "medium"
            suggested_commands.append(BUILD_COMMAND_TEMPLATES["linux-make"])

        # Check for Python
        elif (source_root / "setup.py").exists() or (source_root / "pyproject.toml").exists():
            if (source_root / "setup.py").exists():
                detected_files.append("setup.py")
            if (source_root / "pyproject.toml").exists():
                detected_files.append("pyproject.toml")
            project_type = "python"
            confidence = "medium"

            # Check which test framework
            if (source_root / "tox.ini").exists():
                suggested_commands.append(BUILD_COMMAND_TEMPLATES["python-tox"])
            elif (source_root / "pytest.ini").exists() or (source_root / "tests").exists():
                suggested_commands.append(BUILD_COMMAND_TEMPLATES["python-pytest"])
            else:
                suggested_commands.append(BUILD_COMMAND_TEMPLATES["python-unittest"])

        if project_type:
            return ProjectDetection(
                project_type=project_type,
                confidence=confidence,
                detected_files=detected_files,
                suggested_commands=suggested_commands
            )

        return None

    @classmethod
    def validate_build_command(
        cls,
        build_command: str,
        source_root: Path
    ) -> BuildValidation:
        """
        Validate that the build command is appropriate for the project.

        Args:
            build_command: Configured build command
            source_root: Path to source code root

        Returns:
            BuildValidation with warnings and suggestions
        """
        warnings = []
        suggestions = []

        # Detect project type
        detected = cls.detect_project_type(source_root)

        if not detected:
            warnings.append("Could not detect project type - cannot validate build command")
            return BuildValidation(
                is_valid=True,  # Can't validate, so assume valid
                warnings=warnings,
                suggestions=suggestions,
                detected_project=None
            )

        # Check if build command matches project type
        command_lower = build_command.lower()
        project_type = detected.project_type

        # Rust project should use cargo
        if project_type == "rust" and "cargo" not in command_lower:
            warnings.append(f"Detected Rust project but build_command doesn't use 'cargo'")
            primary = detected.get_primary_suggestion()
            if primary:
                suggestions.append(f"Try: {primary['command']}")

        # Go project should use go
        elif project_type == "go" and "go " not in command_lower:
            warnings.append(f"Detected Go project but build_command doesn't use 'go'")
            primary = detected.get_primary_suggestion()
            if primary:
                suggestions.append(f"Try: {primary['command']}")

        # CMake project should use cmake
        elif project_type == "cmake" and "cmake" not in command_lower:
            warnings.append(f"Detected CMake project but build_command doesn't use 'cmake'")
            primary = detected.get_primary_suggestion()
            if primary:
                suggestions.append(f"Try: {primary['command']}")

        # Node.js should use npm/yarn
        elif project_type == "node" and not any(x in command_lower for x in ["npm", "yarn"]):
            warnings.append(f"Detected Node.js project but build_command doesn't use 'npm' or 'yarn'")
            primary = detected.get_primary_suggestion()
            if primary:
                suggestions.append(f"Try: {primary['command']}")

        # Python should use pytest/unittest/tox
        elif project_type == "python" and not any(x in command_lower for x in ["pytest", "unittest", "tox", "python"]):
            warnings.append(f"Detected Python project but build_command doesn't use test framework")
            suggestions.extend([cmd['command'] for cmd in detected.suggested_commands])

        # FreeBSD should use make buildworld/buildkernel
        elif project_type == "freebsd" and "buildworld" not in command_lower and "buildkernel" not in command_lower:
            warnings.append(f"Detected FreeBSD source tree but build_command doesn't use 'buildworld' or 'buildkernel'")
            suggestions.extend([cmd['command'] for cmd in detected.suggested_commands])

        is_valid = len(warnings) == 0

        return BuildValidation(
            is_valid=is_valid,
            warnings=warnings,
            suggestions=suggestions,
            detected_project=detected
        )

    @classmethod
    def get_suggestion_for_project(cls, source_root: Path) -> Optional[str]:
        """
        Get a suggested build command for the project.

        Args:
            source_root: Path to source code root

        Returns:
            Suggested build command or None
        """
        detected = cls.detect_project_type(source_root)
        if detected:
            primary = detected.get_primary_suggestion()
            if primary:
                return primary['command']
        return None


if __name__ == "__main__":
    # Self-test
    import sys

    if len(sys.argv) < 2:
        print("Usage: python build_validator.py <source_root> [build_command]")
        sys.exit(1)

    source_root = Path(sys.argv[1])

    # Detect project type
    detected = BuildValidator.detect_project_type(source_root)
    if detected:
        print(f"Detected project type: {detected.project_type} (confidence: {detected.confidence})")
        print(f"Based on files: {', '.join(detected.detected_files)}")
        print("\nSuggested build commands:")
        for cmd in detected.suggested_commands:
            print(f"  • {cmd['command']}")
            print(f"    ({cmd['description']})")
    else:
        print("Could not detect project type")

    # Validate build command if provided
    if len(sys.argv) >= 3:
        build_command = sys.argv[2]
        validation = BuildValidator.validate_build_command(build_command, source_root)

        print(f"\nBuild command validation:")
        print(f"  Command: {build_command}")
        print(f"  Valid: {validation.is_valid}")

        if validation.warnings:
            print("\n  Warnings:")
            for warning in validation.warnings:
                print(f"    • {warning}")

        if validation.suggestions:
            print("\n  Suggestions:")
            for suggestion in validation.suggestions:
                print(f"    • {suggestion}")
