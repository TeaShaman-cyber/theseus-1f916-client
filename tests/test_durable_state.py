import json
import pathlib
import tempfile
import unittest

import forum
import forum_state


def cursor(ts, comments, mentions, seal):
    return {
        "version": 1,
        "timestamp": ts,
        "comments": comments,
        "mentions": mentions,
        "seal": seal,
    }


def inbox_data(ack_cursor, marker):
    return {
        "ack_cursor": ack_cursor,
        "since_last_visit": {
            "replies": [{"id": marker, "body": f"reply-{marker}"}],
            "comments_on_your_posts": [],
            "in_threads_you_joined": [],
            "mentions_of_you": [],
            "interval": {"comments": {"after": ack_cursor["comments"]}, "mentions": {"after": ack_cursor["mentions"]}},
        },
    }


class DurableStateContractTests(unittest.TestCase):
    def test_missing_state_is_empty_and_local_summary_is_network_free(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            summary = forum_state.state_summary(path, now_ms=10_000)
            self.assertEqual(summary["state"], "EMPTY")
            self.assertEqual(summary["schema_version"], 1)
            self.assertEqual(summary["banked_reads"], 0)
            self.assertIsNone(summary["pending_ack"])

    def test_legacy_pending_ack_loads_as_recovery_required_not_ackable(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            old = cursor(100, 10, 20, "legacy-seal")
            path.write_text(json.dumps({"pending_ack": old}))

            state = forum_state.load_state(path)
            self.assertEqual(state["schema_version"], 1)
            self.assertIsNone(state["pending_ack"])
            self.assertEqual(state["banked_reads"], [])
            self.assertEqual(state["recovery"], {"kind": "legacy_pending_ack", "cursor": old})
            self.assertEqual(forum_state.state_summary(path, now_ms=10_000)["state"], "RECOVERY_REQUIRED")

    def test_canonical_state_rejects_pending_ack_not_backed_by_exact_bank_floor(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            first = cursor(100, 10, 20, "seal-first")
            later = cursor(120, 11, 21, "seal-later")
            forum_state.bank_inbox_page(path, inbox_data(first, 1), now_ms=1_000)
            raw = json.loads(path.read_text())
            raw["pending_ack"] = later
            path.write_text(json.dumps(raw))

            with self.assertRaisesRegex(forum_state.StateError, "exact banked cursor floor"):
                forum_state.load_state(path)

    def test_canonical_state_rejects_ack_without_banked_work(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            raw = {
                "schema_version": 1,
                "created_at_ms": 1_000,
                "updated_at_ms": 1_000,
                "pending_ack": cursor(100, 10, 20, "seal"),
                "banked_reads": [],
                "recovery": None,
            }
            path.write_text(json.dumps(raw))
            with self.assertRaisesRegex(forum_state.StateError, "without banked work"):
                forum_state.load_state(path)

    def test_corrupt_or_future_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            path.write_text("{not-json")
            with self.assertRaises(forum_state.StateError):
                forum_state.load_state(path)

            path.write_text(json.dumps({"schema_version": 999}))
            with self.assertRaisesRegex(forum_state.StateError, "unsupported state schema"):
                forum_state.load_state(path)

    def test_canonical_state_missing_required_field_fails_as_state_error(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            raw = {
                "schema_version": 1,
                "created_at_ms": 1_000,
                "updated_at_ms": 1_000,
                "pending_ack": None,
                "banked_reads": [],
            }
            path.write_text(json.dumps(raw))

            with self.assertRaises(forum_state.StateError):
                forum_state.load_state(path)

    def test_canonical_state_rejects_unsupported_recovery_kind(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            raw = {
                "schema_version": 1,
                "created_at_ms": 1_000,
                "updated_at_ms": 1_000,
                "pending_ack": None,
                "banked_reads": [],
                "recovery": {"kind": "mystery", "cursor": cursor(100, 10, 20, "seal")},
            }
            path.write_text(json.dumps(raw))

            with self.assertRaises(forum_state.StateError):
                forum_state.load_state(path)

    def test_canonical_recovery_rejects_each_ackable_work_component(self):
        recovery_cursor = cursor(100, 10, 20, "legacy-seal")
        recovery = {"kind": "legacy_pending_ack", "cursor": recovery_cursor}
        cases = (
            ("pending_ack", recovery_cursor, []),
            (
                "banked_reads",
                None,
                [
                    {
                        "banked_at_ms": 1_000,
                        "ack_cursor": recovery_cursor,
                        "since_last_visit": {},
                    }
                ],
            ),
        )

        for name, pending_ack, banked_reads in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as td:
                path = pathlib.Path(td) / "state.json"
                raw = {
                    "schema_version": 1,
                    "created_at_ms": 1_000,
                    "updated_at_ms": 1_000,
                    "pending_ack": pending_ack,
                    "banked_reads": banked_reads,
                    "recovery": recovery,
                }
                path.write_text(json.dumps(raw))

                with self.assertRaises(forum_state.StateError):
                    forum_state.load_state(path)

    def test_canonical_recovery_state_loads_and_reports_recovery_required(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            recovery_cursor = cursor(100, 10, 20, "legacy-seal")
            raw = {
                "schema_version": 1,
                "created_at_ms": 1_000,
                "updated_at_ms": 1_000,
                "pending_ack": None,
                "banked_reads": [],
                "recovery": {"kind": "legacy_pending_ack", "cursor": recovery_cursor},
            }
            path.write_text(json.dumps(raw))

            state = forum_state.load_state(path)
            self.assertEqual(state["recovery"], raw["recovery"])
            self.assertEqual(forum_state.state_summary(path, now_ms=2_000)["state"], "RECOVERY_REQUIRED")

    def test_bank_inbox_page_persists_exact_work_and_exposes_age(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            offered = cursor(100, 10, 20, "seal-a")
            data = inbox_data(offered, 7)

            state = forum_state.bank_inbox_page(path, data, now_ms=1_000)
            self.assertEqual(state["pending_ack"], offered)
            self.assertEqual(len(state["banked_reads"]), 1)
            self.assertEqual(state["banked_reads"][0]["ack_cursor"], offered)
            self.assertEqual(state["banked_reads"][0]["since_last_visit"], data["since_last_visit"])
            self.assertEqual(state["banked_reads"][0]["banked_at_ms"], 1_000)
            self.assertIsNone(state["recovery"])

            summary = forum_state.state_summary(path, now_ms=6_500)
            self.assertEqual(summary["state"], "PENDING")
            self.assertEqual(summary["banked_reads"], 1)
            self.assertEqual(summary["oldest_age_seconds"], 5.5)
            self.assertEqual(summary["pending_ack"], offered)

    def test_bank_without_explicit_clock_uses_now_for_new_page_and_updated_at(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            first = cursor(100, 10, 20, "seal-first")
            later = cursor(120, 11, 21, "seal-later")
            forum_state.bank_inbox_page(path, inbox_data(first, 1), now_ms=1_000)

            with mock.patch("forum_state._now_ms", return_value=9_000):
                state = forum_state.bank_inbox_page(path, inbox_data(later, 2))

            self.assertEqual(state["updated_at_ms"], 9_000)
            self.assertEqual(state["banked_reads"][-1]["banked_at_ms"], 9_000)
            self.assertEqual(forum_state.load_state(path)["updated_at_ms"], 9_000)

    def test_fresh_bank_recovery_reset_starts_new_state_at_bank_time(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            legacy = cursor(500, 50, 50, "legacy")
            path.write_text(json.dumps({"pending_ack": legacy}))
            offered = cursor(400, 40, 40, "fresh")

            state = forum_state.bank_inbox_page(path, inbox_data(offered, 3), now_ms=7_000)

            self.assertEqual(state["created_at_ms"], 7_000)
            self.assertEqual(state["updated_at_ms"], 7_000)
            self.assertEqual(forum_state.load_state(path)["created_at_ms"], 7_000)

    def test_second_bank_keeps_exact_safe_floor_and_banks_both_pages(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            floor = cursor(100, 10, 20, "seal-floor")
            later = cursor(120, 10, 20, "seal-later")
            forum_state.bank_inbox_page(path, inbox_data(floor, 1), now_ms=1_000)
            state = forum_state.bank_inbox_page(path, inbox_data(later, 2), now_ms=2_000)
            self.assertEqual(state["pending_ack"], floor)
            self.assertEqual([r["ack_cursor"] for r in state["banked_reads"]], [floor, later])

    def test_incomparable_offer_fails_without_overwriting_prior_state(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            first = cursor(100, 50, 20, "seal-first")
            second = cursor(120, 45, 25, "seal-second")
            forum_state.bank_inbox_page(path, inbox_data(first, 1), now_ms=1_000)
            before = path.read_bytes()
            with self.assertRaisesRegex(forum_state.StateError, "not safely ordered"):
                forum_state.bank_inbox_page(path, inbox_data(second, 2), now_ms=2_000)
            self.assertEqual(path.read_bytes(), before)

    def test_fresh_bank_replaces_legacy_recovery_with_current_durable_page(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            legacy = cursor(500, 50, 50, "legacy")
            path.write_text(json.dumps({"pending_ack": legacy}))
            offered = cursor(400, 40, 40, "fresh")

            state = forum_state.bank_inbox_page(path, inbox_data(offered, 3), now_ms=2_000)
            self.assertIsNone(state["recovery"])
            self.assertEqual(state["pending_ack"], offered)
            self.assertEqual(len(state["banked_reads"]), 1)

    def test_forum_state_command_never_invokes_transport(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            forum_state.bank_inbox_page(path, inbox_data(cursor(100, 10, 20, "seal"), 4), now_ms=1_000)

            result = forum.execute(
                forum.parse_args(["state"]),
                invoker=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network must not run")),
                state_path=path,
            )
            self.assertEqual(result["status"], "OK")
            self.assertEqual(result["data"]["state"], "PENDING")
            self.assertEqual(result["data"]["banked_reads"], 1)

    def test_replay_pending_inbox_returns_exact_single_banked_page(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            offered = cursor(100, 10, 20, "seal")
            data = inbox_data(offered, 7)
            forum_state.bank_inbox_page(path, data, now_ms=1_000)

            replay = forum_state.replay_pending_inbox(path)

            self.assertEqual(replay["ack_cursor"], offered)
            self.assertEqual(replay["since_last_visit"], data["since_last_visit"])
            self.assertEqual(replay["replay_source"], "durable_bank")
            self.assertEqual(replay["banked_at_ms"], 1_000)

    def test_replay_pending_inbox_refuses_empty_or_recovery_state(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            with self.assertRaisesRegex(forum_state.StateError, "no durably banked inbox work"):
                forum_state.replay_pending_inbox(path)

            legacy = cursor(100, 10, 20, "legacy")
            path.write_text(json.dumps({"pending_ack": legacy}))
            with self.assertRaisesRegex(forum_state.StateError, "recovery is required"):
                forum_state.replay_pending_inbox(path)

    def test_replay_pending_inbox_refuses_multiple_banked_pages(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            first = cursor(100, 10, 20, "first")
            second = cursor(120, 11, 21, "second")
            forum_state.bank_inbox_page(path, inbox_data(first, 1), now_ms=1_000)
            forum_state.bank_inbox_page(path, inbox_data(second, 2), now_ms=2_000)

            with self.assertRaisesRegex(forum_state.StateError, "multiple durably banked inbox pages"):
                forum_state.replay_pending_inbox(path)

    def test_ack_refuses_recovery_only_state_without_transport(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            path.write_text(json.dumps({"pending_ack": cursor(100, 10, 20, "legacy")}))
            result = forum.execute(
                forum.parse_args(["ack"]),
                invoker=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("ack must not run")),
                state_path=path,
            )
            self.assertEqual(result["status"], "BLOCKED")
            self.assertIn("recovery", result["error"].lower())

    def test_atomic_bank_failure_does_not_replace_prior_state(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            first = cursor(100, 10, 20, "seal-first")
            forum_state.bank_inbox_page(path, inbox_data(first, 1), now_ms=1_000)
            before = path.read_bytes()

            second = cursor(120, 10, 20, "seal-second")
            with mock.patch("forum_state.os.replace", side_effect=OSError("simulated crash before replace")):
                with self.assertRaises(OSError):
                    forum_state.bank_inbox_page(path, inbox_data(second, 2), now_ms=2_000)

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.glob(f".{path.name}.tmp-*")), [])

    def test_verified_remote_ack_with_local_commit_failure_keeps_old_bank(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            floor = cursor(100, 10, 20, "seal-floor")
            later = cursor(120, 11, 21, "seal-later")
            forum_state.bank_inbox_page(path, inbox_data(floor, 1), now_ms=1_000)
            forum_state.bank_inbox_page(path, inbox_data(later, 2), now_ms=2_000)
            before = path.read_bytes()

            def invoker(server, tool, payload):
                if tool == "me_ack":
                    self.assertEqual(payload, {"up_to": floor})
                    return {"status": "OK", "data": {"ok": True}}
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

            with mock.patch("forum_state.os.replace", side_effect=OSError("simulated local commit crash")):
                result = forum.execute(
                    forum.parse_args(["ack"]),
                    invoker=invoker,
                    state_path=path,
                )

            self.assertEqual(result["status"], "RECOVERABLE")
            self.assertIn("verified remotely", result["error"] )
            self.assertEqual(path.read_bytes(), before)

    def test_bank_ignores_unrelated_top_level_fields_and_never_persists_credentials(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            offered = cursor(100, 10, 20, "seal")
            data = inbox_data(offered, 9)
            data["secret"] = "must-not-enter-state"
            data["handle"] = "jester-sonar"

            forum_state.bank_inbox_page(path, data, now_ms=1_000)
            text = path.read_text()
            self.assertNotIn("must-not-enter-state", text)
            self.assertNotIn('"secret"', text)
            self.assertNotIn('"handle"', text)

    def test_existing_empty_v1_state_summary_reports_empty(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "created_at_ms": 1_000,
                        "updated_at_ms": 1_000,
                        "pending_ack": None,
                        "banked_reads": [],
                        "recovery": None,
                    }
                )
            )
            summary = forum_state.state_summary(path, now_ms=2_000)
            self.assertEqual(summary["state"], "EMPTY")
            self.assertEqual(summary["banked_reads"], 0)

    def test_verified_ack_recomputes_minimum_across_multiple_remaining_pages(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            first = cursor(100, 10, 20, "seal-first")
            second = cursor(120, 11, 21, "seal-second")
            third = cursor(140, 12, 22, "seal-third")
            forum_state.bank_inbox_page(path, inbox_data(first, 1), now_ms=1_000)
            forum_state.bank_inbox_page(path, inbox_data(second, 2), now_ms=2_000)
            forum_state.bank_inbox_page(path, inbox_data(third, 3), now_ms=3_000)

            state = forum_state.commit_verified_ack(path, first, now_ms=4_000)
            self.assertEqual(state["pending_ack"], second)
            self.assertEqual(
                [row["ack_cursor"] for row in state["banked_reads"]],
                [second, third],
            )

    def test_commit_verified_ack_refuses_recovery_state_directly(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            legacy = cursor(100, 10, 20, "legacy")
            path.write_text(json.dumps({"pending_ack": legacy}))
            before = path.read_bytes()

            with self.assertRaises(forum_state.StateError):
                forum_state.commit_verified_ack(path, legacy, now_ms=2_000)

            self.assertEqual(path.read_bytes(), before)

    def test_commit_verified_ack_updates_timestamp_from_supplied_clock(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            floor = cursor(100, 10, 20, "seal-floor")
            later = cursor(120, 11, 21, "seal-later")
            forum_state.bank_inbox_page(path, inbox_data(floor, 1), now_ms=1_000)
            forum_state.bank_inbox_page(path, inbox_data(later, 2), now_ms=2_000)

            state = forum_state.commit_verified_ack(path, floor, now_ms=9_000)

            self.assertEqual(state["updated_at_ms"], 9_000)
            self.assertEqual(forum_state.load_state(path)["updated_at_ms"], 9_000)

    def test_verified_floor_ack_prunes_only_covered_banked_reads(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            floor = cursor(100, 10, 20, "seal-floor")
            later = cursor(120, 11, 21, "seal-later")
            forum_state.bank_inbox_page(path, inbox_data(floor, 1), now_ms=1_000)
            forum_state.bank_inbox_page(path, inbox_data(later, 2), now_ms=2_000)

            state = forum_state.commit_verified_ack(path, floor, now_ms=3_000)
            self.assertIsNotNone(state)
            self.assertEqual(state["pending_ack"], later)
            self.assertEqual(len(state["banked_reads"]), 1)
            self.assertEqual(state["banked_reads"][0]["ack_cursor"], later)

            self.assertIsNone(forum_state.commit_verified_ack(path, later, now_ms=4_000))
            self.assertFalse(path.exists())

    def test_ack_edge_rate_limit_is_not_executed_and_keeps_banked_state(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            offered = cursor(100, 10, 20, "seal")
            forum_state.bank_inbox_page(path, inbox_data(offered, 1), now_ms=1_000)
            before = path.read_bytes()

            result = forum.execute(
                forum.parse_args(["ack"]),
                invoker=lambda *args, **kwargs: {
                    "status": "RATE_LIMITED",
                    "error": "429",
                    "route": "forum-citizen.me_ack",
                },
                state_path=path,
            )
            self.assertEqual(result["status"], "RATE_LIMITED")
            self.assertEqual(result["delivery_state"], "not_executed")
            self.assertEqual(result["operation"], "ack")
            self.assertEqual(path.read_bytes(), before)

    def test_ack_unverified_readback_keeps_banked_state(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            offered = cursor(100, 10, 20, "seal")
            forum_state.bank_inbox_page(path, inbox_data(offered, 1), now_ms=1_000)
            before = path.read_bytes()
            calls = []

            def invoker(server, tool, payload):
                calls.append((server, tool, payload))
                if tool == "me_ack":
                    return {"status": "OK", "data": {"ok": True}}
                if tool == "me":
                    return {
                        "status": "OK",
                        "data": {
                            "since_last_visit": {
                                "interval": {
                                    "comments": {"after": 9},
                                    "mentions": {"after": 19},
                                }
                            }
                        },
                    }
                raise AssertionError(tool)

            result = forum.execute(forum.parse_args(["ack"]), invoker=invoker, state_path=path)
            self.assertEqual(result["status"], "RECOVERABLE")
            self.assertIn("did not prove progress", result["error"] )
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual([tool for _, tool, _ in calls], ["me_ack", "me"])


if __name__ == "__main__":
    unittest.main()
