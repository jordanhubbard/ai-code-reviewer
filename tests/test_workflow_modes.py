import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import reviewer
from index_generator import generate_index
from ops_logger import OpsLogger


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


class WorkflowModeTests(unittest.TestCase):
    def test_rewrite_index_uses_separate_metadata_file(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_source_tree(root)

            index = generate_index(root, force_rebuild=True, workflow_mode="rewrite")

            self.assertEqual(index.index_path.name, "REWRITE-INDEX.md")
            self.assertTrue(index.index_path.exists())
            self.assertIn("=== REWRITE INDEX SUMMARY ===", index.get_summary_for_ai())
            self.assertFalse((root / ".ai-code-reviewer" / "REVIEW-INDEX.md").exists())

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
                    log_dir=root / "logs",
                    ops_logger=ops,
                )

            self.assertEqual(loop.workflow_mode, "rewrite")
            self.assertEqual(loop.review_summary_file.name, "REWRITE-SUMMARY.md")
            self.assertEqual(loop.index.index_path.name, "REWRITE-INDEX.md")
            self.assertFalse(loop._parallel_mode)

            system_prompt = loop.history[0]["content"]
            init_message = loop.history[1]["content"]
            self.assertIn("code rewriting AI", system_prompt)
            self.assertIn("broader than translation", system_prompt)
            self.assertIn("Rewrite small userland utilities side-by-side.", init_message)

            with patch.object(loop, "_record_directory_attempt", return_value=1):
                result = loop._execute_action({"action": "SET_SCOPE", "directory": "bin/foo"})

            self.assertIn("Now rewriting bin/foo", result)
            self.assertIn("FILES TO REWRITE", result)


if __name__ == "__main__":
    unittest.main()
