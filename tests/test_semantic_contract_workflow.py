from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/semantic-contract-advisory.yml"


class SemanticContractWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text()

    def test_matrix_runs_both_advisory_tools(self):
        self.assertIn("tool: [semble, needle3]", self.text)
        self.assertIn("run_contract_advisory.py", self.text)
        self.assertIn("docs/qa/semantic-contract-corpus", self.text)

    def test_workflow_is_read_only_and_nonblocking(self):
        self.assertIn("contents: read", self.text)
        self.assertIn("actions: read", self.text)
        self.assertIn("cancel-in-progress: true", self.text)
        self.assertIn("exit 0", self.text)
        self.assertNotIn("issues: write", self.text)
        self.assertNotIn("pull-requests: write", self.text)

    def test_workflow_binds_exact_pr_head_and_python_312(self):
        self.assertIn("github.event.pull_request.head.sha || github.sha", self.text)
        self.assertIn('python-version: "3.12"', self.text)


if __name__ == "__main__":
    unittest.main()
