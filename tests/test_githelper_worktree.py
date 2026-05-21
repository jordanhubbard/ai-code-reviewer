import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import reviewer


class GitHelperWorktreeTests(unittest.TestCase):
    def test_get_worktree_branch_paths_parses_porcelain(self) -> None:
        git = reviewer.GitHelper(Path("/tmp/repo"))
        sample = (
            "worktree /tmp/repo\n"
            "HEAD 123456\n"
            "branch refs/heads/main\n"
            "\n"
            "worktree /tmp/repo/.git/beads-worktrees/feature\n"
            "HEAD abcdef\n"
            "branch refs/heads/feature\n"
        )
        with patch.object(git, "_run", return_value=(0, sample)):
            result = git._get_worktree_branch_paths()

        self.assertEqual(result["refs/heads/main"], "/tmp/repo")
        self.assertEqual(
            result["refs/heads/feature"],
            "/tmp/repo/.git/beads-worktrees/feature",
        )

    def test_get_worktree_path_for_branch_returns_path(self) -> None:
        git = reviewer.GitHelper(Path("/tmp/repo"))
        with patch.object(
            git,
            "_get_worktree_branch_paths",
            return_value={"refs/heads/main": "/tmp/wt"},
        ):
            self.assertEqual(git._get_worktree_path_for_branch("main"), "/tmp/wt")
            self.assertIsNone(git._get_worktree_path_for_branch("missing"))

    def test_changed_files_list_includes_untracked_porcelain_paths(self) -> None:
        git = reviewer.GitHelper(Path("/tmp/repo"))
        sample = (
            " M .reviewer-log/ops.jsonl\0"
            "?? .ai-code-reviewer/new.json\0"
            "R  new-name.c\0"
            "old-name.c\0"
        )
        with patch.object(git, "_run_raw", return_value=(0, sample)) as run_raw:
            result = git.changed_files_list(include_untracked=True)

        self.assertEqual(
            result,
            [".reviewer-log/ops.jsonl", ".ai-code-reviewer/new.json", "new-name.c"],
        )
        run_raw.assert_called_once_with([
            "status",
            "--porcelain",
            "-z",
            "--untracked-files=all",
        ])

    def test_changed_files_list_expands_untracked_rust_directories(self) -> None:
        git = reviewer.GitHelper(Path("/tmp/repo"))
        sample = (
            " M usr.bin/true/Makefile\0"
            "?? usr.bin/true/rust/Cargo.toml\0"
            "?? usr.bin/true/rust/src/main.rs\0"
        )
        with patch.object(git, "_run_raw", return_value=(0, sample)):
            result = git.changed_files_list(include_untracked=True)

        self.assertEqual(
            result,
            [
                "usr.bin/true/Makefile",
                "usr.bin/true/rust/Cargo.toml",
                "usr.bin/true/rust/src/main.rs",
            ],
        )

    def test_diff_all_replaces_invalid_utf8_bytes(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo_root, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_root, check=True)
            path = repo_root / "invalid.txt"
            path.write_bytes(b"ok\n")
            subprocess.run(["git", "add", "invalid.txt"], cwd=repo_root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_root, check=True)
            path.write_bytes(b"bad \xe6\n")

            diff = reviewer.GitHelper(repo_root).diff_all()

        self.assertIn("bad", diff)
        self.assertIn("\ufffd", diff)

    def test_clean_paths_removes_untracked_scope_files(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo_root, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_root, check=True)
            scope = repo_root / "usr.bin" / "true"
            scope.mkdir(parents=True)
            (scope / "Makefile").write_text("PROG=true\n")
            subprocess.run(["git", "add", "usr.bin/true/Makefile"], cwd=repo_root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_root, check=True)
            (scope / "rust" / "src").mkdir(parents=True)
            (scope / "rust" / "Cargo.toml").write_text("[package]\nname=\"true\"\n")
            (scope / "rust" / "src" / "main.rs").write_text("fn main() {}\n")

            ok, output = reviewer.GitHelper(repo_root).clean_paths(["usr.bin/true"])

            self.assertTrue(ok, output)
            self.assertFalse((scope / "rust").exists())

    def test_ensure_repository_ready_uses_fallback_branch_when_in_worktree(self) -> None:
        git = reviewer.GitHelper(Path("/tmp/repo"))

        def _run_side_effect(args, capture=True):
            if args[:2] == ["checkout", "-b"]:
                return 0, ""
            if args[:2] == ["status", "--short"]:
                return 0, ""
            return 0, ""

        with patch.object(git, "abort_rebase_if_needed", return_value=(True, None)), \
            patch.object(git, "abort_merge_if_needed", return_value=(True, None)), \
            patch.object(git, "get_current_branch", return_value="HEAD"), \
            patch.object(git, "_get_worktree_path_for_branch", return_value="/tmp/wt"), \
            patch.object(git, "_resolve_branch_ref", return_value="main"), \
            patch.object(git, "_make_fallback_branch", return_value="reviewer/main-000"), \
            patch.object(git, "_run", side_effect=_run_side_effect) as run_mock:
            ok, msg = git.ensure_repository_ready(allow_rebase=False)

        self.assertTrue(ok)
        self.assertIn("reviewer/main-000", msg)
        self.assertIn("/tmp/wt", msg)
        run_mock.assert_any_call(["checkout", "-b", "reviewer/main-000", "main"])
        run_mock.assert_any_call(["status", "--short"])


    def test_checkout_stashes_tool_files_on_untracked_error(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            (repo_root / ".beads").mkdir()
            git = reviewer.GitHelper(repo_root)
            error_output = (
                "error: The following untracked working tree files would be overwritten by checkout:\n"
                "        .beads/metadata.json\n"
                "Please move or remove them before you switch branches.\n"
                "Aborting"
            )
            state = {"attempts": 0}

            def _run_side_effect(args, capture=True):
                if args[:3] == ["checkout", "-b", "reviewer/main-000"]:
                    state["attempts"] += 1
                    if state["attempts"] == 1:
                        return 1, error_output
                    return 0, ""
                if args[:2] == ["stash", "push"]:
                    return 0, "Saved working directory"
                if args[:2] == ["status", "--short"]:
                    return 0, ""
                return 0, ""

            with patch.object(git, "abort_rebase_if_needed", return_value=(True, None)),                 patch.object(git, "abort_merge_if_needed", return_value=(True, None)),                 patch.object(git, "get_current_branch", return_value="HEAD"),                 patch.object(git, "_get_worktree_path_for_branch", return_value="/tmp/wt"),                 patch.object(git, "_resolve_branch_ref", return_value="main"),                 patch.object(git, "_make_fallback_branch", return_value="reviewer/main-000"),                 patch.object(git, "_run", side_effect=_run_side_effect) as run_mock:
                ok, msg = git.ensure_repository_ready(allow_rebase=False)

        self.assertTrue(ok)
        self.assertIn("stashed tool files", msg)
        run_mock.assert_any_call([
            "stash",
            "push",
            "--all",
            "-m",
            "reviewer-prep-tool-files",
            "--",
            ".beads/",
        ])


if __name__ == "__main__":
    unittest.main()
