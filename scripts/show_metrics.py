#!/usr/bin/env python3
"""
Show persona effectiveness metrics.

Usage:
    python scripts/show_metrics.py [source_root] [persona_name]
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from persona_metrics import PersonaMetricsTracker


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/show_metrics.py [source_root] [persona_name]")
        print("\nIf no arguments provided, will use current directory as source_root")
        source_root = Path.cwd()
    else:
        source_root = Path(sys.argv[1])

    metrics_dir = source_root / ".ai-code-reviewer" / "metrics"

    if not metrics_dir.exists():
        print(f"No metrics found at: {metrics_dir}")
        print("Run a review session first to generate metrics.")
        sys.exit(1)

    tracker = PersonaMetricsTracker(metrics_dir)

    # Get all sessions
    all_sessions = tracker.get_all_sessions()

    if not all_sessions:
        print(f"No sessions found in {metrics_dir}")
        sys.exit(0)

    # Get unique personas
    persona_names = sorted(set(s.persona_name for s in all_sessions))

    print(f"Found {len(all_sessions)} sessions for {len(persona_names)} persona(s)\n")

    # If persona specified, show only that one
    if len(sys.argv) >= 3:
        persona_names = [sys.argv[2]]

    for persona_name in persona_names:
        print("=" * 70)
        stats = tracker.get_aggregate_stats(persona_name)

        print(f"Persona: {stats['persona_name']}")
        print(f"Total sessions: {stats['total_sessions']}")
        print()
        print(f"Review Progress:")
        print(f"  • Directories reviewed: {stats['total_directories_reviewed']}")
        print(f"  • Files reviewed: {stats['total_files_reviewed']}")
        print()
        print(f"Build Statistics:")
        print(f"  • Total builds: {stats['total_builds']}")
        print(f"  • Success rate: {stats['build_success_rate']:.1%}")
        print()
        print(f"Edit Statistics:")
        print(f"  • Total edits: {stats['total_edits']}")
        print(f"  • Success rate: {stats['edit_success_rate']:.1%}")
        print()
        print(f"Learning:")
        print(f"  • Lessons learned: {stats['total_lessons_learned']}")
        print()
        print(f"Effectiveness Score: {stats['avg_effectiveness_score']:.1f}/100")
        print()

        # Show individual sessions
        if len(stats['sessions']) <= 5:
            print("Sessions:")
            for session_id in stats['sessions']:
                session = tracker.load_session(session_id)
                if session:
                    print(f"  • {session_id}: {session.directories_reviewed} dirs, "
                          f"score: {session.get_effectiveness_score():.1f}/100")
        else:
            print(f"Sessions: {len(stats['sessions'])} (use session ID to view details)")

        print("=" * 70)
        print()


if __name__ == "__main__":
    main()
