import json
import pathlib
import tempfile
import unittest
from unittest import mock

import forum
import forum_ledger


class ExecutionLedgerContractTests(unittest.TestCase):
    def test_missing_ledger_is_empty_and_operations_summary_is_network_free(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "operations.json"
            summary = forum_ledger.operations_summary(path, now_ms=10_000)
            self.assertEqual(summary["schema_version"], 1)
            self.assertEqual(summary["operations"], 0)
            self.assertEqual(summary["unresolved"], 0)
            self.assertEqual(summary["states"], {})

            result = forum.execute(
                forum.parse_args(["operations"]),
                invoker=lambda *args, **kwargs: (_ for _ in ()).throw(
                    AssertionError("operations must not use network")
                ),
                operations_path=path,
            )
            self.assertEqual(result["status"], "OK")
            self.assertEqual(result["data"]["operations"], 0)

    def test_begin_operation_is_durable_secret_free_and_not_replayable(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "operations.json"
            record = forum_ledger.begin_operation(
                path,
                operation="comment",
                intent={"post_id": 6108, "body": "hello"},
                now_ms=1_000,
                operation_id="op-1",
            )
            self.assertEqual(record["state"], "ATTEMPTED")
            self.assertFalse(record["auto_replay_allowed"])
            self.assertEqual(record["intent"], {"post_id": 6108, "body": "hello"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            raw = json.loads(path.read_text())
            self.assertEqual(raw["schema_version"], 1)
            self.assertEqual(raw["operations"][0]["id"], "op-1")
            text = path.read_text()
            self.assertNotIn("bearer", text.lower())
            self.assertNotIn("authorization", text.lower())

    def test_transition_states_are_monotone_and_verified_is_terminal(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "operations.json"
            forum_ledger.begin_operation(
                path,
                operation="post",
                intent={"title": "T", "body": "B"},
                now_ms=1_000,
                operation_id="op-1",
            )
            completed = forum_ledger.transition_operation(
                path,
                "op-1",
                "COMPLETED",
                evidence={"route": "forum-citizen.post", "post_id": 3000},
                now_ms=2_000,
            )
            self.assertEqual(completed["state"], "COMPLETED")
            self.assertFalse(completed["auto_replay_allowed"])

            verified = forum_ledger.transition_operation(
                path,
                "op-1",
                "VERIFIED",
                evidence={"readback_id": 3000},
                now_ms=3_000,
            )
            self.assertEqual(verified["state"], "VERIFIED")
            with self.assertRaises(forum_ledger.LedgerError):
                forum_ledger.transition_operation(
                    path, "op-1", "RECOVERABLE", now_ms=4_000
                )

    def test_transition_operation_persists_supplied_timestamp_and_bounded_error(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "operations.json"
            forum_ledger.begin_operation(
                path,
                operation="comment",
                intent={"post_id": 6108, "body": "hello"},
                now_ms=1_000,
                operation_id="op-1",
            )
            error = "E" * 2_500
            transitioned = forum_ledger.transition_operation(
                path,
                "op-1",
                "RECOVERABLE",
                evidence={"transport_status": "RATE_LIMITED"},
                error=error,
                now_ms=2_500,
            )
            self.assertEqual(transitioned["updated_at_ms"], 2_500)
            self.assertEqual(transitioned["error"], "E" * 2_000)
            self.assertFalse(transitioned["auto_replay_allowed"])
            self.assertEqual(
                transitioned["evidence"], {"transport_status": "RATE_LIMITED"}
            )

            raw = json.loads(path.read_text())
            self.assertEqual(raw["updated_at_ms"], 2_500)
            persisted = raw["operations"][0]
            self.assertEqual(persisted["updated_at_ms"], 2_500)
            self.assertEqual(persisted["error"], "E" * 2_000)
            self.assertNotIn("XXupdated_at_msXX", persisted)
            self.assertNotIn("UPDATED_AT_MS", persisted)
            self.assertNotIn("XXerrorXX", persisted)
            self.assertNotIn("ERROR", persisted)

    def test_transition_unknown_operation_fails_as_ledger_error(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "operations.json"
            forum_ledger.begin_operation(
                path,
                operation="comment",
                intent={"post_id": 6108, "body": "hello"},
                now_ms=1_000,
                operation_id="op-1",
            )
            with self.assertRaises(forum_ledger.LedgerError):
                forum_ledger.transition_operation(
                    path, "missing-op", "RECOVERABLE", now_ms=2_000
                )

    def test_record_reconciliation_persists_exact_history_contract_and_retains_last_twenty(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "operations.json"
            forum_ledger.begin_operation(
                path,
                operation="ack",
                intent={"up_to": {"version": 1}},
                now_ms=1_000,
                operation_id="op-1",
            )

            for index in range(21):
                delivery = ("unknown", "contradiction", "recovered_match")[index % 3]
                record = forum_ledger.record_reconciliation(
                    path,
                    "op-1",
                    delivery,
                    evidence={"index": index, "nested": {"value": index}},
                    error="E" * 2_500 if index == 20 else None,
                    now_ms=2_000 + index,
                )

            self.assertIsNotNone(record)
            self.assertEqual(record["state"], "ATTEMPTED")
            self.assertFalse(record["auto_replay_allowed"])
            self.assertEqual(record["updated_at_ms"], 2_020)
            history = record["evidence"]["reconciliation_history"]
            self.assertEqual(len(history), 20)
            self.assertEqual([entry["evidence"]["index"] for entry in history], list(range(1, 21)))
            last = history[-1]
            self.assertEqual(last["at_ms"], 2_020)
            self.assertEqual(last["delivery_state"], "recovered_match")
            self.assertEqual(last["evidence"], {"index": 20, "nested": {"value": 20}})
            self.assertEqual(last["error"], "E" * 2_000)

            raw = json.loads(path.read_text())
            self.assertEqual(raw["updated_at_ms"], 2_020)
            persisted = raw["operations"][0]
            self.assertEqual(persisted, record)
            self.assertNotIn("XXupdated_at_msXX", persisted)
            self.assertNotIn("UPDATED_AT_MS", persisted)
            self.assertNotIn("XXauto_replay_allowedXX", persisted)
            self.assertNotIn("AUTO_REPLAY_ALLOWED", persisted)
            self.assertNotIn("XXat_msXX", last)
            self.assertNotIn("AT_MS", last)
            self.assertNotIn("XXerrorXX", last)
            self.assertNotIn("ERROR", last)

    def test_record_reconciliation_unknown_operation_fails_as_ledger_error(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "operations.json"
            forum_ledger.begin_operation(
                path,
                operation="comment",
                intent={"post_id": 6108, "body": "hello"},
                now_ms=1_000,
                operation_id="op-1",
            )
            with self.assertRaises(forum_ledger.LedgerError):
                forum_ledger.record_reconciliation(
                    path, "missing-op", "unknown", now_ms=2_000
                )

    def test_record_reconciliation_terminal_operation_fails_as_ledger_error(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "operations.json"
            forum_ledger.record_blocked(
                path,
                operation="vote",
                intent={"kind": "comment", "id": 71462},
                error="precondition",
                now_ms=1_000,
                operation_id="op-terminal",
            )
            with self.assertRaises(forum_ledger.LedgerError):
                forum_ledger.record_reconciliation(
                    path, "op-terminal", "unknown", now_ms=2_000
                )

    def test_ledger_failure_before_write_prevents_external_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            operations = pathlib.Path(td) / "operations.json"
            calls = []
            with mock.patch(
                "forum_ledger.begin_operation",
                side_effect=OSError("simulated ledger fsync failure"),
            ):
                result = forum.execute(
                    forum.parse_args(["comment", "--post", "6108", "--body", "hello"]),
                    invoker=lambda *args, **kwargs: calls.append(args) or {"status": "OK"},
                    operations_path=operations,
                )
            self.assertEqual(result["status"], "BLOCKED")
            self.assertIn("before write", result["error"])
            self.assertEqual(calls, [])

    def test_non_ok_write_response_becomes_recoverable_and_is_not_retried(self):
        with tempfile.TemporaryDirectory() as td:
            operations = pathlib.Path(td) / "operations.json"
            calls = []

            def invoker(server, tool, payload):
                calls.append((server, tool, payload))
                return {
                    "status": "RATE_LIMITED",
                    "route": "forum-citizen.comment",
                    "error": "HTTP 429 after request may have left process",
                }

            result = forum.execute(
                forum.parse_args(["comment", "--post", "6108", "--body", "hello"]),
                invoker=invoker,
                operations_path=operations,
            )
            self.assertEqual(result["status"], "RECOVERABLE")
            self.assertEqual(len(calls), 1)
            raw = json.loads(operations.read_text())
            record = raw["operations"][0]
            self.assertEqual(record["state"], "RECOVERABLE")
            self.assertFalse(record["auto_replay_allowed"])
            self.assertEqual(record["evidence"]["transport_status"], "RATE_LIMITED")

    def test_write_ok_then_failed_readback_remains_recoverable(self):
        with tempfile.TemporaryDirectory() as td:
            operations = pathlib.Path(td) / "operations.json"
            calls = []

            def invoker(server, tool, payload):
                calls.append((server, tool, payload))
                if tool == "post":
                    return {"status": "OK", "data": {"post_id": 3000}}
                if tool == "read_post":
                    return {"status": "RATE_LIMITED", "error": "readback 429"}
                raise AssertionError(tool)

            result = forum.execute(
                forum.parse_args(["post", "--title", "T", "--body", "B"]),
                invoker=invoker,
                operations_path=operations,
            )
            self.assertEqual(result["status"], "RECOVERABLE")
            self.assertEqual([tool for _, tool, _ in calls], ["post", "read_post"])
            record = json.loads(operations.read_text())["operations"][0]
            self.assertEqual(record["state"], "RECOVERABLE")
            self.assertEqual(record["evidence"]["write_id"], 3000)

    def test_verified_write_is_recorded_verified(self):
        with tempfile.TemporaryDirectory() as td:
            operations = pathlib.Path(td) / "operations.json"

            def invoker(server, tool, payload):
                if tool == "comment":
                    return {"status": "OK", "data": {"comment_id": 4000}}
                if tool == "read_comment":
                    return {
                        "status": "OK",
                        "data": {
                            "comment": {
                                "id": 4000,
                                "post_id": 6108,
                                "body": "hello",
                            }
                        },
                    }
                raise AssertionError(tool)

            result = forum.execute(
                forum.parse_args(["comment", "--post", "6108", "--body", "hello"]),
                invoker=invoker,
                operations_path=operations,
            )
            self.assertEqual(result["status"], "WRITE_VERIFIED")
            record = json.loads(operations.read_text())["operations"][0]
            self.assertEqual(record["state"], "VERIFIED")
            self.assertEqual(record["evidence"]["readback_id"], 4000)

    def test_completed_or_recoverable_operations_are_visible_locally(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "operations.json"
            for index, state in enumerate(("ATTEMPTED", "COMPLETED", "RECOVERABLE", "VERIFIED"), start=1):
                forum_ledger.begin_operation(
                    path,
                    operation="comment",
                    intent={"post_id": 6108, "body": str(index)},
                    now_ms=index * 1_000,
                    operation_id=f"op-{index}",
                )
                if state == "COMPLETED":
                    forum_ledger.transition_operation(
                        path, f"op-{index}", "COMPLETED", now_ms=index * 1_000 + 1
                    )
                elif state == "RECOVERABLE":
                    forum_ledger.transition_operation(
                        path, f"op-{index}", "RECOVERABLE", now_ms=index * 1_000 + 1
                    )
                elif state == "VERIFIED":
                    forum_ledger.transition_operation(
                        path, f"op-{index}", "COMPLETED", now_ms=index * 1_000 + 1
                    )
                    forum_ledger.transition_operation(
                        path, f"op-{index}", "VERIFIED", now_ms=index * 1_000 + 2
                    )
            summary = forum_ledger.operations_summary(path, now_ms=10_000)
            self.assertEqual(summary["operations"], 4)
            self.assertEqual(summary["unresolved"], 3)
            self.assertEqual(summary["states"]["VERIFIED"], 1)
            self.assertEqual(summary["states"]["RECOVERABLE"], 1)

    def test_ack_transport_failure_is_recoverable_and_keeps_inbox_state(self):
        with tempfile.TemporaryDirectory() as td:
            state = pathlib.Path(td) / "state.json"
            operations = pathlib.Path(td) / "operations.json"
            offered = {
                "version": 1,
                "timestamp": 100,
                "comments": 10,
                "mentions": 20,
                "seal": "seal-a",
            }
            forum_state_data = {
                "ack_cursor": offered,
                "since_last_visit": {},
            }
            import forum_state
            forum_state.bank_inbox_page(state, forum_state_data, now_ms=1_000)
            before = state.read_bytes()
            calls = []

            result = forum.execute(
                forum.parse_args(["ack"]),
                invoker=lambda server, tool, payload: calls.append((server, tool, payload))
                or {"status": "RATE_LIMITED", "error": "429", "route": "forum-citizen.me_ack"},
                state_path=state,
                operations_path=operations,
            )
            self.assertEqual(result["status"], "RECOVERABLE")
            self.assertEqual([tool for _, tool, _ in calls], ["me_ack"])
            self.assertEqual(state.read_bytes(), before)
            record = json.loads(operations.read_text())["operations"][0]
            self.assertEqual(record["operation"], "ack")
            self.assertEqual(record["state"], "RECOVERABLE")

    def test_vote_precondition_failure_records_blocked_without_write(self):
        with tempfile.TemporaryDirectory() as td:
            operations = pathlib.Path(td) / "operations.json"
            calls = []

            def invoker(server, tool, payload):
                calls.append((server, tool, payload))
                self.assertEqual(tool, "read_comment")
                return {"status": "RATE_LIMITED", "error": "pre-read 429"}

            result = forum.execute(
                forum.parse_args(["vote", "comment", "24315"]),
                invoker=invoker,
                operations_path=operations,
            )
            self.assertEqual(result["status"], "RATE_LIMITED")
            self.assertEqual([tool for _, tool, _ in calls], ["read_comment"])
            record = json.loads(operations.read_text())["operations"][0]
            self.assertEqual(record["operation"], "vote")
            self.assertEqual(record["state"], "BLOCKED")
            self.assertFalse(record["auto_replay_allowed"])

    def test_vote_verified_path_is_recorded_verified(self):
        with tempfile.TemporaryDirectory() as td:
            operations = pathlib.Path(td) / "operations.json"
            calls = []

            def invoker(server, tool, payload):
                calls.append((server, tool, payload))
                reads = [row for row in calls if row[1] == "read_comment"]
                if tool == "read_comment" and len(reads) == 1:
                    return {"status": "OK", "data": {"comment": {"id": 24315, "votes": 3}}}
                if tool == "vote":
                    return {"status": "OK", "data": {"ok": True}}
                if tool == "read_comment":
                    return {"status": "OK", "data": {"comment": {"id": 24315, "votes": 4}}}
                raise AssertionError(tool)

            result = forum.execute(
                forum.parse_args(["vote", "comment", "24315"]),
                invoker=invoker,
                operations_path=operations,
            )
            self.assertEqual(result["status"], "WRITE_VERIFIED")
            record = json.loads(operations.read_text())["operations"][0]
            self.assertEqual(record["state"], "VERIFIED")
            self.assertEqual(record["evidence"]["before_votes"], 3)
            self.assertEqual(record["evidence"]["after_votes"], 4)

    def test_remote_write_ok_but_local_completion_update_failure_stays_attempted(self):
        with tempfile.TemporaryDirectory() as td:
            operations = pathlib.Path(td) / "operations.json"
            original = forum_ledger.transition_operation
            calls = []

            def invoker(server, tool, payload):
                calls.append((server, tool, payload))
                if tool == "comment":
                    return {"status": "OK", "data": {"comment_id": 4000}}
                raise AssertionError("readback must not run after ledger persistence failure")

            with mock.patch(
                "forum_ledger.transition_operation",
                side_effect=OSError("simulated post-write ledger fsync failure"),
            ):
                result = forum.execute(
                    forum.parse_args(["comment", "--post", "6108", "--body", "hello"]),
                    invoker=invoker,
                    operations_path=operations,
                )

            self.assertEqual(result["status"], "RECOVERABLE")
            self.assertIn("ledger_error", result)
            self.assertEqual([tool for _, tool, _ in calls], ["comment"])
            record = json.loads(operations.read_text())["operations"][0]
            self.assertEqual(record["state"], "ATTEMPTED")
            self.assertFalse(record["auto_replay_allowed"])



if __name__ == "__main__":
    unittest.main()
