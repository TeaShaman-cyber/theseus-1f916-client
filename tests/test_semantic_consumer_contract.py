from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SemanticConsumerContractTests(unittest.TestCase):
    def test_profile_pins_cross_repo_package_and_source(self):
        profile = json.loads((ROOT / "tools/semantic_qa/profile.json").read_text())
        semdup = profile["semdup"]
        package = semdup["package"]
        self.assertEqual(package["repository"], "TeaShaman-cyber/theseus-research")
        self.assertEqual(package["artifact_id"], 10614095162)
        self.assertEqual(
            package["tar_sha256"],
            "58894995a4a087be743a3b802a31df05677902c3b6829775382538b1a65bbbdb",
        )
        self.assertEqual(
            semdup["source_sha"],
            "829f90ad94f453b73a24e46413202e9ece0b49e8",
        )

    def test_workflow_is_advisory_read_only_and_cancellable(self):
        workflow = (ROOT / ".github/workflows/semantic-semdup-advisory.yml").read_text()
        self.assertIn("contents: read", workflow)
        self.assertIn("actions: read", workflow)
        self.assertIn("cancel-in-progress: true", workflow)
        self.assertIn("exit 0", workflow)
        self.assertNotIn("issues: write", workflow)
        self.assertNotIn("pull-requests: write", workflow)


if __name__ == "__main__":
    unittest.main()
