import unittest
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import reviewer
from index_generator import generate_index
from ops_logger import OpsLogger, create_logger_from_config


class _FakeBuildExecutor:
    class _Cfg:
        build_command = "true"

    config = _Cfg()


class _FakeLLM:
    config = type("Cfg", (), {"timeout": 1})()

    def get_recommended_parallelism(self, max_parallel=16):
        raise AssertionError("rewrite mode should not query parallel review capacity")

    def chat(self, history):
        raise AssertionError("chat should not be called by these tests")


def _make_source_tree(root: Path) -> None:
    (root / "bin" / "foo").mkdir(parents=True)
    (root / "bin" / "foo" / "Makefile").write_text("PROG=foo\n")
    (root / "bin" / "foo" / "main.c").write_text("int main(void) { return 0; }\n")


def _make_freebsd_smoke_selection_tree(root: Path) -> None:
    _make_source_tree(root)
    (root / "include" / "arpa").mkdir(parents=True)
    (root / "include" / "arpa" / "Makefile").write_text("INCS=nameser.h\n")
    (root / "include" / "arpa" / "nameser.h").write_text(
        "\n".join(f"#define NS_VALUE_{idx} {idx}" for idx in range(500)) + "\n"
    )
    (root / "usr.sbin" / "hyperv" / "tools").mkdir(parents=True)
    (root / "usr.sbin" / "hyperv" / "tools" / "Makefile.inc").write_text(
        "CFLAGS.gcc+= -Wno-uninitialized\n"
    )


def _make_rust_source_tree(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "Cargo.toml").write_text(
        "[package]\n"
        "name = \"rewrite-smoke\"\n"
        "version = \"0.1.0\"\n"
        "edition = \"2021\"\n"
    )
    (root / "src" / "main.rs").write_text("fn main() { println!(\"hello\"); }\n")
    (root / "tests" / "cli.rs").write_text("#[test]\nfn smoke() {}\n")


class WorkflowModeTests(unittest.TestCase):
    def test_rewrite_mode_skips_full_preflight_build_by_default(self) -> None:
        self.assertFalse(
            reviewer.should_run_preflight_build({"workflow": "rewrite"}, "rewrite")
        )
        self.assertTrue(
            reviewer.should_run_preflight_build(
                {"workflow": "rewrite", "rewrite": {"preflight_build": True}},
                "rewrite",
            )
        )
        self.assertTrue(
            reviewer.should_run_preflight_build({"workflow": "review"}, "review")
        )
        self.assertFalse(
            reviewer.should_run_preflight_build(
                {"workflow": "review"}, "review", skip_preflight_arg=True
            )
        )

    def test_relative_run_log_paths_use_source_root(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                reviewer.resolve_run_log_dir(
                    {"logging": {"log_dir": ".ai-code-reviewer/logs"}},
                    root,
                ),
                root / ".ai-code-reviewer" / "logs",
            )

            ops = create_logger_from_config(
                {"ops_logging": {"log_dir": ".reviewer-log"}},
                source_root=root,
            )
            self.assertEqual(ops.log_dir, root / ".reviewer-log")

    def test_rewrite_index_uses_separate_metadata_file(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_source_tree(root)

            index = generate_index(root, force_rebuild=True, workflow_mode="rewrite")

            self.assertEqual(index.index_path.name, "REWRITE-INDEX.md")
            self.assertTrue(index.index_path.exists())
            self.assertIn("=== REWRITE INDEX SUMMARY ===", index.get_summary_for_ai())
            self.assertFalse((root / ".ai-code-reviewer" / "REVIEW-INDEX.md").exists())

    def test_rewrite_small_first_policy_prefers_buildable_smoke_units(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_freebsd_smoke_selection_tree(root)

            index = generate_index(root, force_rebuild=True, workflow_mode="rewrite")

            self.assertEqual(index.get_next_pending(), "include/arpa")
            self.assertIsNone(index.entries["usr.sbin/hyperv/tools"].build_command)
            self.assertEqual(
                index.get_next_pending(selection_policy="small_first"),
                "bin/foo",
            )
            self.assertEqual(index.get_next_pending(selection_policy="smoke"), "bin/foo")

    def test_rewrite_index_scans_generic_rust_project(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_rust_source_tree(root)

            index = generate_index(root, force_rebuild=True, workflow_mode="rewrite")
            valid, error = reviewer.validate_source_tree(root)

            self.assertEqual(index.index_path.name, "REWRITE-INDEX.md")
            self.assertTrue(valid, error)
            self.assertIn("src", index.entries)
            self.assertEqual(index.entries["src"].total_lines, 1)
            self.assertEqual(index.entries["src"].unit_kind, "rust-binary")
            self.assertEqual(index.entries["src"].stage, "application")
            self.assertEqual(index.entries["src"].build_command, "cargo test")
            self.assertIn("Cargo.toml", index.entries["src"].files)
            self.assertIn("tests/cli.rs", index.entries["src"].files)
            self.assertEqual(index.entries["tests"].unit_kind, "rust-tests")
            self.assertEqual(index.entries["tests"].stage, "validation")
            self.assertEqual(index.entries["tests"].depends_on, ["src"])
            self.assertEqual(index.get_next_pending(), "src")

            content = index.index_path.read_text()
            self.assertIn("## Stage: application", content)
            self.assertIn("<!-- unit:", content)

            loaded = generate_index(root, force_rebuild=False, workflow_mode="rewrite")
            self.assertEqual(loaded.entries["src"].unit_kind, "rust-binary")
            self.assertEqual(loaded.entries["tests"].depends_on, ["src"])

    @unittest.skipIf(shutil.which("git") is None, "git command not available")
    def test_rewrite_index_skips_gitignored_directories(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_source_tree(root)
            (root / ".gitignore").write_text("bin/generated/\n")
            (root / "bin" / "generated").mkdir(parents=True)
            (root / "bin" / "generated" / "main.c").write_text(
                "int main(void) { return 1; }\n"
            )
            subprocess.run(
                ["git", "init"],
                cwd=root,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            index = generate_index(root, force_rebuild=True, workflow_mode="rewrite")

            self.assertIn("bin/foo", index.entries)
            self.assertNotIn("bin/generated", index.entries)

    def test_rewrite_loop_uses_rewrite_prompt_and_summary(self) -> None:
        persona_dir = Path(__file__).resolve().parents[1] / "personas" / "friendly-mentor"
        self.assertTrue((persona_dir / "agent.yaml").exists())

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_source_tree(root)
            ops = OpsLogger(log_dir=root / ".ops-log", session_id="test-session")

            mock_git = MagicMock(spec=reviewer.GitHelper)
            mock_git.repo_root = root
            mock_git._run.return_value = (0, "")
            mock_git.is_ignored.return_value = False
            mock_git.has_changes.return_value = False
            mock_git.ensure_commit_prefix.side_effect = (
                lambda message: message
                if message.startswith(reviewer.COMMIT_PREFIX)
                else f"{reviewer.COMMIT_PREFIX}{message}"
            )

            with patch.object(reviewer.ReviewLoop, "_init_beads_manager", return_value=None), \
                 patch("reviewer.GitHelper", return_value=mock_git):
                loop = reviewer.ReviewLoop(
                    ollama_client=_FakeLLM(),
                    build_executor=_FakeBuildExecutor(),
                    source_root=root,
                    persona_dir=persona_dir,
                    review_config={
                        "workflow": "rewrite",
                        "rewrite": {
                            "objective": "Rewrite small userland utilities side-by-side.",
                        },
                    },
                    target_directories=1,
                    max_iterations_per_directory=10,
                    max_parallel_files=0,
                    ops_logger=ops,
                )

            self.assertEqual(loop.workflow_mode, "rewrite")
            self.assertEqual(loop.review_summary_file.name, "REWRITE-SUMMARY.md")
            self.assertEqual(loop.index.index_path.name, "REWRITE-INDEX.md")
            self.assertEqual(loop.log_dir, root / ".ai-code-reviewer" / "logs")
            self.assertFalse(loop._parallel_mode)

            system_prompt = loop.history[0]["content"]
            init_message = loop.history[1]["content"]
            self.assertIn("code rewriting AI", system_prompt)
            self.assertIn("broader than translation", system_prompt)
            self.assertNotIn("FreeBSD source code", system_prompt)
            self.assertIn("Rewrite small userland utilities side-by-side.", init_message)

            with patch.object(loop, "_record_directory_attempt", return_value=1):
                result = loop._execute_action({"action": "SET_SCOPE", "directory": "bin/foo"})

            self.assertIn("Now rewriting bin/foo", result)
            self.assertIn("WORK UNIT:", result)
            self.assertIn("Kind: freebsd-command", result)
            self.assertIn("Stage: application", result)
            self.assertIn("Build command: make -C bin/foo", result)
            self.assertIn("FILES TO REWRITE", result)
            self.assertEqual(loop._current_build_command(), "make -C bin/foo")

            with patch.object(loop, "_ask_ai_simple", return_value=None):
                commit_msg = loop._generate_commit_message("", [], "bin/foo")
            self.assertIn("[ai-code-reviewer]", commit_msg)
            self.assertIn("foo", commit_msg)

    def test_tool_metadata_paths_are_recognized(self) -> None:
        self.assertTrue(reviewer.is_tool_metadata_path(".reviewer-log/ops.jsonl"))
        self.assertTrue(
            reviewer.is_tool_metadata_path(".ai-code-reviewer/REWRITE-INDEX.md")
        )
        self.assertTrue(reviewer.is_tool_metadata_path("REWRITE-SUMMARY.md"))
        self.assertFalse(reviewer.is_tool_metadata_path("bin/foo/main.c"))


if __name__ == "__main__":
    unittest.main()
