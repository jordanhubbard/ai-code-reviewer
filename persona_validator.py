#!/usr/bin/env python3
"""
Persona Validator for AI Code Reviewer

Validates that persona files are properly structured and contain
required sections for effective code review.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple, Optional
import logging

logger = logging.getLogger(__name__)


@dataclass
class PersonaValidation:
    """Result of persona validation."""
    is_valid: bool
    errors: List[str]
    warnings: List[str]
    persona_name: Optional[str] = None
    focus_areas: List[str] = None

    def __post_init__(self):
        if self.focus_areas is None:
            self.focus_areas = []

    def get_report(self) -> str:
        """Generate a human-readable validation report."""
        lines = []

        if self.is_valid:
            lines.append(f"✓ Persona validated: {self.persona_name or 'unknown'}")
            if self.focus_areas:
                lines.append(f"  Focus areas: {', '.join(self.focus_areas)}")
        else:
            lines.append(f"✗ Persona validation FAILED")

        if self.errors:
            lines.append("\nErrors:")
            for error in self.errors:
                lines.append(f"  • {error}")

        if self.warnings:
            lines.append("\nWarnings:")
            for warning in self.warnings:
                lines.append(f"  • {warning}")

        return "\n".join(lines)


class PersonaValidator:
    """Validates persona directory structure and content."""

    # Required sections in AI_START_HERE.md (flexible matching)
    # We look for these concepts, not exact headers
    REQUIRED_CONCEPTS = {
        "mission": ["mission", "goal", "purpose", "objective", "reviewer"],
        "guidance": ["action", "command", "operation", "tool", "read_file", "edit_file", "build", "instructions"],
        "standards": ["standard", "rule", "guideline", "requirement", "check", "enforce", "verify"],
    }

    # Recommended sections (optional but good to have)
    RECOMMENDED_CONCEPTS = {
        "examples": ["example", "sample", "demonstration"],
        "personality": ["personality", "tone", "style", "character"],
    }

    # Placeholder text that indicates incomplete persona
    PLACEHOLDER_PATTERNS = [
        "TODO",
        "FILL THIS IN",
        "REPLACE THIS",
        "[YOUR TEXT HERE]",
        "TBD",
    ]

    @classmethod
    def validate_persona(cls, persona_dir: Path) -> PersonaValidation:
        """
        Validate a persona directory.

        Args:
            persona_dir: Path to persona directory

        Returns:
            PersonaValidation with results
        """
        errors = []
        warnings = []
        persona_name = persona_dir.name
        focus_areas = []

        # Check directory exists
        if not persona_dir.exists():
            return PersonaValidation(
                is_valid=False,
                errors=[f"Persona directory does not exist: {persona_dir}"],
                warnings=[],
                persona_name=persona_name
            )

        if not persona_dir.is_dir():
            return PersonaValidation(
                is_valid=False,
                errors=[f"Path is not a directory: {persona_dir}"],
                warnings=[],
                persona_name=persona_name
            )

        # Check required files
        bootstrap_file = persona_dir / "AI_START_HERE.md"
        if not bootstrap_file.exists():
            errors.append("Missing required file: AI_START_HERE.md")
            return PersonaValidation(
                is_valid=False,
                errors=errors,
                warnings=warnings,
                persona_name=persona_name
            )

        # Validate bootstrap content
        try:
            bootstrap_content = bootstrap_file.read_text(encoding='utf-8')
        except Exception as e:
            errors.append(f"Cannot read AI_START_HERE.md: {e}")
            return PersonaValidation(
                is_valid=False,
                errors=errors,
                warnings=warnings,
                persona_name=persona_name
            )

        # Check for required concepts (flexible matching)
        content_lower = bootstrap_content.lower()
        missing_concepts = []

        for concept_name, keywords in cls.REQUIRED_CONCEPTS.items():
            if not any(keyword in content_lower for keyword in keywords):
                missing_concepts.append(concept_name)

        if missing_concepts:
            errors.append(f"Missing required concepts: {', '.join(missing_concepts)}")
            errors.append(f"  (Looking for content about: {', '.join(missing_concepts)})")

        # Check for recommended concepts
        missing_recommended = []
        for concept_name, keywords in cls.RECOMMENDED_CONCEPTS.items():
            if not any(keyword in content_lower for keyword in keywords):
                missing_recommended.append(concept_name)

        if missing_recommended:
            warnings.append(f"Missing recommended concepts: {', '.join(missing_recommended)}")

        # Check for placeholder text (but only if it seems unfinished)
        # Count total lines to determine if TODO is part of actual content
        lines = bootstrap_content.split('\n')
        todo_count = sum(1 for line in lines if 'TODO' in line)

        # Only flag as error if there are many TODOs or the file is very short
        # A few TODOs in a long file is okay (work in progress)
        if todo_count > 5 or (todo_count > 2 and len(bootstrap_content) < 1000):
            placeholders_found = [
                pattern for pattern in cls.PLACEHOLDER_PATTERNS
                if pattern in bootstrap_content
            ]
            if placeholders_found:
                errors.append(f"Contains many placeholder markers ({todo_count} found)")
        elif todo_count > 0:
            warnings.append(f"Contains {todo_count} TODO marker(s) - consider completing them")

        # Extract focus areas (look for common keywords)
        content_lower = bootstrap_content.lower()
        if "security" in content_lower:
            focus_areas.append("security")
        if "performance" in content_lower or "optimization" in content_lower:
            focus_areas.append("performance")
        if "style" in content_lower or "formatting" in content_lower:
            focus_areas.append("style")
        if "correctness" in content_lower or "bugs" in content_lower:
            focus_areas.append("correctness")

        # Check minimum content length
        if len(bootstrap_content.strip()) < 500:
            warnings.append("AI_START_HERE.md seems very short (< 500 chars)")

        # Check for PERSONA.md (optional but recommended)
        persona_file = persona_dir / "PERSONA.md"
        if not persona_file.exists():
            warnings.append("Missing PERSONA.md (recommended for documentation)")

        # Validate that standards are actually defined
        if "## STANDARDS TO ENFORCE" in bootstrap_content:
            standards_section = bootstrap_content.split("## STANDARDS TO ENFORCE", 1)[1]
            # Get content until next section or end
            if "##" in standards_section:
                standards_section = standards_section.split("##", 1)[0]

            # Check if standards section is too short or empty
            if len(standards_section.strip()) < 100:
                errors.append("STANDARDS TO ENFORCE section is too short or empty")

        is_valid = len(errors) == 0

        return PersonaValidation(
            is_valid=is_valid,
            errors=errors,
            warnings=warnings,
            persona_name=persona_name,
            focus_areas=focus_areas
        )

    @classmethod
    def validate_and_report(cls, persona_dir: Path) -> Tuple[bool, str]:
        """
        Validate persona and return simple pass/fail with message.

        Args:
            persona_dir: Path to persona directory

        Returns:
            Tuple of (is_valid, message)
        """
        validation = cls.validate_persona(persona_dir)
        return validation.is_valid, validation.get_report()


if __name__ == "__main__":
    # Self-test
    import sys

    if len(sys.argv) < 2:
        print("Usage: python persona_validator.py <persona_dir>")
        sys.exit(1)

    persona_dir = Path(sys.argv[1])
    validation = PersonaValidator.validate_persona(persona_dir)

    print(validation.get_report())
    sys.exit(0 if validation.is_valid else 1)
