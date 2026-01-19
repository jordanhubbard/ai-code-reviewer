#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
from datetime import datetime
from pathlib import Path
import pty


def parse_value(raw: str) -> str | None:
    value = raw.split('#', 1)[0].strip()
    if not value:
        return None
    if value[0] in ('"', "'") and value[-1] == value[0]:
        value = value[1:-1]
    return value or None


def read_config_values(config_path: Path) -> tuple[str | None, str | None]:
    if not config_path.exists():
        return None, None
    source_root = None
    ops_log_dir = None
    current_section = None
    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if not line.startswith(' '):
            current_section = stripped.split(':', 1)[0]
            continue
        if current_section == "source" and stripped.startswith("root:"):
            source_root = parse_value(stripped.split(':', 1)[1])
        elif current_section == "ops_logging" and stripped.startswith("log_dir:"):
            ops_log_dir = parse_value(stripped.split(':', 1)[1])
    return source_root, ops_log_dir


def sanitize_name(name: str) -> str:
    sanitized = re.sub(r"\s+", "", name)
    return sanitized or "source"


def resolve_source_root_name(source_root: str | None, config_path: Path) -> str:
    if not source_root:
        return "unknown"
    root_path = Path(source_root).expanduser()
    if not root_path.is_absolute():
        root_path = (config_path.parent / root_path).resolve()
    name = root_path.name or "source"
    return sanitize_name(name)


def resolve_log_dir(log_dir_value: str | None, project_root: Path) -> Path:
    log_dir = Path(log_dir_value) if log_dir_value else Path(".reviewer-log")
    if not log_dir.is_absolute():
        log_dir = project_root / log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def write_log(log_file, text: str) -> None:
    if not text.endswith("\n"):
        text += "\n"
    log_file.write(text.encode("utf-8", errors="replace"))
    log_file.flush()


def echo(log_file, text: str) -> None:
    print(text, flush=True)
    write_log(log_file, text)


def run_command(label: str, argv: list[str], log_file) -> int:
    echo(log_file, f"\n=== {label} ===")
    echo(log_file, f"Command: {shlex.join(argv)}")

    def master_read(fd: int) -> bytes:
        data = os.read(fd, 1024)
        if data:
            log_file.write(data)
            log_file.flush()
        return data

    status = pty.spawn(argv, master_read=master_read)
    exit_code = os.waitstatus_to_exitcode(status)
    if exit_code != 0:
        echo(log_file, f"*** {label} failed with exit code {exit_code}")
    return exit_code


def resolve_bash() -> str | None:
    bash = shutil.which("bash")
    if bash:
        return bash
    # Common FreeBSD location when installed via pkg
    candidate = "/usr/local/bin/bash"
    return candidate if Path(candidate).exists() else None


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    os.chdir(project_root)

    config_path = project_root / "config.yaml"
    defaults_path = project_root / "config.yaml.defaults"

    source_root, ops_log_dir = read_config_values(config_path)
    if source_root is None and defaults_path.exists():
        source_root, ops_log_dir = read_config_values(defaults_path)

    log_dir = resolve_log_dir(ops_log_dir, project_root)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    root_name = resolve_source_root_name(source_root, config_path if config_path.exists() else defaults_path)
    log_path = log_dir / f"make-run-{root_name}-{timestamp}.log"

    with log_path.open("ab") as log_file:
        echo(log_file, f"*** Make run session log: {log_path}")
        write_log(log_file, f"*** Project root: {project_root}")
        write_log(log_file, f"*** Config path: {config_path}")

        exit_code = run_command("check-deps", ["make", "check-deps"], log_file)
        if exit_code != 0:
            return exit_code

        if not config_path.exists():
            echo(log_file, "*** No config.yaml found - running interactive setup...")
            bash = resolve_bash()
            if not bash:
                echo(log_file, "*** ERROR: bash not found; required to run scripts/config-init.sh")
                echo(log_file, "*** Install bash (e.g. FreeBSD: pkg install bash) and retry")
                return 1
            exit_code = run_command("config-init", [bash, "./scripts/config-init.sh"], log_file)
            if exit_code != 0:
                return exit_code
        elif defaults_path.exists() and defaults_path.stat().st_mtime > config_path.stat().st_mtime:
            echo(log_file, "*** config.yaml.defaults is newer than config.yaml")
            exit_code = run_command("config-update", ["make", "config-update"], log_file)
            if exit_code != 0:
                return exit_code

        if config_path.exists():
            updated_root, _ = read_config_values(config_path)
            updated_name = resolve_source_root_name(updated_root, config_path)
            updated_path = log_dir / f"make-run-{updated_name}-{timestamp}.log"
            if updated_path != log_path:
                try:
                    log_path.rename(updated_path)
                    log_path = updated_path
                    echo(log_file, f"*** Session log renamed to: {log_path}")
                except OSError as exc:
                    echo(log_file, f"*** Failed to rename session log: {exc}")

        venv = os.environ.get("VENV", ".venv")
        venv_py = str(Path(venv) / "bin" / "python")
        exit_code = run_command("reviewer", [venv_py, "reviewer.py", "--config", str(config_path)], log_file)
        return exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
