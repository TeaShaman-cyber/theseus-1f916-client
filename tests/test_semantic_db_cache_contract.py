from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SemanticDbCacheContractTests(unittest.TestCase):
    def setUp(self):
        self.profile = json.loads((ROOT / "tools/semantic_qa/profile.json").read_text())
        self.advisory = (ROOT / ".github/workflows/semantic-semdup-advisory.yml").read_text()
        self.seed = (ROOT / ".github/workflows/semantic-semdup-seed.yml").read_text()

    def test_profile_uses_exact_base_cache_contract(self):
        cache = self.profile["semdup"]["db_cache"]
        self.assertEqual(cache["schema"], 3)
        self.assertEqual(cache["pr_restore"], "exact-base-sha-only")
        self.assertEqual(cache["save_authority"], "trusted-default-branch-push-only")

    def test_pr_restores_without_saving(self):
        self.assertIn("actions/cache/restore@", self.advisory)
        self.assertNotIn("actions/cache/save@", self.advisory)
        self.assertIn("exact base cache missing; cold embedding skipped by policy", self.advisory)

    def test_seed_saves_only_from_main_workflow(self):
        self.assertIn("branches: [main]", self.seed)
        self.assertIn("github.ref == 'refs/heads/main'", self.seed)
        self.assertIn("actions/cache/save@", self.seed)
        self.assertIn("actions: write", self.seed)
        self.assertIn("timeout-minutes: 45", self.seed)
        self.assertIn("Extract exact main corpus", self.seed)
        self.assertIn("Embed pending main corpus", self.seed)
        self.assertIn("Finalize seed receipt", self.seed)
        self.assertNotIn("ci --mode scan", self.seed)


if __name__ == "__main__":
    unittest.main()
