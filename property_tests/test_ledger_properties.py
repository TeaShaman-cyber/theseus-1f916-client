import pathlib
import tempfile
import unittest

from hypothesis import given, settings, strategies as st

import forum_ledger


PROPERTY_SETTINGS = settings(
    max_examples=250,
    deadline=None,
    derandomize=True,
    database=None,
)

TEXT = st.text(min_size=0, max_size=48)
OPERATION = st.sampled_from(("post", "comment", "vote", "ack"))


class ExecutionLedgerPropertyTests(unittest.TestCase):
    @PROPERTY_SETTINGS
    @given(operation=OPERATION, terminal=st.sampled_from(("VERIFIED", "RECOVERABLE")))
    def test_legal_write_paths_never_enable_auto_replay(self, operation, terminal):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "operations.json"
            record = forum_ledger.begin_operation(
                path,
                operation=operation,
                intent={"marker": "intent"},
                now_ms=1_000,
                operation_id="op",
            )
            self.assertFalse(record["auto_replay_allowed"])

            if terminal == "VERIFIED":
                record = forum_ledger.transition_operation(
                    path, "op", "COMPLETED", now_ms=2_000
                )
                self.assertFalse(record["auto_replay_allowed"])
                record = forum_ledger.transition_operation(
                    path, "op", "VERIFIED", now_ms=3_000
                )
            else:
                record = forum_ledger.transition_operation(
                    path, "op", "RECOVERABLE", now_ms=2_000
                )
            self.assertFalse(record["auto_replay_allowed"])

    @PROPERTY_SETTINGS
    @given(operation=OPERATION, attempted_target=st.sampled_from(tuple(sorted(forum_ledger.STATES))))
    def test_terminal_verified_or_blocked_states_reject_every_further_transition(
        self, operation, attempted_target
    ):
        for terminal in ("VERIFIED", "BLOCKED"):
            with tempfile.TemporaryDirectory() as td:
                path = pathlib.Path(td) / "operations.json"
                if terminal == "BLOCKED":
                    forum_ledger.record_blocked(
                        path,
                        operation=operation,
                        intent={"marker": "intent"},
                        error="local precondition",
                        now_ms=1_000,
                        operation_id="op",
                    )
                else:
                    forum_ledger.begin_operation(
                        path,
                        operation=operation,
                        intent={"marker": "intent"},
                        now_ms=1_000,
                        operation_id="op",
                    )
                    forum_ledger.transition_operation(
                        path, "op", "COMPLETED", now_ms=2_000
                    )
                    forum_ledger.transition_operation(
                        path, "op", "VERIFIED", now_ms=3_000
                    )
                with self.assertRaises(forum_ledger.LedgerError):
                    forum_ledger.transition_operation(
                        path, "op", attempted_target, now_ms=4_000
                    )

    @PROPERTY_SETTINGS
    @given(operation=OPERATION, error=TEXT, marker=TEXT)
    def test_recoverable_transition_preserves_intent_and_records_evidence(
        self, operation, error, marker
    ):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "operations.json"
            intent = {"marker": marker, "nested": {"value": 1}}
            forum_ledger.begin_operation(
                path,
                operation=operation,
                intent=intent,
                now_ms=1_000,
                operation_id="op",
            )
            record = forum_ledger.transition_operation(
                path,
                "op",
                "RECOVERABLE",
                evidence={"transport_status": "RATE_LIMITED"},
                error=error,
                now_ms=2_000,
            )
            self.assertEqual(record["intent"], intent)
            self.assertEqual(record["evidence"]["transport_status"], "RATE_LIMITED")
            self.assertFalse(record["auto_replay_allowed"])


if __name__ == "__main__":
    unittest.main()
