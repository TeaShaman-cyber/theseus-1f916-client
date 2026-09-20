from __future__ import annotations

import unittest
from datetime import datetime, timezone

from tools.semantic_qa.package_freshness import evaluate_freshness


class PackageFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 20, tzinfo=timezone.utc)

    def test_upstream_move_is_stale_not_unavailable(self):
        result = evaluate_freshness(
            now=self.now,
            built_at="2026-09-19T00:00:00Z",
            expires_at="2026-12-19T00:00:00Z",
            max_age_days=30,
            expiry_warning_days=14,
            pinned_git_sha="a" * 40,
            current_git_sha="b" * 40,
        )
        self.assertEqual(result["status"], "STALE_AVAILABLE")

    def test_currentness_failure_is_unknown(self):
        result = evaluate_freshness(
            now=self.now,
            built_at="2026-09-19T00:00:00Z",
            expires_at="2026-12-19T00:00:00Z",
            max_age_days=30,
            expiry_warning_days=14,
            currentness_errors=["network unavailable"],
        )
        self.assertEqual(result["status"], "CURRENTNESS_UNKNOWN")


if __name__ == "__main__":
    unittest.main()
