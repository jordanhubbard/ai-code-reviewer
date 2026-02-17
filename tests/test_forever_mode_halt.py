import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import subprocess


import reviewer
from ops_logger import OpsLogger


class _FakeBuildExecutor:
    class _Cfg:
        build_command = "true"

    config = _Cfg()


class _FakeLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.loop: reviewer.ReviewLoop | None = None
        # reviewer.py references self.ollama.config.timeout in error handling
        self.config = type("Cfg", (), {"timeout": 1})()

    def chat(self, history):
        self.calls += 1
        if self.calls == 1:
            return "ACTION: HALT"
        if self.calls == 2:
            assert self.loop is not None
            # Simulate all work being done so HALT can be acknowledged.
            for entry in self.loop.index.entries.values():
                entry.status = "done"
            self.loop.session.current_directory = None
            return "ACTION: HALT"
        raise AssertionError(f"Unexpected chat() call {self.calls}")


class ForeverModeHaltTests(unittest.TestCase):
    def test_forever_mode_does_not_stop_on_rejected_halt(self) -> None:
        persona_dir = Path(__file__).resolve().parents[1] / "personas" / "friendly-mentor"
        self.assertTrue((persona_dir / "agent.yaml").exists())

        with TemporaryDirectory() as tmp:
            root = Path(tmp)

            # Create a reviewable directory for the index generator.
            (root / "bin" / "foo").mkdir(parents=True)
            (root / "bin" / "foo" / "main.c").write_text("int main(void){return 0;}\n")

            # Ensure source_root is a git repo so GitHelper calls in run() don't fail.
            subprocess.run(["git", "init"], cwd=str(root), check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(root), check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=str(root), check=True, capture_output=True, text=True)
            subprocess.run(["git", "add", "."], cwd=str(root), check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(root), check=True, capture_output=True, text=True)

            llm = _FakeLLM()
            ops = OpsLogger(log_dir=root / ".ops-log", session_id="test-session")

            with patch.object(reviewer.ReviewLoop, "_init_beads_manager", return_value=None):
                loop = reviewer.ReviewLoop(
                    ollama_client=llm,
                    build_executor=_FakeBuildExecutor(),
                    source_root=root,
                    persona_dir=persona_dir,
                    review_config={},
                    target_directories=0,
                    max_iterations_per_directory=10,
                    max_parallel_files=1,
                    log_dir=root / "logs",
                    ops_logger=ops,
                    forever_mode=True,
                )

            llm.loop = loop
            loop.run()

            # If HALT were handled directly (without validation), the loop would exit on call 1.
            self.assertEqual(llm.calls, 2)
            self.assertTrue(any(
                m.get("role") == "user" and "HALT_REJECTED" in m.get("content", "")
                for m in loop.history
            ))


if __name__ == "__main__":
    unittest.main()
