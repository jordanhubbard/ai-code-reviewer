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
        build_environment = {}

    config = _Cfg()

    def _build_env(self):
        return None


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


def _make_two_unit_source_tree(root: Path) -> None:
    _make_source_tree(root)
    (root / "bin" / "bar").mkdir(parents=True)
    (root / "bin" / "bar" / "Makefile").write_text("PROG=bar\n")
    (root / "bin" / "bar" / "main.c").write_text("int main(void) { return 0; }\n")


def _make_freebsd_smoke_selection_tree(root: Path) -> None:
    _make_source_tree(root)
    (root / "sbin" / "tests").mkdir(parents=True)
    (root / "sbin" / "tests" / "Makefile").write_text("SUBDIR=ifconfig\n")
    (root / "libexec" / "rtld-elf" / "tests" / "libval").mkdir(parents=True)
    (root / "libexec" / "rtld-elf" / "tests" / "libval" / "Makefile").write_text("LIB=val\n")
    (root / "libexec" / "rtld-elf" / "tests" / "libval" / "libval.c").write_text("int val(void) { return 0; }\n")
    (root / "usr.bin" / "command").mkdir(parents=True)
    (root / "usr.bin" / "command" / "Makefile").write_text("SCRIPTS=command.sh\n")
    (root / "usr.bin" / "command" / "command.sh").write_text("#!/bin/sh\nexit 0\n")
    (root / "usr.bin" / "clang" / "llvm-dwp").mkdir(parents=True)
    (root / "usr.bin" / "clang" / "llvm-dwp" / "Makefile").write_text("PROG=llvm-dwp\n")
    (root / "usr.bin" / "clang" / "llvm-dwp" / "llvm-dwp-driver.cpp").write_text(
        "int main(void) { return 0; }\n"
    )
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


def _mock_git_for_loop(root: Path) -> MagicMock:
    mock_git = MagicMock(spec=reviewer.GitHelper)
    mock_git.repo_root = root
    mock_git._run.return_value = (0, "abc123456789\n")
    mock_git.diff.return_value = "diff --git a/bin/foo/main.c b/bin/foo/main.c\n"
    mock_git.diff_all.return_value = "diff --git a/bin/foo/main.c b/bin/foo/main.c\n"
    mock_git.is_ignored.return_value = False
    mock_git.has_changes.return_value = False
    mock_git.ensure_commit_prefix.side_effect = (
        lambda message: message
        if message.startswith(reviewer.COMMIT_PREFIX)
        else f"{reviewer.COMMIT_PREFIX}{message}"
    )
    return mock_git


def _make_rewrite_loop(root: Path, mock_git: MagicMock, ops: OpsLogger) -> reviewer.ReviewLoop:
    persona_dir = Path(__file__).resolve().parents[1] / "personas" / "friendly-mentor"
    with patch.object(reviewer.ReviewLoop, "_init_beads_manager", return_value=None), \
         patch("reviewer.GitHelper", return_value=mock_git):
        return reviewer.ReviewLoop(
            ollama_client=_FakeLLM(),
            build_executor=_FakeBuildExecutor(),
            source_root=root,
            persona_dir=persona_dir,
            review_config={"workflow": "rewrite"},
            target_directories=2,
            max_iterations_per_directory=10,
            max_parallel_files=0,
            ops_logger=ops,
        )


class WorkflowModeTests(unittest.TestCase):
    def test_file_editor_rejects_identical_noop_edit(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "main.c"
            original = "int main(void) { return 0; }\n"
            path.write_text(original)

            mock_git = MagicMock()
            editor = reviewer.FileEditor(mock_git)

            success, message, diff = editor.edit_file(
                path,
                "return 0;",
                "return 0;",
            )

            self.assertFalse(success)
            self.assertIn("No-op edit rejected", message)
            self.assertEqual(diff, "")
            self.assertEqual(path.read_text(), original)
            mock_git.diff.assert_not_called()

    def test_file_editor_rejects_identical_noop_write(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "main.c"
            original = "int main(void) { return 0; }\n"
            path.write_text(original)

            mock_git = MagicMock()
            editor = reviewer.FileEditor(mock_git)

            success, message, diff = editor.write_file(path, original)

            self.assertFalse(success)
            self.assertIn("No-op edit rejected", message)
            self.assertEqual(diff, "")
            self.assertEqual(path.read_text(), original)
            mock_git.diff.assert_not_called()

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
            self.assertEqual(index.entries["sbin/tests"].build_command, "make -C sbin/tests")
            self.assertEqual(index.entries["sbin/tests"].unit_kind, "freebsd-tests")
            self.assertEqual(
                index.entries["libexec/rtld-elf/tests/libval"].unit_kind,
                "freebsd-tests",
            )
            self.assertEqual(index.entries["usr.bin/clang/llvm-dwp"].unit_kind, "bootstrap-tool")
            self.assertEqual(
                index.get_next_pending(selection_policy="small_first"),
                "bin/foo",
            )
            self.assertEqual(index.get_next_pending(selection_policy="smoke"), "bin/foo")
            self.assertEqual(
                index.get_next_pending(
                    selection_policy="small_first",
                    required_source_suffixes=[".c", ".cc", ".cpp", ".cxx"],
                ),
                "bin/foo",
            )

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
                            "target_language": "rust",
                            "source_suffixes": [".c", ".cc"],
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
            self.assertIn("Do not create placeholder crates", system_prompt)
            self.assertIn("choose another scope instead of fabricating a rewrite", system_prompt)
            self.assertIn("normal active unit build must build the Rust version", system_prompt)
            self.assertNotIn("FreeBSD source code", system_prompt)
            self.assertIn("Rewrite small userland utilities side-by-side.", init_message)
            self.assertIn("Required source suffixes: .c, .cc", init_message)

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

            changed_files = [
                "bin/foo/Makefile",
                "bin/foo/crates/foo/Cargo.toml",
                "bin/foo/crates/foo/src/main.rs",
            ]
            c_only_build = reviewer.BuildResult(
                success=True,
                return_code=0,
                duration_seconds=1.0,
                raw_output="cc -o foo foo.o\n",
            )
            rejection = loop._rewrite_build_completion_error(c_only_build, changed_files)
            self.assertIsNotNone(rejection)
            self.assertIn("BUILD_REJECTED", rejection)
            self.assertIn("normal active unit build produce the Rust version", rejection)

            rust_build = reviewer.BuildResult(
                success=True,
                return_code=0,
                duration_seconds=1.0,
                raw_output="cargo build --manifest-path crates/foo/Cargo.toml\n",
            )
            self.assertIsNone(loop._rewrite_build_completion_error(rust_build, changed_files))

            no_rust_changes = reviewer.BuildResult(
                success=True,
                return_code=0,
                duration_seconds=1.0,
                raw_output="cargo build --manifest-path crates/foo/Cargo.toml\n",
            )
            no_rust_rejection = loop._rewrite_build_completion_error(
                no_rust_changes,
                ["bin/foo/Makefile"],
            )
            self.assertIsNotNone(no_rust_rejection)
            self.assertIn("requires Rust source or Cargo manifest changes", no_rust_rejection)

            (root / "bin" / "foo" / "rust").mkdir()
            (root / "bin" / "foo" / "rust" / "Cargo.toml").write_text(
                "[package]\nname = \"foo\"\nversion = \"0.0.0\"\nedition = \"2021\"\n"
            )
            loop.session.current_directory = "bin/foo"
            loop.rewrite_config["contract"] = {
                "integrated_build_must_invoke": ["cargo"],
                "rust_build": "cargo build --manifest-path {manifest} --offline --target-dir {target_dir}",
                "artifacts": {
                    "rust_files_required": True,
                    "cargo_manifest_required": True,
                    "external_crates": "vendored_only",
                },
            }
            contract_files = [
                "bin/foo/rust/Cargo.toml",
                "bin/foo/rust/src/main.rs",
            ]
            contract_build = reviewer.BuildResult(
                success=True,
                return_code=0,
                duration_seconds=1.0,
                raw_output="cargo build --manifest-path bin/foo/rust/Cargo.toml\n",
            )
            with patch.object(
                loop,
                "_run_contract_command",
                return_value=reviewer.BuildResult(
                    success=True,
                    return_code=0,
                    duration_seconds=0.1,
                    raw_output="Finished `dev` profile",
                ),
            ) as contract_run:
                self.assertIsNone(loop._rewrite_build_completion_error(contract_build, contract_files))
            contract_run.assert_called_once()

            with patch.object(
                loop,
                "_run_contract_command",
                return_value=reviewer.BuildResult(
                    success=False,
                    return_code=101,
                    duration_seconds=0.1,
                    raw_output="cargo error",
                ),
            ):
                contract_failure = loop._rewrite_build_completion_error(contract_build, contract_files)
            self.assertIsNotNone(contract_failure)
            self.assertIn("configured Rust build failed", contract_failure)

            loop.rewrite_config["contract"]["rust_build"] = (
                "cargo build --manifest-path {manifest} --target-dir {target_dir}"
            )
            offline_failure = loop._rewrite_build_completion_error(contract_build, contract_files)
            self.assertIsNotNone(offline_failure)
            self.assertIn("requires offline/vendored Cargo", offline_failure)

            loop.history.extend(
                [
                    {"role": "assistant", "content": "analysis\nACTION: BUILD"},
                    {
                        "role": "user",
                        "content": "BUILD_SUCCESS\nFINAL DIFFS:\n" + ("x" * 200_000),
                    },
                ]
            )
            compacted = loop._compact_history_for_llm(aggressive=True)
            self.assertTrue(compacted)
            self.assertLess(loop._estimate_history_tokens(), loop._history_token_budget(aggressive=True))
            self.assertIn("history compacted", loop.history[-1]["content"])

    def test_rewrite_contract_cli_equivalence_checks(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_source_tree(root)
            ops = OpsLogger(log_dir=root / ".ops-log", session_id="test-session")
            mock_git = _mock_git_for_loop(root)
            loop = _make_rewrite_loop(root, mock_git, ops)
            loop.session.current_directory = "bin/foo"
            loop.rewrite_config["require_rust_build"] = False
            loop.rewrite_config["contract"] = {
                "equivalence": {
                    "cli": [
                        {
                            "unit": "bin/bar",
                            "source_command": "false",
                            "rust_command": "false",
                        },
                        {
                            "unit": "bin/foo",
                            "source_command": "printf source",
                            "rust_command": "printf rust",
                        }
                    ]
                }
            }
            build_result = reviewer.BuildResult(
                success=True,
                return_code=0,
                duration_seconds=0.1,
                raw_output="",
            )

            equal = subprocess.CompletedProcess(args="", returncode=0, stdout="same", stderr="")
            with patch.object(loop, "_run_contract_process", side_effect=[equal, equal]) as run_case:
                self.assertIsNone(loop._rewrite_build_completion_error(build_result, ["bin/foo/main.c"]))
            self.assertEqual(run_case.call_count, 2)

            source = subprocess.CompletedProcess(args="", returncode=0, stdout="source", stderr="")
            rust = subprocess.CompletedProcess(args="", returncode=0, stdout="rust", stderr="")
            with patch.object(loop, "_run_contract_process", side_effect=[source, rust]):
                mismatch = loop._rewrite_build_completion_error(build_result, ["bin/foo/main.c"])
            self.assertIsNotNone(mismatch)
            self.assertIn("stdout mismatch", mismatch)

    def test_scope_change_ignores_metadata_only_dirty_state(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_two_unit_source_tree(root)
            ops = OpsLogger(log_dir=root / ".ops-log", session_id="test-session")
            mock_git = _mock_git_for_loop(root)
            mock_git.changed_files_list.return_value = [".reviewer-log/ops.jsonl"]
            loop = _make_rewrite_loop(root, mock_git, ops)

            loop.session.current_directory = "bin/foo"
            loop.session.pending_changes = False
            loop.session.changed_files = []

            with patch.object(loop, "_record_directory_attempt", return_value=1):
                result = loop._execute_action({"action": "SET_SCOPE", "directory": "bin/bar"})

            self.assertIn("Now rewriting bin/bar", result)
            self.assertEqual(loop.session.current_directory, "bin/bar")

    def test_completed_work_unit_cannot_be_reopened_or_edited(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_two_unit_source_tree(root)
            ops = OpsLogger(log_dir=root / ".ops-log", session_id="test-session")
            mock_git = _mock_git_for_loop(root)
            mock_git.changed_files_list.return_value = []
            loop = _make_rewrite_loop(root, mock_git, ops)
            loop.index.mark_done("bin/foo", "done")

            set_scope = loop._execute_action({"action": "SET_SCOPE", "directory": "bin/foo"})
            edit = loop._execute_action({
                "action": "EDIT_FILE",
                "file_path": "bin/foo/main.c",
                "old_text": "return 0;",
                "new_text": "return 1;",
            })
            write = loop._execute_action({
                "action": "WRITE_FILE",
                "file_path": "bin/foo/new.rs",
                "content": "fn main() {}\n",
            })

            self.assertIn("already marked complete", set_scope)
            self.assertIn("Refusing to edit", edit)
            self.assertIn("already complete", edit)
            self.assertIn("Refusing to write", write)
            self.assertIn("NEXT: Use SET_SCOPE bin/bar", set_scope)

    def test_build_without_source_changes_is_rejected_before_build_runs(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_source_tree(root)
            ops = OpsLogger(log_dir=root / ".ops-log", session_id="test-session")
            mock_git = _mock_git_for_loop(root)
            mock_git.changed_files_list.return_value = [".reviewer-log/ops.jsonl"]
            loop = _make_rewrite_loop(root, mock_git, ops)
            loop.session.current_directory = "bin/foo"
            loop.session.pending_changes = False
            loop.session.changed_files = []

            with patch.object(loop, "_run_build_with_live_output") as build_mock:
                result = loop._execute_action({"action": "BUILD"})

            self.assertIn("BUILD_REJECTED: No pending source or build-file changes", result)
            self.assertIn("Only reviewer metadata is dirty", result)
            build_mock.assert_not_called()

    def test_successful_build_clears_active_scope_after_commit(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_two_unit_source_tree(root)
            ops = OpsLogger(log_dir=root / ".ops-log", session_id="test-session")
            mock_git = _mock_git_for_loop(root)
            mock_git.changed_files_list.return_value = [
                "bin/foo/Makefile",
                "bin/foo/rust/Cargo.toml",
            ]
            loop = _make_rewrite_loop(root, mock_git, ops)
            loop.session.current_directory = "bin/foo"
            loop.session.pending_changes = True
            loop.session.changed_files = ["bin/foo/Makefile", "bin/foo/rust/Cargo.toml"]
            build_result = reviewer.BuildResult(
                success=True,
                return_code=0,
                duration_seconds=0.1,
                raw_output="cargo build --manifest-path bin/foo/rust/Cargo.toml\n",
            )

            with patch.object(loop, "_run_build_with_live_output", return_value=build_result), \
                 patch.object(loop, "_generate_commit_message", return_value="[ai-code-reviewer] foo: rewrite"), \
                 patch.object(loop, "_commit_and_push", return_value=(True, "pushed")), \
                 patch.object(loop, "_commit_tool_metadata_after_success", return_value="metacommit"):
                result = loop._execute_action({"action": "BUILD"})

            self.assertIn("BUILD_SUCCESS", result)
            self.assertIn("Directory bin/foo is now complete", result)
            self.assertIn("NEXT: Use SET_SCOPE bin/bar", result)
            self.assertIsNone(loop.session.current_directory)
            self.assertFalse(loop.session.pending_changes)
            self.assertEqual(loop.session.changed_files, [])
            self.assertEqual(loop.session.directories_completed, 1)
            self.assertEqual(loop.session.completed_directories, ["bin/foo"])
            self.assertEqual(loop.index.entries["bin/foo"].status, reviewer.Status.DONE)

    def test_repeated_noop_edit_requests_stop_run(self) -> None:
        persona_dir = Path(__file__).resolve().parents[1] / "personas" / "friendly-mentor"

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
                    review_config={"workflow": "rewrite"},
                    target_directories=1,
                    max_iterations_per_directory=10,
                    max_parallel_files=0,
                    ops_logger=ops,
                )

            loop.session.current_directory = "bin/foo"
            action = {
                "action": "EDIT_FILE",
                "file_path": "bin/foo/main.c",
                "old_text": "return 0;",
                "new_text": "return 0;",
            }

            first = loop._execute_action(action)
            second = loop._execute_action(action)
            third = loop._execute_action(action)

            self.assertIn("NO-OP EDIT REJECTED", first)
            self.assertIn("NO-OP EDIT REJECTED", second)
            self.assertIn("NO-OP EDIT LOOP DETECTED", third)
            self.assertTrue(loop._stop_requested)
            self.assertEqual(loop._stop_reason, "Repeated no-op EDIT_FILE loop on bin/foo/main.c")
            self.assertFalse(loop.session.pending_changes)
            self.assertEqual(loop.session.changed_files, [])

    def test_repeated_noop_write_requests_stop_run(self) -> None:
        persona_dir = Path(__file__).resolve().parents[1] / "personas" / "friendly-mentor"

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
                    review_config={"workflow": "rewrite"},
                    target_directories=1,
                    max_iterations_per_directory=10,
                    max_parallel_files=0,
                    ops_logger=ops,
                )

            loop.session.current_directory = "bin/foo"
            content = (root / "bin" / "foo" / "main.c").read_text()
            action = {
                "action": "WRITE_FILE",
                "file_path": "bin/foo/main.c",
                "content": content,
            }

            first = loop._execute_action(action)
            second = loop._execute_action(action)
            third = loop._execute_action(action)

            self.assertIn("NO-OP EDIT REJECTED", first)
            self.assertIn("NO-OP EDIT REJECTED", second)
            self.assertIn("NO-OP EDIT LOOP DETECTED", third)
            self.assertTrue(loop._stop_requested)
            self.assertEqual(loop._stop_reason, "Repeated no-op WRITE_FILE loop on bin/foo/main.c")
            self.assertFalse(loop.session.pending_changes)
            self.assertEqual(loop.session.changed_files, [])

    def test_write_file_parser_accepts_incomplete_content_fences(self) -> None:
        action = reviewer.ActionParser.parse(
            "ACTION: WRITE_FILE usr.bin/foo/Cargo.toml\n"
            "CONTENT:\n"
            "<<<\n"
            "[package]\n"
            "name = \"foo\"\n"
        )

        self.assertIsNotNone(action)
        self.assertEqual(action["action"], "WRITE_FILE")
        self.assertEqual(action["file_path"], "usr.bin/foo/Cargo.toml")
        self.assertIn("name = \"foo\"", action["content"])

    def test_write_file_parser_accepts_plain_content(self) -> None:
        action = reviewer.ActionParser.parse(
            "ACTION: WRITE_FILE usr.bin/foo/Cargo.toml\n"
            "CONTENT:\n"
            "[package]\n"
            "name = \"foo\"\n"
        )

        self.assertIsNotNone(action)
        self.assertEqual(action["content"], "[package]\nname = \"foo\"")

    def test_tool_metadata_paths_are_recognized(self) -> None:
        self.assertTrue(reviewer.is_tool_metadata_path(".reviewer-log/ops.jsonl"))
        self.assertTrue(
            reviewer.is_tool_metadata_path(".ai-code-reviewer/REWRITE-INDEX.md")
        )
        self.assertTrue(reviewer.is_tool_metadata_path("REWRITE-SUMMARY.md"))
        self.assertFalse(reviewer.is_tool_metadata_path("bin/foo/main.c"))


if __name__ == "__main__":
    unittest.main()
