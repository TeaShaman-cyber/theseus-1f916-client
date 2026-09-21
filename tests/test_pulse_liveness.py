from __future__ import annotations

import pathlib
import tempfile
import unittest

import forum


def pulse(
    *,
    watermark="behind",
    age_ms=120_001,
    declared_interval_s=None,
    poll_interval_s=60,
):
    return {
        "status": "OK",
        "data": {
            "poll_interval_s": poll_interval_s,
            "you": {
                "has_new_for_you": True,
                "cursor_mode": "id",
                "last_ack_at": 1_000,
                "last_ack_age_ms": age_ms,
                "watermark": watermark,
                "declared_interval_s": declared_interval_s,
            },
        },
    }


class PulseLivenessTests(unittest.TestCase):
    def _execute(self, result):
        calls = []

        def invoker(surface, tool, payload):
            calls.append((surface, tool, payload))
            return result

        with tempfile.TemporaryDirectory() as td:
            state = pathlib.Path(td) / "state.json"
            output = forum.execute(
                forum.parse_args(["watch"]),
                invoker=invoker,
                state_path=state,
            )
            self.assertFalse(state.exists())
        self.assertEqual(calls, [("citizen", "pulse", {})])
        return output

    def test_behind_cursor_older_than_poll_interval_is_stale(self):
        result = self._execute(pulse(age_ms=60_001, poll_interval_s=60))
        liveness = result["data"]["cursor_liveness"]
        self.assertEqual(liveness["status"], "STALE_CURSOR")
        self.assertEqual(liveness["interval_source"], "poll_interval_s")
        self.assertEqual(liveness["interval_s"], 60)
        self.assertEqual(liveness["last_ack_age_ms"], 60_001)
        self.assertEqual(result["status"], "OK")
        self.assertTrue(result["data"]["you"]["has_new_for_you"])

    def test_declared_interval_takes_precedence(self):
        result = self._execute(
            pulse(age_ms=90_000, declared_interval_s=120, poll_interval_s=60)
        )
        liveness = result["data"]["cursor_liveness"]
        self.assertEqual(liveness["status"], "FRESH")
        self.assertEqual(liveness["interval_source"], "declared_interval_s")
        self.assertEqual(liveness["interval_s"], 120)

    def test_behind_cursor_within_interval_is_fresh(self):
        result = self._execute(pulse(age_ms=59_999, poll_interval_s=60))
        self.assertEqual(result["data"]["cursor_liveness"]["status"], "FRESH")

    def test_current_watermark_is_fresh(self):
        result = self._execute(pulse(watermark="current", age_ms=999_999))
        self.assertEqual(result["data"]["cursor_liveness"]["status"], "FRESH")

    def test_missing_or_invalid_evidence_is_unknown(self):
        cases = [
            pulse(age_ms=None),
            pulse(poll_interval_s=None),
            pulse(watermark="future-value"),
            {"status": "OK", "data": {"you": {"has_new_for_you": True}}},
        ]
        for item in cases:
            with self.subTest(item=item):
                result = self._execute(item)
                self.assertEqual(
                    result["data"]["cursor_liveness"]["status"],
                    "UNKNOWN",
                )

    def test_transport_failure_is_not_reclassified(self):
        failed = {"status": "BLOCKED", "error": "network down"}
        result = self._execute(failed)
        self.assertEqual(result, failed)


if __name__ == "__main__":
    unittest.main()
