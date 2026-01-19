import unittest
from pathlib import Path
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
        git = reviewer.GitHelper(Path("/tmp/repo"))
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

        with patch.object(git, "abort_rebase_if_needed", return_value=(True, None)),             patch.object(git, "abort_merge_if_needed", return_value=(True, None)),             patch.object(git, "get_current_branch", return_value="HEAD"),             patch.object(git, "_get_worktree_path_for_branch", return_value="/tmp/wt"),             patch.object(git, "_resolve_branch_ref", return_value="main"),             patch.object(git, "_make_fallback_branch", return_value="reviewer/main-000"),             patch.object(git, "_run", side_effect=_run_side_effect) as run_mock:
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
            ".ai-code-reviewer/",
            ".angry-ai/",
            "REVIEW-INDEX.md",
        ])


if __name__ == "__main__":
    unittest.main()
