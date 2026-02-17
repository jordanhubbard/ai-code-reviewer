from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from make_run import read_config_values


PENDING_MARKER = "- [ ] `"
CURRENT_MARKER = "- [>] `"


def _resolve_source_root(project_root: Path) -> Path | None:
    config_path = project_root / "config.yaml"
    defaults_path = project_root / "config.yaml.defaults"

    source_root, _ = read_config_values(config_path)
    if source_root is None and defaults_path.exists():
        source_root, _ = read_config_values(defaults_path)

    if not source_root:
        return None

    root_path = Path(source_root).expanduser()
    if not root_path.is_absolute():
        root_path = (project_root / root_path).resolve()
    return root_path


def _index_has_work(source_root: Path) -> bool:
    index_path = source_root / ".ai-code-reviewer" / "REVIEW-INDEX.md"
    if not index_path.exists():
        return True

    try:
        content = index_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return True

    return PENDING_MARKER in content or CURRENT_MARKER in content


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    os.chdir(project_root)

    backoff_seconds = 5
    max_backoff_seconds = 300

    while True:
        try:
            print(f"\n*** make run (session) [{datetime.now().isoformat(timespec='seconds')}]", flush=True)
            proc = subprocess.run(["make", "run"])
        except KeyboardInterrupt:
            return 130

        exit_code = proc.returncode
        if exit_code == 130:
            return 130

        if exit_code != 0:
            print(
                f"*** make run exited with code {exit_code}; "
                f"retrying in {backoff_seconds}s (capped at {max_backoff_seconds}s)",
                flush=True,
            )
            time.sleep(backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2, max_backoff_seconds)
            continue

        backoff_seconds = 5

        source_root = _resolve_source_root(project_root)
        if source_root and not _index_has_work(source_root):
            print("*** REVIEW INDEX COMPLETE: no pending directories remain", flush=True)
            return 0


if __name__ == "__main__":
    sys.exit(main())
