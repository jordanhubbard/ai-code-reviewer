#!/usr/bin/env python3
"""
Persona Effectiveness Metrics for AI Code Reviewer

Tracks and measures how effective each persona is at finding and fixing issues.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
import json
import logging

logger = logging.getLogger(__name__)


@dataclass
class PersonaMetrics:
    """Tracks persona effectiveness over time."""
    persona_name: str
    session_id: str
    start_time: str = field(default_factory=lambda: datetime.now().isoformat())

    # Review statistics
    directories_reviewed: int = 0
    files_reviewed: int = 0
    total_iterations: int = 0

    # Edit statistics
    edits_made: int = 0
    successful_edits: int = 0  # Edits that didn't break the build
    failed_edits: int = 0  # Edits that broke the build

    # Build statistics
    builds_run: int = 0
    builds_succeeded: int = 0
    builds_failed: int = 0

    # Learning statistics
    lessons_learned: int = 0  # Entries added to LESSONS.md
    loop_detections: int = 0  # Times loop was detected
    recoveries: int = 0  # Times AI recovered from error

    # Time statistics
    total_time_seconds: float = 0.0
    avg_iterations_per_directory: float = 0.0

    def update_from_session(self, session: 'ReviewSession') -> None:
        """Update metrics from a ReviewSession."""
        self.directories_reviewed = session.directories_completed
        self.files_reviewed = session.files_fixed

        # Calculate average iterations
        if self.directories_reviewed > 0:
            self.avg_iterations_per_directory = (
                self.total_iterations / self.directories_reviewed
            )

    def record_build(self, success: bool) -> None:
        """Record a build attempt."""
        self.builds_run += 1
        if success:
            self.builds_succeeded += 1
        else:
            self.builds_failed += 1

    def record_edit(self, caused_build_failure: bool = False) -> None:
        """Record an edit made by the AI."""
        self.edits_made += 1
        if caused_build_failure:
            self.failed_edits += 1
        else:
            self.successful_edits += 1

    def record_lesson(self) -> None:
        """Record a lesson learned."""
        self.lessons_learned += 1

    def record_loop_detection(self) -> None:
        """Record a loop detection."""
        self.loop_detections += 1

    def record_recovery(self) -> None:
        """Record a successful recovery from error."""
        self.recoveries += 1

    def get_effectiveness_score(self) -> float:
        """
        Calculate persona effectiveness score (0-100).

        Higher is better. Considers:
        - Build success rate (positive)
        - Edit success rate (positive)
        - Lessons learned (indicates improvement, positive if moderate)
        - Loop detections (negative, indicates confusion)
        """
        if self.builds_run == 0:
            return 0.0

        # Build success rate (0-1)
        build_success_rate = self.builds_succeeded / self.builds_run

        # Edit success rate (0-1)
        edit_success_rate = 1.0
        if self.edits_made > 0:
            edit_success_rate = self.successful_edits / self.edits_made

        # Lessons penalty (too many lessons means lots of mistakes)
        # Moderate lessons (1-10) is good, more than that is concerning
        lessons_factor = 1.0
        if self.lessons_learned > 10:
            lessons_factor = max(0.5, 1.0 - (self.lessons_learned - 10) * 0.02)

        # Loop detection penalty
        loop_penalty = min(0.3, self.loop_detections * 0.05)

        # Recovery bonus (shows adaptability)
        recovery_bonus = min(0.1, self.recoveries * 0.02)

        # Weighted score
        score = (
            build_success_rate * 0.5 +
            edit_success_rate * 0.3 +
            lessons_factor * 0.2 -
            loop_penalty +
            recovery_bonus
        )

        return max(0.0, min(100.0, score * 100))

    def get_summary(self) -> str:
        """Get a human-readable summary."""
        lines = [
            f"Persona: {self.persona_name}",
            f"Session: {self.session_id}",
            f"",
            f"Review Progress:",
            f"  • Directories reviewed: {self.directories_reviewed}",
            f"  • Files reviewed: {self.files_reviewed}",
            f"  • Avg iterations/directory: {self.avg_iterations_per_directory:.1f}",
            f"",
            f"Build Statistics:",
            f"  • Builds run: {self.builds_run}",
            f"  • Success rate: {self.builds_succeeded}/{self.builds_run} ({self._percent(self.builds_succeeded, self.builds_run)})",
            f"",
            f"Edit Statistics:",
            f"  • Edits made: {self.edits_made}",
            f"  • Success rate: {self.successful_edits}/{self.edits_made} ({self._percent(self.successful_edits, self.edits_made)})",
            f"",
            f"Learning:",
            f"  • Lessons learned: {self.lessons_learned}",
            f"  • Recoveries: {self.recoveries}",
            f"  • Loop detections: {self.loop_detections}",
            f"",
            f"Effectiveness Score: {self.get_effectiveness_score():.1f}/100",
        ]
        return "\n".join(lines)

    @staticmethod
    def _percent(numerator: int, denominator: int) -> str:
        """Format as percentage."""
        if denominator == 0:
            return "N/A"
        return f"{(numerator / denominator) * 100:.1f}%"

    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> 'PersonaMetrics':
        """Create from dictionary."""
        return cls(**data)


class PersonaMetricsTracker:
    """Manages persona metrics persistence and retrieval."""

    def __init__(self, metrics_dir: Path):
        """
        Initialize metrics tracker.

        Args:
            metrics_dir: Directory to store metrics files
        """
        self.metrics_dir = metrics_dir
        self.metrics_dir.mkdir(parents=True, exist_ok=True)
        self.current_metrics: Optional[PersonaMetrics] = None

    def start_session(self, persona_name: str, session_id: str) -> PersonaMetrics:
        """
        Start tracking a new session.

        Args:
            persona_name: Name of the persona
            session_id: Unique session identifier

        Returns:
            New PersonaMetrics instance
        """
        self.current_metrics = PersonaMetrics(
            persona_name=persona_name,
            session_id=session_id
        )
        return self.current_metrics

    def save_session(self) -> None:
        """Save current metrics to disk."""
        if not self.current_metrics:
            return

        # Save to session-specific file
        filename = f"metrics_{self.current_metrics.session_id}.json"
        filepath = self.metrics_dir / filename

        try:
            with open(filepath, 'w') as f:
                json.dump(self.current_metrics.to_dict(), f, indent=2)
            logger.info(f"Saved metrics to {filepath}")
        except Exception as e:
            logger.error(f"Failed to save metrics: {e}")

    def load_session(self, session_id: str) -> Optional[PersonaMetrics]:
        """
        Load metrics for a specific session.

        Args:
            session_id: Session identifier

        Returns:
            PersonaMetrics or None if not found
        """
        filename = f"metrics_{session_id}.json"
        filepath = self.metrics_dir / filename

        if not filepath.exists():
            return None

        try:
            with open(filepath, 'r') as f:
                data = json.load(f)
            return PersonaMetrics.from_dict(data)
        except Exception as e:
            logger.error(f"Failed to load metrics from {filepath}: {e}")
            return None

    def get_all_sessions(self, persona_name: Optional[str] = None) -> List[PersonaMetrics]:
        """
        Get metrics for all sessions, optionally filtered by persona.

        Args:
            persona_name: Optional persona name to filter by

        Returns:
            List of PersonaMetrics
        """
        all_metrics = []

        for filepath in self.metrics_dir.glob("metrics_*.json"):
            try:
                with open(filepath, 'r') as f:
                    data = json.load(f)
                metrics = PersonaMetrics.from_dict(data)

                if persona_name is None or metrics.persona_name == persona_name:
                    all_metrics.append(metrics)
            except Exception as e:
                logger.warning(f"Failed to load {filepath}: {e}")

        return all_metrics

    def get_aggregate_stats(self, persona_name: str) -> Dict:
        """
        Get aggregate statistics for a persona across all sessions.

        Args:
            persona_name: Name of the persona

        Returns:
            Dictionary of aggregate statistics
        """
        sessions = self.get_all_sessions(persona_name=persona_name)

        if not sessions:
            return {
                "persona_name": persona_name,
                "total_sessions": 0,
                "error": "No sessions found"
            }

        total_directories = sum(s.directories_reviewed for s in sessions)
        total_files = sum(s.files_reviewed for s in sessions)
        total_builds = sum(s.builds_run for s in sessions)
        successful_builds = sum(s.builds_succeeded for s in sessions)
        total_edits = sum(s.edits_made for s in sessions)
        successful_edits = sum(s.successful_edits for s in sessions)
        total_lessons = sum(s.lessons_learned for s in sessions)

        avg_effectiveness = sum(s.get_effectiveness_score() for s in sessions) / len(sessions)

        return {
            "persona_name": persona_name,
            "total_sessions": len(sessions),
            "total_directories_reviewed": total_directories,
            "total_files_reviewed": total_files,
            "total_builds": total_builds,
            "build_success_rate": successful_builds / total_builds if total_builds > 0 else 0,
            "total_edits": total_edits,
            "edit_success_rate": successful_edits / total_edits if total_edits > 0 else 0,
            "total_lessons_learned": total_lessons,
            "avg_effectiveness_score": avg_effectiveness,
            "sessions": [s.session_id for s in sessions]
        }


if __name__ == "__main__":
    # Self-test
    import sys

    if len(sys.argv) < 2:
        print("Usage: python persona_metrics.py <metrics_dir>")
        sys.exit(1)

    metrics_dir = Path(sys.argv[1])
    tracker = PersonaMetricsTracker(metrics_dir)

    # Show all personas
    all_sessions = tracker.get_all_sessions()
    persona_names = set(s.persona_name for s in all_sessions)

    print(f"Found {len(all_sessions)} sessions for {len(persona_names)} personas\n")

    for persona_name in sorted(persona_names):
        print(f"=" * 70)
        stats = tracker.get_aggregate_stats(persona_name)
        print(f"Persona: {stats['persona_name']}")
        print(f"Total sessions: {stats['total_sessions']}")
        print(f"Directories reviewed: {stats['total_directories_reviewed']}")
        print(f"Files reviewed: {stats['total_files_reviewed']}")
        print(f"Build success rate: {stats['build_success_rate']:.1%}")
        print(f"Edit success rate: {stats['edit_success_rate']:.1%}")
        print(f"Lessons learned: {stats['total_lessons_learned']}")
        print(f"Avg effectiveness: {stats['avg_effectiveness_score']:.1f}/100")
        print()
