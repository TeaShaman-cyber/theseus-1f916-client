import json
import pathlib
import tempfile
import unittest
from dataclasses import dataclass
from unittest import mock

import forum
import forum_ledger


ROOT = pathlib.Path(__file__).resolve().parents[1]
TAXONOMY = ROOT / "docs" / "qa" / "fault-matrix.json"


@dataclass(frozen=True)
class OperationSpec:
    name: str
    argv: tuple
    write_tool: str
    read_tool: str
    id_key: str
    object_key: str
    object_id: int

    def write_result(self):
        return {"status": "OK", "data": {self.id_key: self.object_id}}

    def exact_object(self):
        if self.name == "post":
            return {
                "id": self.object_id,
                "title": "T",
                "body": "B",
            }
        return {
            "id": self.object_id,
            "post_id": 6108,
            "parent_id": None,
            "body": "hello",
        }

    def mismatched_object(self):
        row = dict(self.exact_object())
        row["body"] = "different"
        return row


OPERATIONS = (
    OperationSpec(
        "post",
        ("post", "--title", "T", "--body", "B"),
        "post",
        "read_post",
        "post_id",
        "post",
        3000,
    ),
    OperationSpec(
        "comment",
        ("comment", "--post", "6108", "--body", "hello"),
        "comment",
        "read_comment",
        "comment_id",
        "comment",
        4000,
    ),
)

TRANSPORTS = ("http", "mcp")


class ScriptedFaultInvoker:
    def __init__(self, spec, fault, transport):
        self.spec = spec
        self.fault = fault
        self.transport = transport
        self.calls = []

    def __call__(self, surface, tool, payload):
        self.calls.append((surface, tool, payload))
        route = f"{self.transport}:{tool}"

        if tool == self.spec.write_tool:
            if self.fault == "ambiguous_write_outcome":
                return {
                    "status": "RATE_LIMITED",
                    "route": route,
                    "error": "simulated ambiguous 429",
                }
            return self.spec.write_result()

        if tool == self.spec.read_tool:
            if self.fault == "readback_unavailable":
                return {
                    "status": "RATE_LIMITED",
                    "route": route,
                    "error": "simulated readback 429",
                }
            if self.fault == "readback_mismatch":
                return {
                    "status": "OK",
                    "route": route,
                    "data": {self.spec.object_key: self.spec.mismatched_object()},
                }
            return {
                "status": "OK",
                "route": route,
                "data": {self.spec.object_key: self.spec.exact_object()},
            }

        raise AssertionError(f"unexpected tool: {tool}")


class FaultMatrixTests(unittest.TestCase):
    def test_taxonomy_is_small_and_covers_grounded_classes(self):
        data = json.loads(TAXONOMY.read_text())
        self.assertEqual(data["schema_version"], 1)
        rows = data["rows"]
        self.assertLessEqual(len(rows), 8)
        ids = {row["id"] for row in rows}
        self.assertEqual(
            ids,
            {
                "pre_write_local_failure",
                "ambiguous_write_outcome",
                "post_write_local_persistence_failure",
                "readback_unavailable",
                "contradictory_readback",
                "shared_edge_429",
                "stale_external_currentness",
            },
        )

    def test_write_fault_matrix_preserves_durable_state_call_count_and_route(self):
        cases = {
            "ambiguous_write_outcome": {
                "calls": 1,
                "transport_status": "RATE_LIMITED",
            },
            "readback_unavailable": {
                "calls": 2,
                "readback_status": "RATE_LIMITED",
            },
            "readback_mismatch": {
                "calls": 2,
                "readback_status": "OK",
            },
        }

        for spec in OPERATIONS:
            for transport in TRANSPORTS:
                for fault, expected in cases.items():
                    with self.subTest(
                        operation=spec.name, transport=transport, fault=fault
                    ), tempfile.TemporaryDirectory() as td:
                        operations = pathlib.Path(td) / "operations.json"
                        invoker = ScriptedFaultInvoker(spec, fault, transport)
                        result = forum.execute(
                            forum.parse_args(list(spec.argv)),
                            invoker=invoker,
                            operations_path=operations,
                        )

                        self.assertEqual(result["status"], "RECOVERABLE")
                        self.assertEqual(result["operation"], spec.name)
                        self.assertEqual(len(invoker.calls), expected["calls"])

                        ledger = forum_ledger.load_ledger(operations)
                        self.assertEqual(len(ledger["operations"]), 1)
                        record = ledger["operations"][0]
                        self.assertEqual(record["state"], "RECOVERABLE")
                        self.assertFalse(record["auto_replay_allowed"])

                        if fault == "ambiguous_write_outcome":
                            self.assertEqual(
                                record["evidence"]["transport_status"],
                                expected["transport_status"],
                            )
                            self.assertEqual(
                                record["evidence"]["route"],
                                f"{transport}:{spec.write_tool}",
                            )
                            self.assertEqual(
                                result["route"], f"{transport}:{spec.write_tool}"
                            )
                        else:
                            self.assertEqual(
                                record["evidence"]["readback_status"],
                                expected["readback_status"],
                            )
                            self.assertEqual(
                                result["route"], f"{transport}:{spec.read_tool}"
                            )

    def test_pre_write_local_failure_spends_zero_remote_calls(self):
        for spec in OPERATIONS:
            with self.subTest(operation=spec.name), tempfile.TemporaryDirectory() as td:
                operations = pathlib.Path(td) / "operations.json"
                calls = []
                with mock.patch(
                    "forum_ledger.begin_operation",
                    side_effect=OSError("simulated pre-write fsync failure"),
                ):
                    result = forum.execute(
                        forum.parse_args(list(spec.argv)),
                        invoker=lambda *args, **kwargs: calls.append(args)
                        or {"status": "OK"},
                        operations_path=operations,
                    )
                self.assertEqual(result["status"], "BLOCKED")
                self.assertIn("before write", result["error"])
                self.assertEqual(calls, [])

    def test_post_write_local_persistence_failure_stops_before_readback(self):
        for spec in OPERATIONS:
            for transport in TRANSPORTS:
                with self.subTest(
                    operation=spec.name, transport=transport
                ), tempfile.TemporaryDirectory() as td:
                    operations = pathlib.Path(td) / "operations.json"
                    invoker = ScriptedFaultInvoker(spec, "success", transport)
                    with mock.patch(
                        "forum_ledger.transition_operation",
                        side_effect=OSError("simulated post-write fsync failure"),
                    ):
                        result = forum.execute(
                            forum.parse_args(list(spec.argv)),
                            invoker=invoker,
                            operations_path=operations,
                        )

                    self.assertEqual(result["status"], "RECOVERABLE")
                    self.assertIn("ledger_error", result)
                    self.assertEqual(len(invoker.calls), 1)

                    record = forum_ledger.load_ledger(operations)["operations"][0]
                    self.assertEqual(record["state"], "ATTEMPTED")
                    self.assertFalse(record["auto_replay_allowed"])

    def test_reconciliation_matrix_records_unknown_and_contradiction(self):
        for spec in OPERATIONS:
            for transport in TRANSPORTS:
                for delivery in ("unknown", "contradiction"):
                    with self.subTest(
                        operation=spec.name,
                        transport=transport,
                        delivery=delivery,
                    ), tempfile.TemporaryDirectory() as td:
                        operations = pathlib.Path(td) / "operations.json"
                        forum_ledger.begin_operation(
                            operations,
                            operation=spec.name,
                            intent=(
                                {"title": "T", "body": "B"}
                                if spec.name == "post"
                                else {
                                    "post_id": 6108,
                                    "body": "hello",
                                    "parent_id": None,
                                }
                            ),
                            now_ms=1000,
                            operation_id="op-1",
                        )
                        forum_ledger.transition_operation(
                            operations,
                            "op-1",
                            "RECOVERABLE",
                            evidence={"write_id": spec.object_id},
                            error="ambiguous",
                            now_ms=2000,
                        )

                        calls = []

                        def invoker(surface, tool, payload):
                            calls.append((surface, tool, payload))
                            route = f"{transport}:{tool}"
                            if delivery == "unknown":
                                return {
                                    "status": "RATE_LIMITED",
                                    "route": route,
                                    "error": "readback 429",
                                }
                            return {
                                "status": "OK",
                                "route": route,
                                "data": {
                                    spec.object_key: spec.mismatched_object()
                                },
                            }

                        result = forum.execute(
                            forum.parse_args(["reconcile", "op-1"]),
                            invoker=invoker,
                            operations_path=operations,
                        )
                        expected_status = (
                            "RATE_LIMITED" if delivery == "unknown" else "OK"
                        )
                        self.assertEqual(result["status"], expected_status)
                        self.assertEqual(result["delivery_state"], delivery)
                        self.assertEqual(len(calls), 1)

                        record = forum_ledger.get_operation(operations, "op-1")
                        self.assertEqual(record["state"], "RECOVERABLE")
                        self.assertFalse(record["auto_replay_allowed"])
                        history = record["evidence"]["reconciliation_history"][-1]
                        self.assertEqual(history["delivery_state"], delivery)

                        if delivery == "contradiction":
                            evidence = history["evidence"]
                            self.assertEqual(len(evidence["expected_sha256"]), 64)
                            self.assertEqual(len(evidence["observed_sha256"]), 64)
                            self.assertNotIn("expected", evidence)
                            self.assertNotIn("observed", evidence)

    def test_shared_edge_429_matrix_spends_one_request_and_no_peer(self):
        cases = (
            ("read_post", {"post_id": 6108}),
            ("search", {"query": "client"}),
        )
        for tool, payload in cases:
            with self.subTest(tool=tool):
                calls = []
                result = forum.auto_invoke(
                    "read",
                    tool,
                    payload,
                    http_invoker=lambda *args: calls.append(("http", args))
                    or {
                        "status": "RATE_LIMITED",
                        "route": f"http:{tool}",
                        "error": "429",
                    },
                    mcp_invoker=lambda *args: (_ for _ in ()).throw(
                        AssertionError("shared/unknown edge scope must not fallback")
                    ),
                )
                self.assertEqual(result["status"], "RATE_LIMITED")
                self.assertEqual(len(calls), 1)
                self.assertEqual(result["observer_scope"], "shared_or_unknown_edge")
                self.assertEqual(result["retry_policy"], "rate-limit-backoff-no-peer")
                self.assertGreaterEqual(result["recommended_backoff_seconds"], 10)


if __name__ == "__main__":
    unittest.main()
