import json
import pathlib
import tempfile
import unittest

import forum
import forum_ledger
import forum_state


def cursor(ts=100, comments=10, mentions=20, seal="seal"):
    return {
        "version": 1,
        "timestamp": ts,
        "comments": comments,
        "mentions": mentions,
        "seal": seal,
    }


class ReconciliationContractTests(unittest.TestCase):
    def _begin(self, path, operation, intent, state="COMPLETED", evidence=None):
        forum_ledger.begin_operation(
            path,
            operation=operation,
            intent=intent,
            now_ms=1_000,
            operation_id="op-1",
        )
        if state == "COMPLETED":
            forum_ledger.transition_operation(
                path,
                "op-1",
                "COMPLETED",
                evidence=evidence or {},
                now_ms=2_000,
            )
        elif state == "RECOVERABLE":
            forum_ledger.transition_operation(
                path,
                "op-1",
                "RECOVERABLE",
                evidence=evidence or {},
                error="ambiguous",
                now_ms=2_000,
            )
        elif state != "ATTEMPTED":
            raise AssertionError(state)

    def test_reconcile_completed_comment_exact_match_marks_verified(self):
        with tempfile.TemporaryDirectory() as td:
            operations = pathlib.Path(td) / "operations.json"
            self._begin(
                operations,
                "comment",
                {"post_id": 6108, "body": "hello", "parent_id": 71153},
                evidence={"write_id": 4000},
            )
            calls = []

            def invoker(server, tool, payload):
                calls.append((server, tool, payload))
                self.assertEqual(tool, "read_comment")
                return {
                    "status": "OK",
                    "data": {
                        "comment": {
                            "id": 4000,
                            "post_id": 6108,
                            "parent_id": 71153,
                            "body": "hello",
                        }
                    },
                }

            result = forum.execute(
                forum.parse_args(["reconcile", "op-1"]),
                invoker=invoker,
                operations_path=operations,
            )
            self.assertEqual(result["status"], "OK")
            self.assertEqual(result["delivery_state"], "recovered_match")
            self.assertEqual([tool for _, tool, _ in calls], ["read_comment"])
            record = forum_ledger.get_operation(operations, "op-1")
            self.assertEqual(record["state"], "VERIFIED")
            self.assertEqual(record["evidence"]["readback_id"], 4000)

    def test_reconcile_exact_object_conflict_records_contradiction(self):
        with tempfile.TemporaryDirectory() as td:
            operations = pathlib.Path(td) / "operations.json"
            self._begin(
                operations,
                "post",
                {"title": "Expected", "body": "Body"},
                state="RECOVERABLE",
                evidence={"write_id": 3000},
            )

            result = forum.execute(
                forum.parse_args(["reconcile", "op-1"]),
                invoker=lambda *args: {
                    "status": "OK",
                    "data": {
                        "post": {
                            "id": 3000,
                            "title": "Different",
                            "body": "Body",
                            "url": None,
                        }
                    },
                },
                operations_path=operations,
            )
            self.assertEqual(result["status"], "OK")
            self.assertEqual(result["delivery_state"], "contradiction")
            record = forum_ledger.get_operation(operations, "op-1")
            self.assertEqual(record["state"], "RECOVERABLE")
            history = record["evidence"]["reconciliation_history"][-1]
            self.assertEqual(history["delivery_state"], "contradiction")
            self.assertIn("title", history["evidence"]["mismatched_fields"])
            self.assertEqual(len(history["evidence"]["expected_sha256"]), 64)
            self.assertEqual(len(history["evidence"]["observed_sha256"]), 64)
            self.assertNotIn("expected", history["evidence"])
            self.assertNotIn("observed", history["evidence"])

    def test_reconcile_unavailable_exact_read_is_unknown_and_not_replayed(self):
        with tempfile.TemporaryDirectory() as td:
            operations = pathlib.Path(td) / "operations.json"
            self._begin(
                operations,
                "comment",
                {"post_id": 6108, "body": "hello"},
                state="RECOVERABLE",
                evidence={"write_id": 4000},
            )
            calls = []

            def invoker(server, tool, payload):
                calls.append((server, tool, payload))
                return {"status": "RATE_LIMITED", "error": "readback 429"}

            result = forum.execute(
                forum.parse_args(["reconcile", "op-1"]),
                invoker=invoker,
                operations_path=operations,
            )
            self.assertEqual(result["status"], "RATE_LIMITED")
            self.assertEqual(result["delivery_state"], "unknown")
            self.assertEqual([tool for _, tool, _ in calls], ["read_comment"])
            record = forum_ledger.get_operation(operations, "op-1")
            self.assertEqual(record["state"], "RECOVERABLE")

    def test_reconcile_attempted_without_write_id_is_unknown_without_network(self):
        with tempfile.TemporaryDirectory() as td:
            operations = pathlib.Path(td) / "operations.json"
            self._begin(
                operations,
                "comment",
                {"post_id": 6108, "body": "hello"},
                state="ATTEMPTED",
            )
            result = forum.execute(
                forum.parse_args(["reconcile", "op-1"]),
                invoker=lambda *args: (_ for _ in ()).throw(
                    AssertionError("partial search/read must not run without exact write id")
                ),
                operations_path=operations,
            )
            self.assertEqual(result["status"], "OK")
            self.assertEqual(result["delivery_state"], "unknown")
            self.assertIn("no exact write id", result["reason"])
            self.assertEqual(forum_ledger.get_operation(operations, "op-1")["state"], "ATTEMPTED")

    def test_reconcile_ack_proven_progress_commits_bank_and_marks_verified(self):
        with tempfile.TemporaryDirectory() as td:
            operations = pathlib.Path(td) / "operations.json"
            state = pathlib.Path(td) / "state.json"
            offered = cursor()
            forum_state.bank_inbox_page(
                state,
                {"ack_cursor": offered, "since_last_visit": {}},
                now_ms=500,
            )
            self._begin(
                operations,
                "ack",
                {"up_to": offered},
                state="RECOVERABLE",
            )
            calls = []

            def invoker(server, tool, payload):
                calls.append((server, tool, payload))
                self.assertEqual(tool, "me")
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

            result = forum.execute(
                forum.parse_args(["reconcile", "op-1"]),
                invoker=invoker,
                state_path=state,
                operations_path=operations,
            )
            self.assertEqual(result["status"], "OK")
            self.assertEqual(result["delivery_state"], "recovered_match")
            self.assertFalse(state.exists())
            self.assertEqual(forum_ledger.get_operation(operations, "op-1")["state"], "VERIFIED")
            self.assertEqual([tool for _, tool, _ in calls], ["me"])

    def test_reconcile_vote_remains_unknown_without_identity_safe_proof(self):
        with tempfile.TemporaryDirectory() as td:
            operations = pathlib.Path(td) / "operations.json"
            self._begin(
                operations,
                "vote",
                {"target_type": "comment", "target_id": 24315, "before_votes": 3},
                state="RECOVERABLE",
            )
            result = forum.execute(
                forum.parse_args(["reconcile", "op-1"]),
                invoker=lambda *args: (_ for _ in ()).throw(
                    AssertionError("vote count alone is not identity-safe reconciliation")
                ),
                operations_path=operations,
            )
            self.assertEqual(result["status"], "OK")
            self.assertEqual(result["delivery_state"], "unknown")
            self.assertIn("vote identity", result["reason"])

    def test_reconcile_terminal_operation_is_local_noop(self):
        with tempfile.TemporaryDirectory() as td:
            operations = pathlib.Path(td) / "operations.json"
            self._begin(
                operations,
                "comment",
                {"post_id": 6108, "body": "hello"},
                evidence={"write_id": 4000},
            )
            forum_ledger.transition_operation(
                operations,
                "op-1",
                "VERIFIED",
                evidence={"readback_id": 4000},
                now_ms=3_000,
            )
            result = forum.execute(
                forum.parse_args(["reconcile", "op-1"]),
                invoker=lambda *args: (_ for _ in ()).throw(
                    AssertionError("terminal reconciliation must not use network")
                ),
                operations_path=operations,
            )
            self.assertEqual(result["status"], "OK")
            self.assertEqual(result["delivery_state"], "already_terminal")
            self.assertEqual(result["operation_state"], "VERIFIED")

    def test_reconcile_never_invokes_write_tools(self):
        with tempfile.TemporaryDirectory() as td:
            operations = pathlib.Path(td) / "operations.json"
            self._begin(
                operations,
                "post",
                {"title": "T", "body": "B"},
                evidence={"write_id": 3000},
            )

            def invoker(server, tool, payload):
                self.assertEqual(server, "read")
                self.assertEqual(tool, "read_post")
                return {
                    "status": "OK",
                    "data": {"post": {"id": 3000, "title": "T", "body": "B", "url": None}},
                }

            result = forum.execute(
                forum.parse_args(["reconcile", "op-1"]),
                invoker=invoker,
                operations_path=operations,
            )
            self.assertEqual(result["delivery_state"], "recovered_match")

    def test_reconcile_attempted_ack_can_be_verified_without_resend(self):
        with tempfile.TemporaryDirectory() as td:
            operations = pathlib.Path(td) / "operations.json"
            state = pathlib.Path(td) / "state.json"
            offered = cursor()
            forum_state.bank_inbox_page(
                state,
                {"ack_cursor": offered, "since_last_visit": {}},
                now_ms=500,
            )
            self._begin(
                operations,
                "ack",
                {"up_to": offered},
                state="ATTEMPTED",
            )
            calls = []

            def invoker(server, tool, payload):
                calls.append((server, tool, payload))
                self.assertEqual((server, tool), ("citizen", "me"))
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

            result = forum.execute(
                forum.parse_args(["reconcile", "op-1"]),
                invoker=invoker,
                state_path=state,
                operations_path=operations,
            )
            self.assertEqual(result["delivery_state"], "recovered_match")
            self.assertEqual([tool for _, tool, _ in calls], ["me"])
            record = forum_ledger.get_operation(operations, "op-1")
            self.assertEqual(record["state"], "VERIFIED")
            self.assertFalse(record["auto_replay_allowed"])

    def test_unknown_and_contradiction_never_enable_auto_replay(self):
        with tempfile.TemporaryDirectory() as td:
            operations = pathlib.Path(td) / "operations.json"
            self._begin(
                operations,
                "post",
                {"title": "Expected", "body": "Body"},
                state="RECOVERABLE",
                evidence={"write_id": 3000},
            )
            contradiction = forum.execute(
                forum.parse_args(["reconcile", "op-1"]),
                invoker=lambda *args: {
                    "status": "OK",
                    "data": {"post": {"id": 3000, "title": "Other", "body": "Body", "url": None}},
                },
                operations_path=operations,
            )
            self.assertEqual(contradiction["delivery_state"], "contradiction")
            self.assertFalse(forum_ledger.get_operation(operations, "op-1")["auto_replay_allowed"])

        with tempfile.TemporaryDirectory() as td:
            operations = pathlib.Path(td) / "operations.json"
            self._begin(
                operations,
                "comment",
                {"post_id": 6108, "body": "hello"},
                state="ATTEMPTED",
            )
            unknown = forum.execute(
                forum.parse_args(["reconcile", "op-1"]),
                invoker=lambda *args: (_ for _ in ()).throw(AssertionError("no network")),
                operations_path=operations,
            )
            self.assertEqual(unknown["delivery_state"], "unknown")
            self.assertFalse(forum_ledger.get_operation(operations, "op-1")["auto_replay_allowed"])

    def test_reconcile_remote_match_with_ledger_write_failure_stays_unresolved(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            operations = pathlib.Path(td) / "operations.json"
            self._begin(
                operations,
                "comment",
                {"post_id": 6108, "body": "hello"},
                evidence={"write_id": 4000},
            )
            before = operations.read_bytes()
            with mock.patch(
                "forum_ledger._atomic_write",
                side_effect=OSError("simulated reconciliation fsync failure"),
            ):
                result = forum.execute(
                    forum.parse_args(["reconcile", "op-1"]),
                    invoker=lambda *args: {
                        "status": "OK",
                        "data": {
                            "comment": {
                                "id": 4000,
                                "post_id": 6108,
                                "parent_id": None,
                                "body": "hello",
                            }
                        },
                    },
                    operations_path=operations,
                )
            self.assertEqual(result["status"], "BLOCKED")
            self.assertEqual(result["delivery_state"], "recovered_match")
            self.assertIn("ledger_error", result)
            self.assertEqual(operations.read_bytes(), before)
            self.assertEqual(forum_ledger.get_operation(operations, "op-1")["state"], "COMPLETED")

    def test_reconcile_ack_after_local_bank_already_committed_can_finish_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            operations = pathlib.Path(td) / "operations.json"
            state = pathlib.Path(td) / "state.json"
            offered = cursor()
            self._begin(
                operations,
                "ack",
                {"up_to": offered},
                state="RECOVERABLE",
            )
            self.assertFalse(state.exists())
            result = forum.execute(
                forum.parse_args(["reconcile", "op-1"]),
                invoker=lambda server, tool, payload: {
                    "status": "OK",
                    "data": {
                        "since_last_visit": {
                            "interval": {
                                "comments": {"after": 10},
                                "mentions": {"after": 20},
                            }
                        }
                    },
                },
                state_path=state,
                operations_path=operations,
            )
            self.assertEqual(result["delivery_state"], "recovered_match")
            self.assertEqual(forum_ledger.get_operation(operations, "op-1")["state"], "VERIFIED")


if __name__ == "__main__":
    unittest.main()
