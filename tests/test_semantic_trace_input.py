from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.semantic_qa.trace_input import build_manifest


class SemanticTraceInputTests(unittest.TestCase):
    def _repo(self):
        temp = tempfile.TemporaryDirectory()
        repo = Path(temp.name)
        subprocess.run(["git", "init", "-q", repo], check=True)
        subprocess.run(["git", "-C", repo, "config", "user.name", "test"], check=True)
        subprocess.run(["git", "-C", repo, "config", "user.email", "test@example.invalid"], check=True)
        return temp, repo

    def _commit(self, repo: Path, message: str) -> str:
        subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", repo, "commit", "-q", "-m", message], check=True)
        return subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"], text=True).strip()

    def test_changed_patch_manifest_binds_exact_shas(self):
        temp, repo = self._repo()
        self.addCleanup(temp.cleanup)
        (repo / "a.py").write_text("def f():\n    return 1\n")
        base = self._commit(repo, "base")
        (repo / "a.py").write_text("def f():\n    return 2\n")
        head = self._commit(repo, "head")
        manifest = build_manifest(
            repo, base, head, repo / "trace", "owner/repo", "pr-1",
            max_files=20, max_total_bytes=262144, max_file_bytes=65536,
        )
        self.assertEqual(manifest["status"], "READY")
        self.assertEqual(manifest["base_sha"], base)
        self.assertEqual(manifest["candidate_sha"], head)

    def test_budget_overflow_is_degraded(self):
        temp, repo = self._repo()
        self.addCleanup(temp.cleanup)
        (repo / "a.txt").write_text("a\n")
        base = self._commit(repo, "base")
        (repo / "a.txt").write_text("x" * 200 + "\n")
        head = self._commit(repo, "head")
        manifest = build_manifest(
            repo, base, head, repo / "trace", "owner/repo", "pr-2",
            max_files=20, max_total_bytes=10, max_file_bytes=65536,
        )
        self.assertEqual(manifest["status"], "DEGRADED")
        self.assertEqual(manifest["skipped"][0]["reason"], "total_byte_budget")


if __name__ == "__main__":
    unittest.main()
