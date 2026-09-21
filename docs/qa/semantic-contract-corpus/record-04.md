`.forum-state.json` is ignored runtime state. Inbox work is banked before it becomes ackable. The stored acknowledgement cursor is the exact server-offered cursor, including seals; the client does not synthesize mixed sealed cursors.

A verified acknowledgement advances/prunes only work covered by the acknowledged floor. Legacy pre-v1 pending-ack-only state loads as `RECOVERY_REQUIRED` and is not ackable until a fresh page is durably banked. Corrupt or unsupported durable state fails closed.
