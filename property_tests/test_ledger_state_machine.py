import pathlib
import tempfile

from hypothesis import settings, strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

import forum_ledger


STATEFUL_SETTINGS = settings(
    max_examples=60,
    stateful_step_count=20,
    deadline=None,
    derandomize=True,
    database=None,
)

OPERATION = st.sampled_from(("post", "comment", "vote", "ack"))
DELIVERY = st.sampled_from(("unknown", "contradiction", "recovered_match"))
TARGET_STATE = st.sampled_from(tuple(sorted(forum_ledger.STATES)))


class LedgerLifecycleMachine(RuleBasedStateMachine):
    """Small model of state/evidence semantics, not of ledger implementation details."""

    def __init__(self):
        super().__init__()
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tempdir.name) / "operations.json"
        self.operation_id = "stateful-op"
        self.operation = None
        self.intent = None
        self.model_state = None
        self.model_history = []
        self.clock = 1_000

    def teardown(self):
        self.tempdir.cleanup()

    def tick(self):
        self.clock += 100
        return self.clock

    @initialize(operation=OPERATION, start_blocked=st.booleans())
    def start(self, operation, start_blocked):
        self.operation = operation
        self.intent = {"marker": "stateful", "operation": operation}
        if start_blocked:
            forum_ledger.record_blocked(
                self.path,
                operation=operation,
                intent=self.intent,
                error="local precondition",
                now_ms=self.tick(),
                operation_id=self.operation_id,
            )
            self.model_state = "BLOCKED"
        else:
            forum_ledger.begin_operation(
                self.path,
                operation=operation,
                intent=self.intent,
                now_ms=self.tick(),
                operation_id=self.operation_id,
            )
            self.model_state = "ATTEMPTED"

    @precondition(lambda self: self.model_state == "ATTEMPTED")
    @rule()
    def complete(self):
        forum_ledger.transition_operation(
            self.path,
            self.operation_id,
            "COMPLETED",
            evidence={"transport": "write-returned"},
            now_ms=self.tick(),
        )
        self.model_state = "COMPLETED"

    @precondition(lambda self: self.model_state in {"ATTEMPTED", "COMPLETED"})
    @rule()
    def become_recoverable(self):
        forum_ledger.transition_operation(
            self.path,
            self.operation_id,
            "RECOVERABLE",
            evidence={"transport": "ambiguous"},
            error="delivery uncertain",
            now_ms=self.tick(),
        )
        self.model_state = "RECOVERABLE"

    @precondition(lambda self: self.model_state in {"COMPLETED", "RECOVERABLE"})
    @rule()
    def verify_by_transition(self):
        forum_ledger.transition_operation(
            self.path,
            self.operation_id,
            "VERIFIED",
            evidence={"readback": "match"},
            now_ms=self.tick(),
        )
        self.model_state = "VERIFIED"

    @precondition(lambda self: self.model_state in forum_ledger.UNRESOLVED_STATES)
    @rule(delivery=DELIVERY, marker=st.integers(min_value=0, max_value=10_000))
    def record_reconciliation(self, delivery, marker):
        forum_ledger.record_reconciliation(
            self.path,
            self.operation_id,
            delivery,
            evidence={"marker": marker},
            error=None if delivery == "recovered_match" else "still unresolved",
            now_ms=self.tick(),
        )
        self.model_history.append(delivery)
        self.model_history = self.model_history[-20:]

    @precondition(lambda self: self.model_state in forum_ledger.UNRESOLVED_STATES)
    @rule(marker=st.integers(min_value=0, max_value=10_000))
    def verify_by_reconciliation(self, marker):
        forum_ledger.mark_reconciled_verified(
            self.path,
            self.operation_id,
            evidence={"marker": marker, "readback": "exact"},
            now_ms=self.tick(),
        )
        self.model_history.append("recovered_match")
        self.model_history = self.model_history[-20:]
        self.model_state = "VERIFIED"

    @precondition(lambda self: self.model_state in forum_ledger.UNRESOLVED_STATES)
    @rule(target=TARGET_STATE)
    def reject_illegal_unresolved_transition(self, target):
        allowed = forum_ledger.TRANSITIONS[self.model_state]
        if target in allowed:
            return
        before = self.path.read_bytes()
        try:
            forum_ledger.transition_operation(
                self.path,
                self.operation_id,
                target,
                now_ms=self.tick(),
            )
        except forum_ledger.LedgerError:
            pass
        else:
            raise AssertionError(
                f"illegal transition unexpectedly succeeded: {self.model_state} -> {target}"
            )
        assert self.path.read_bytes() == before

    @precondition(lambda self: self.model_state in {"VERIFIED", "BLOCKED"})
    @rule(target=TARGET_STATE)
    def terminal_transition_is_rejected(self, target):
        before = self.path.read_bytes()
        try:
            forum_ledger.transition_operation(
                self.path,
                self.operation_id,
                target,
                now_ms=self.tick(),
            )
        except forum_ledger.LedgerError:
            pass
        else:
            raise AssertionError(
                f"terminal transition unexpectedly succeeded: {self.model_state} -> {target}"
            )
        assert self.path.read_bytes() == before

    @precondition(lambda self: self.model_state in {"VERIFIED", "BLOCKED"})
    @rule(delivery=DELIVERY)
    def terminal_reconciliation_is_rejected(self, delivery):
        before = self.path.read_bytes()
        try:
            forum_ledger.record_reconciliation(
                self.path,
                self.operation_id,
                delivery,
                now_ms=self.tick(),
            )
        except forum_ledger.LedgerError:
            pass
        else:
            raise AssertionError(
                f"terminal reconciliation unexpectedly succeeded from {self.model_state}"
            )
        assert self.path.read_bytes() == before

    @precondition(lambda self: self.model_state in {"VERIFIED", "BLOCKED"})
    @rule()
    def terminal_reverification_is_rejected(self):
        before = self.path.read_bytes()
        try:
            forum_ledger.mark_reconciled_verified(
                self.path,
                self.operation_id,
                now_ms=self.tick(),
            )
        except forum_ledger.LedgerError:
            pass
        else:
            raise AssertionError(
                f"terminal re-verification unexpectedly succeeded from {self.model_state}"
            )
        assert self.path.read_bytes() == before

    @invariant()
    def persisted_state_matches_model(self):
        if self.model_state is None:
            return
        record = forum_ledger.get_operation(self.path, self.operation_id)
        ledger = forum_ledger.load_ledger(self.path)

        assert record["state"] == self.model_state
        assert record["intent"] == self.intent
        assert record["auto_replay_allowed"] is False
        assert ledger["updated_at_ms"] == record["updated_at_ms"]

        history = record["evidence"].get("reconciliation_history", [])
        assert isinstance(history, list)
        assert len(history) <= 20
        assert [row["delivery_state"] for row in history] == self.model_history

        if self.model_state in {"VERIFIED", "BLOCKED"}:
            assert self.model_state not in forum_ledger.UNRESOLVED_STATES


TestLedgerLifecycleMachine = LedgerLifecycleMachine.TestCase
TestLedgerLifecycleMachine.settings = STATEFUL_SETTINGS
