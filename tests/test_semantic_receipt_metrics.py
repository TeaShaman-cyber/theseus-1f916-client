from __future__ import annotations

import unittest

from tools.semantic_qa.run_semdup_advisory import _finding_count


class SemanticReceiptMetricsTests(unittest.TestCase):
    def test_finding_count_handles_list_payload(self):
        self.assertEqual(_finding_count([{"a": 1}, {"a": 2}]), 2)

    def test_finding_count_handles_common_dict_shapes(self):
        self.assertEqual(_finding_count({"findings": [1, 2, 3]}), 3)
        self.assertEqual(_finding_count({"pairs": [1]}), 1)
        self.assertEqual(_finding_count({"results": []}), 0)

    def test_finding_count_fails_closed_for_unknown_shape(self):
        self.assertEqual(_finding_count({"unexpected": [1, 2]}), 0)
        self.assertEqual(_finding_count("not-json-shape"), 0)


if __name__ == "__main__":
    unittest.main()
