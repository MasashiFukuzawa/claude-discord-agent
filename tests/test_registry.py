"""Registry tests: worktree path resolution, DB path management."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.db import Database
from lib.registry import Registry, RegistryError, preflight_check


class TestPreflightCheck(unittest.TestCase):
    """preflight_check のバリデーションテスト。"""

    def test_nonexistent_path(self):
        with self.assertRaises(RegistryError):
            preflight_check("/nonexistent/path/xyz")

    def test_not_a_directory(self):
        with tempfile.NamedTemporaryFile() as f:
            with self.assertRaises(RegistryError):
                preflight_check(f.name)

    def test_not_a_git_repo(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RegistryError):
                preflight_check(d)

    def test_valid_git_repo(self):
        """有効なgitリポジトリでpreflight_checkが成功する。"""
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", d], check=True, capture_output=True)
            result = preflight_check(d)
            self.assertTrue(result["valid"])
            self.assertIn("git_root", result)
            self.assertIn("branch", result)

    def test_expected_git_root_mismatch(self):
        """expected_git_rootと実際のgit rootが異なる場合はエラー。"""
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", d], check=True, capture_output=True)
            with self.assertRaises(RegistryError):
                preflight_check(d, expected_git_root="/some/other/path")


class TestRegistryWorktreePath(unittest.TestCase):
    """worktree path の登録・解決テスト。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)
        self.registry = Registry(self.db)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)

    def test_register_and_resolve_worktree_path(self):
        """worktree パスをそのまま登録・解決できる。"""
        with tempfile.TemporaryDirectory() as work_dir:
            subprocess.run(["git", "init", work_dir], check=True, capture_output=True)
            self.registry.register("test-repo", work_dir)
            resolved = self.registry.resolve("test-repo")
            self.assertEqual(resolved["path"], str(Path(work_dir).resolve()))

    def test_worktree_path_differs_from_main(self):
        """main と .work は異なるパスとして解決される。"""
        with tempfile.TemporaryDirectory() as main_dir:
            with tempfile.TemporaryDirectory() as work_dir:
                subprocess.run(["git", "init", main_dir], check=True, capture_output=True)
                subprocess.run(["git", "init", work_dir], check=True, capture_output=True)

                self.registry.register("main-repo", main_dir, skip_preflight=True)
                self.registry.register("work-repo", work_dir, skip_preflight=True)

                main_resolved = self.registry.resolve("main-repo")
                work_resolved = self.registry.resolve("work-repo")

                self.assertNotEqual(main_resolved["path"], work_resolved["path"])

    def test_update_path_to_worktree(self):
        """既存 repo のパスを worktree に変更できる（DB 直接更新の想定動作）。"""
        with tempfile.TemporaryDirectory() as main_dir:
            with tempfile.TemporaryDirectory() as work_dir:
                subprocess.run(["git", "init", main_dir], check=True, capture_output=True)
                subprocess.run(["git", "init", work_dir], check=True, capture_output=True)

                self.registry.register("orchestrator-meta", main_dir, skip_preflight=True)

                work_path = str(Path(work_dir).resolve())
                self.db.update_repo(
                    "orchestrator-meta",
                    path=work_path,
                    expected_git_root=work_path,
                )

                resolved = self.registry.resolve("orchestrator-meta")
                self.assertEqual(resolved["path"], work_path)
                self.assertEqual(resolved["expected_git_root"], work_path)

    def test_runner_uses_db_path_as_working_dir(self):
        """runner.run_job が DB の repo.path を cwd として使う（worktree 分離の核心）。"""
        from unittest.mock import MagicMock, patch

        from lib.runner import Runner

        with tempfile.TemporaryDirectory() as work_dir:
            repo_id = self.db.create_repo("orchestrator-meta", work_dir)
            sess_id = self.db.create_session(repo_id)
            job_id = self.db.create_job(repo_id, "echo from work tree: pwd")

            runner = Runner(self.db)
            with patch("lib.runner.subprocess.Popen") as mock_popen:
                mock_proc = MagicMock()
                mock_proc.pid = 42
                mock_proc.stdout = iter([])
                mock_proc.stderr = iter([])
                mock_proc.poll.return_value = None
                mock_popen.return_value = mock_proc

                with patch("lib.runner.ClaudeProcess._read_stdout"):
                    runner.run_job(job_id, sess_id)

                call_kwargs = mock_popen.call_args
                actual_cwd = call_kwargs.kwargs.get("cwd") or (
                    call_kwargs[1].get("cwd") if call_kwargs[1] else None
                )
                self.assertEqual(actual_cwd, work_dir)


class TestRegistryValidate(unittest.TestCase):
    """Registry.validate() の worktree 対応テスト。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)
        self.registry = Registry(self.db)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)

    def test_validate_worktree_path(self):
        """worktree パスで validate が成功する（git_root がworktreeパスと一致）。"""
        with tempfile.TemporaryDirectory() as work_dir:
            subprocess.run(["git", "init", work_dir], check=True, capture_output=True)
            work_path = str(Path(work_dir).resolve())
            self.registry.register(
                "orchestrator-meta",
                work_path,
                skip_preflight=False,
            )
            result = self.registry.validate("orchestrator-meta")
            self.assertTrue(result["valid"])

    def test_expected_git_root_matches_worktree_not_main(self):
        """expected_git_root が main worktree のとき validate は失敗する。"""
        with tempfile.TemporaryDirectory() as main_dir:
            with tempfile.TemporaryDirectory() as work_dir:
                subprocess.run(["git", "init", main_dir], check=True, capture_output=True)
                subprocess.run(["git", "init", work_dir], check=True, capture_output=True)

                work_path = str(Path(work_dir).resolve())
                main_path = str(Path(main_dir).resolve())

                # path=.work, expected_git_root=main (意図的に不一致)
                self.db.create_repo(
                    "orchestrator-meta",
                    work_path,
                    expected_git_root=main_path,
                )
                with self.assertRaises(RegistryError):
                    self.registry.validate("orchestrator-meta")


if __name__ == "__main__":
    unittest.main()
