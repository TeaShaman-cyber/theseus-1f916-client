from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

import forum
import forum_liveness
import forum_state


def pulse(
    *,
    now=1_000_000,
    age_ms=121_000,
    watermark="behind",
    declared_interval_s=60,
    poll_interval_s=60,
    has_new=True,
):
    return {
        "now": now,
        "you": {
            "cursor_mode": "id",
            "last_ack_at": now - age_ms,
            "last_ack_age_ms": age_ms,
            "watermark": watermark,
            "declared_interval_s": declared_interval_s,
            "has_new_for_you": has_new,
        },
        "poll_interval_s": poll_interval_s,
    }


class CursorLivenessTests(unittest.TestCase):
    def test_stale_cursor_requires_behind_and_age_over_two_intervals(self):
        result = forum_liveness.classify_pulse(pulse())
        self.assertEqual(result["read_freshness"], "STALE_CURSOR")
        self.assertEqual(result["interval_s"], 60)
        self.assertEqual(result["interval_source"], "declared_interval_s")
        self.assertEqual(result["threshold_ms"], 120_000)
        self.assertFalse(result["wake_signal_usable"])

    def test_fresh_cursor_keeps_wake_signal_usable(self):
        result = forum_liveness.classify_pulse(pulse(age_ms=119_000))
        self.assertEqual(result["read_freshness"], "FRESH")
        self.assertTrue(result["wake_signal_usable"])

    def test_local_config_precedes_server_default_when_no_declared_interval(self):
        result = forum_liveness.classify_pulse(
            pulse(age_ms=500_000, declared_interval_s=None, poll_interval_s=60),
            configured_interval_s=300,
        )
        self.assertEqual(result["read_freshness"], "FRESH")
        self.assertEqual(result["interval_s"], 300)
        self.assertEqual(result["interval_source"], "client_config")

    def test_server_default_is_last_interval_fallback(self):
        result = forum_liveness.classify_pulse(
            pulse(age_ms=121_000, declared_interval_s=None, poll_interval_s=60)
        )
        self.assertEqual(result["read_freshness"], "STALE_CURSOR")
        self.assertEqual(result["interval_source"], "server_default_poll_interval_s")

    def test_missing_liveness_evidence_is_unknown_not_fresh(self):
        data = pulse()
        data["you"].pop("last_ack_age_ms")
        data["you"].pop("last_ack_at")
        result = forum_liveness.classify_pulse(data)
        self.assertEqual(result["read_freshness"], "UNKNOWN")
        self.assertFalse(result["wake_signal_usable"])

    def test_watch_persists_liveness_without_ack_or_cursor_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            liveness = root / "liveness.json"
            inbox_state = root / "inbox.json"
            calls = []

            def invoker(surface, tool, payload):
                calls.append((surface, tool, payload))
                return {"status": "OK", "data": pulse()}

            result = forum.execute(
                forum.parse_args(["watch"]),
                invoker=invoker,
                state_path=inbox_state,
                liveness_path=liveness,
            )
            self.assertEqual(result["status"], "OK")
            self.assertEqual(result["read_freshness"], "STALE_CURSOR")
            self.assertFalse(result["wake_signal_usable"])
            self.assertEqual(calls, [("citizen", "pulse", {})])
            self.assertFalse(inbox_state.exists())
            saved = json.loads(liveness.read_text())
            self.assertEqual(saved["read_freshness"], "STALE_CURSOR")
            self.assertEqual(saved["cursor_mode"], "id")
            self.assertEqual(saved["server_last_ack_at_ms"], 879_000)

    def test_verified_ack_records_liveness_without_claiming_freshness(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            state = root / "state.json"
            liveness = root / "liveness.json"
            offered = {
                "version": 1,
                "timestamp": 100,
                "comments": 10,
                "mentions": 20,
                "seal": "sealed",
            }
            forum_state.bank_inbox_page(
                state,
                {
                    "ack_cursor": offered,
                    "since_last_visit": {
                        "interval": {
                            "comments": {"after": 10},
                            "mentions": {"after": 20},
                        }
                    },
                },
                now_ms=1_000,
            )

            calls = []
            def invoker(surface, tool, payload):
                calls.append((surface, tool, payload))
                if tool == "me_ack":
                    return {"status": "OK", "data": {"advanced": True}}
                if tool == "me":
                    return {
                        "status": "OK",
                        "data": {
                            "since_last_visit": {
                                "interval": {
                                    "comments": {"after": 10},
                                    "mentions": {"after": 20},
                                }
                            }
                        },
                    }
                raise AssertionError(tool)

            result = forum.execute(
                forum.parse_args(["ack"]),
                invoker=invoker,
                state_path=state,
                liveness_path=liveness,
            )
            self.assertEqual(result["status"], "WRITE_VERIFIED")
            self.assertEqual(
                result["cursor_liveness"]["read_freshness"],
                "UNKNOWN_AFTER_VERIFIED_ACK",
            )
            saved = forum_liveness.load(liveness)
            self.assertEqual(saved["cursor_mode"], "id")
            self.assertIsInstance(saved["last_verified_ack_at_ms"], int)
            self.assertGreater(saved["last_verified_ack_at_ms"], 0)
            self.assertFalse(saved["wake_signal_usable"])
            self.assertEqual([tool for _, tool, _ in calls], ["me_ack", "me"])

    def test_verified_ack_time_is_durable_but_requires_new_pulse_for_freshness(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "liveness.json"
            forum_liveness.observe_pulse(path, pulse(age_ms=200_000), observed_at_ms=1_000_000)
            forum_liveness.record_verified_ack(path, cursor_mode="id", now_ms=1_100_000)
            saved = forum_liveness.load(path)
            self.assertEqual(saved["last_verified_ack_at_ms"], 1_100_000)
            self.assertEqual(saved["read_freshness"], "UNKNOWN_AFTER_VERIFIED_ACK")
            self.assertFalse(saved["wake_signal_usable"])


if __name__ == "__main__":
    unittest.main()
